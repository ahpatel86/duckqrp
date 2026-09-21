"""
Pipeline orchestration.

The whole Type 2 pipeline is a linear sequence of named stages. There is
no cohort loop: resolved configuration is registered as DuckDB tables and
the SQL joins to it, so every cohort is evaluated in the same pass.

That single change removes, at a stroke:
  * the per-cohort re-scan of the shared claim tables,
  * the write/read round trip through `<dplocal>/mstr` that the SAS
    `proc append` pattern forced,
  * the schema-drift hack that round trip required (narrow cohorts
    inferring INTEGER where wide cohorts wrote DOUBLE), and
  * the need for cross-iteration accumulator variables.
"""

from __future__ import annotations

import re as _re
from datetime import date, timedelta
import time
from dataclasses import dataclass
import shutil as _shutil
import warnings
from pathlib import Path

from .config import StudyConfig
from .engine import Engine, sql_str
# Output naming, disclosure routing and writing live in outputs.py.
# Re-exported here because callers and tests import them from the
# pipeline, and moving a name is not the point of the split.
from .outputs import (  # noqa: F401
    DISCLOSURE,
    OPTIONAL_OUTPUT_TABLES,
    OUTPUT_TABLES,
    OUTPUTS,
    SAS_CONTRACT,
    SAS_NAME_NOTES,
    SAS_NAMES,
    Output,
    _run_metadata,
    tables_for,
    _write_split_layout,
)
from .events import EmptyResult, LogMessage, Level, RunFinished, RunStarted
from .scdm import SCDM, columns_of, identify_by_columns, reader, resolve_table

# Stages that produce a table the caller may want on disk.




def _finish(eng: Engine, study: StudyConfig, dt: float,
            verbose: bool) -> None:
    tables = {}
    for tbl in (*OUTPUT_TABLES, *(OPTIONAL_OUTPUT_TABLES
                                  if study.any_covariates else ())):
        try:
            tables[tbl] = eng.count(tbl)
        except Exception:
            pass
    eng.emit(RunFinished(
        seconds=dt, ok=True, tables=tables,
        peak_memory_bytes=eng.peak_memory_bytes,
        peak_spill_bytes=eng.peak_spill_bytes,
    ))
    if verbose:
        print(eng.summary())


def _check_exposure(eng: Engine, study: StudyConfig, indata) -> None:
    """Warn loudly when a cohort's codes match nothing in the data."""
    try:
        rows = eng.con.execute(
            "SELECT c.cohortgrp, count(e.patid) "
            "FROM (SELECT DISTINCT cohortgrp FROM cfg_codes "
            "      WHERE role = 'DEF') c "
            "LEFT JOIN exposure_claims e ON e.cohortgrp = c.cohortgrp "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    except Exception:
        return

    empty = [name for name, n in rows if n == 0]
    if not empty:
        return

    # Distinguish "these codes are absent" from "there are no claims at
    # all", because the fix is different.
    total_claims = eng.con.execute(
        "SELECT count(*) FROM cdm_dispensing"
    ).fetchone()[0]
    sample = eng.con.execute(
        "SELECT code FROM cfg_codes WHERE role = 'DEF' LIMIT 3"
    ).fetchall()
    have = eng.con.execute(
        "SELECT code FROM cdm_dispensing GROUP BY 1 ORDER BY count(*) DESC "
        "LIMIT 3"
    ).fetchall()

    if total_claims == 0:
        reason = f"No dispensing claims were read from {indata}."
        hint = "Check --indata and run `qrp inspect --indata <path>`."
    else:
        reason = (
            f"No exposure claims matched for: {', '.join(empty)}. "
            f"Every output table will be empty."
        )
        hint = (
            f"The study's codes are not in this data. Study expects e.g. "
            f"{', '.join(r[0] for r in sample)}; the data's most common "
            f"codes are {', '.join(r[0] for r in have)}. "
            f"This usually means the study definition and the extract do "
            f"not belong together."
        )
    eng.emit(EmptyResult(stage="exposure + stockpiling",
                         table="exposure_claims", reason=reason, hint=hint))
    eng.emit(LogMessage(level=Level.WARNING, message=reason, detail=hint))


_SAFE_LEVEL = _re.compile(r"[^A-Za-z0-9]")


def _covar_strata(study: StudyConfig) -> tuple[int, ...]:
    """Covariate numbers any t2cida level stratifies on.

    SAS's `&covarstrat.`: USERSTRATA levelvars beginning with "covar"
    add a column per covariate to t2_cida
    (ms_cidatables.sas:403-412, 425). Without them a study asking to
    stratify by covar1 got the UNSTRATIFIED totals labelled as that
    level — silently wrong, which is worse than a missing column.

    The union across levels, because the table is one relation: a level
    that does not stratify on a covariate carries NULL for it, the same
    convention used for agegroup and sex.
    """
    out: set[int] = set()
    for lv in study.cida_levels():
        for var in lv.levelvars:
            v = var.lower()
            if v.startswith("covar") and v[5:].isdigit():
                out.add(int(v[5:]))
    return tuple(sorted(out))


def _covar_strata_sql(study: StudyConfig) -> tuple[str, str, str, str]:
    """(base, select, group_by, final) SQL fragments for covariate strata.

    Empty strings when no level stratifies on a covariate, so the query
    is unchanged for the common case.
    """
    nums = _covar_strata(study)
    if not nums:
        return "", "", "", ""

    base_parts, sel_parts, grp_parts = [], [], []
    for n in nums:
        base_parts.append(
            ",\n        EXISTS (SELECT 1 FROM covariates_long x"
            " WHERE x.cohortgrp = c.cohortgrp AND x.patid = c.patid"
            f" AND x.indexdt = c.indexdt AND x.covarnum = {n})::INTEGER"
            f" AS covar{n}")
        # `covarstrat` is a space-joined list, so match the PADDED string:
        # a bare LIKE '%covar1%' would also match covar12.
        cond = (f"CASE WHEN ' ' || lv.covarstrat || ' ' LIKE '% covar{n} %'"
                f" THEN b.covar{n} END")
        sel_parts.append(f",\n    {cond} AS covar{n}")
        grp_parts.append(f",\n    {cond}")
    final_parts = [f",\n    n.covar{n}" for n in nums]
    return ("".join(base_parts), "".join(sel_parts), "".join(grp_parts),
            "".join(final_parts))


def _baseline_dummies(eng: Engine, study: StudyConfig) -> str:
    """Widen `baseline` with one column per OBSERVED category level.

    SAS emits Sex_F, Sex_M, Race_1.., Hispanic_Y, Age<bucket> and
    covar1..covarN as 0/1 dummies summed per group
    (ms_createdistbaselinetable.sas:455-470), then SQUARES the result so
    a level absent from one group is 0 rather than missing.

    The levels are read from the data rather than hardcoded: a race code
    or age band the study did not anticipate would otherwise vanish
    silently. Level values are sanitised to `[A-Za-z0-9]` before
    becoming identifiers — they come from claims data, so they are
    never interpolated raw.
    """
    dummies: list[str] = []
    # SAS sums these dummy groups: patient, Age:, Sex_:, year_:,
    # race_:, hispanic_:, covar1..N, and cb_reg_:/sdi_: when geography
    # is on (ms_createdistbaselinetable.sas:457-460). `year_` was
    # missing here — an index-year distribution the study asked for and
    # did not get.
    for column, prefix in (("sex", "Sex"), ("race", "Race"),
                           ("hispanic", "Hispanic"), ("agegroup", "Age"),
                           ):
        rows = eng.con.execute(
            f"SELECT DISTINCT {column} FROM cohort_final "
            f"WHERE {column} IS NOT NULL ORDER BY 1"
        ).fetchall()
        seen: set[str] = set()
        for (value,) in rows:
            safe = _SAFE_LEVEL.sub("_", str(value))
            if not safe:
                continue
            # Two levels can sanitise to the same identifier — 'A-B' and
            # 'A_B' both become 'A_B' — which would emit a duplicate
            # column. Disambiguate rather than silently drop one.
            base, n = safe, 2
            while f"{prefix}_{safe}" in seen:
                safe = f"{base}_{n}"
                n += 1
            seen.add(f"{prefix}_{safe}")
            # COUNT over a filtered CASE, so an absent level sums to 0
            # rather than NULL — that is the squaring.
            # The IDENTIFIER is sanitised; the LITERAL must be escaped.
            # A level value of O'BRIEN would otherwise emit
            # `c.race = 'O'BRIEN'` — invalid SQL. These values come from
            # claims data, so the assumption that they contain no quotes
            # is not one to rely on. Reported in review.
            dummies.append(
                f"sum(CASE WHEN c.{column} = {sql_str(value)} "
                f"THEN 1 ELSE 0 END) AS \"{prefix}_{safe}\""
            )

    # Index-year dummies. Derived, not a stored column, so built
    # separately from the categorical ones above.
    years = eng.con.execute(
        "SELECT DISTINCT year(indexdt) FROM cohort_final "
        "WHERE indexdt IS NOT NULL ORDER BY 1").fetchall()
    for (yr,) in years:
        dummies.append(
            f"sum(CASE WHEN year(c.indexdt) = {int(yr)} "
            f"THEN 1 ELSE 0 END) AS \"year_{int(yr)}\"")

    # Geography dummies, only when the geography stage ran — SAS gates
    # these on `&geog = Y` for the same reason.
    for column, prefix in (("cb_reg", "cb_reg"), ("sdi_cat", "sdi")):
        try:
            rows = eng.con.execute(
                f"SELECT DISTINCT {column} FROM cohort_final "
                f"WHERE {column} IS NOT NULL ORDER BY 1").fetchall()
        except Exception:
            continue                      # column absent: stage not run
        for (value,) in rows:
            safe = _SAFE_LEVEL.sub("_", str(value))
            if not safe:
                continue
            dummies.append(
                f"sum(CASE WHEN c.{column} = {sql_str(value)} "
                f"THEN 1 ELSE 0 END) AS \"{prefix}_{safe}\"")

    for cov in study.covariates:
        dummies.append(
            f"sum(CASE WHEN EXISTS (SELECT 1 FROM covariates_long x "
            f"WHERE x.cohortgrp = c.cohortgrp AND x.patid = c.patid "
            f"AND x.indexdt = c.indexdt AND x.covarnum = {int(cov.covarnum)})"
            f" THEN 1 ELSE 0 END) AS \"covar{int(cov.covarnum)}\""
        )

    # Continuous variables get mean_ and std_, not a sum
    # (ms_createdistbaselinetable.sas:474-500). SAS lists Age, the risk
    # scores, and the utilization counts NumAV/NumOA/NumIP/NumIS/NumED
    # and NumGeneric/NumClass/NumRx. Age is in 96_baseline.sql; the
    # others are added here when their optional stage produced them,
    # exactly as SAS gates them.
    # (source column, SAS name). The two differ: the utilization stage
    # names its encounter counts enc_av/enc_oa/..., SAS calls them
    # NumAV/NumOA/.... An earlier version listed only the SAS names and
    # the `continue` below silently skipped five of the eight — the
    # exact failure this mapping exists to prevent.
    for table, cols in (
        ("utilization", (("enc_av", "NumAV"), ("enc_oa", "NumOA"),
                         ("enc_ip", "NumIP"), ("enc_is", "NumIS"),
                         ("enc_ed", "NumED"), ("numgeneric", "NumGeneric"),
                         ("numclass", "NumClass"), ("numrx", "NumRx"))),
        ("risk_scores", (("score", "RiskScore"),)),
    ):
        try:
            have = {d[0].lower() for d in eng.con.execute(
                f"SELECT * FROM {table} LIMIT 0").description}
        except Exception:
            continue                      # optional stage did not run
        missing = [src for src, _ in cols if src not in have]
        if missing:
            # Loud, not silent: a column the stage was expected to
            # produce and did not is a defect, not a configuration.
            warnings.warn(
                f"baseline: {table} is missing {missing}; those "
                f"mean_/std_ columns will be absent from the baseline "
                f"table", stacklevel=2)
        for src, sas in cols:
            if src not in have:
                continue
            lookup = (f"(SELECT u.{src} FROM {table} u "
                      f"WHERE u.cohortgrp = c.cohortgrp "
                      f"AND u.patid = c.patid AND u.indexdt = c.indexdt)")
            dummies.append(f"round(avg({lookup}), 4) AS \"mean_{sas}\"")
            dummies.append(
                f"round(stddev_samp({lookup}), 4) AS \"std_{sas}\"")

    if not dummies:
        return ""
    return (
        "CREATE OR REPLACE TABLE baseline AS\n"
        "SELECT b.*, " + ",\n       ".join(dummies) + "\n"
        "FROM baseline b\n"
        "JOIN cohort_final c ON c.cohortgrp = b.\"group\"\n"
        "GROUP BY ALL\n"
        "ORDER BY 1;"
    )


def _combo_sql(study: StudyConfig) -> str:
    """Build the INSERT for combo covariates (codecat='CC').

    Each is a boolean expression over other covariate numbers — e.g.
    `2 and (3 or 4 or 5 or 6)`. The parser produced a template with {N}
    placeholders; each becomes an EXISTS against covariates_long, so the
    expression evaluates per episode.

    Only integers substituted into a fixed template ever reach SQL. The
    study's own text is never concatenated in.
    """
    parts = []
    for cov in study.covariates:
        if cov.codecat != "CC":
            continue
        tests = {
            n: (f"EXISTS (SELECT 1 FROM covariates_long x "
                f"WHERE x.cohortgrp = m.cohortgrp AND x.patid = m.patid "
                f"AND x.indexdt = m.indexdt AND x.covarnum = {int(n)})")
            for n in cov.combo_refs
        }
        expr = cov.combo_sql.format(**{f"c{k}": v for k, v in tests.items()})
        name = cov.covarname.replace("'", "''")
        parts.append(
            f"SELECT m.cohortgrp, m.patid, m.indexdt, {int(cov.covarnum)}, "
            f"'{name}' FROM ptsmasterlist m WHERE {expr}"
        )
    return ("INSERT INTO covariates_long "
            "(cohortgrp, patid, indexdt, covarnum, covarname)\n"
            + "\nUNION ALL\n".join(parts) + ";")


def _empty_relation(spec) -> str:
    """A typed empty table, for an optional SCDM table that is absent.

    Better than failing: a site with no death file should still be able
    to run, with death censoring simply never triggering.
    """
    cols = ", ".join(f"NULL AS {c}" for c in (*spec.required, *spec.optional))
    return f"(SELECT {cols} WHERE FALSE)"


COVAR_SOURCE_VIEW = """
CREATE OR REPLACE VIEW covar_source AS
-- rxsup/rxamt are carried so the inclusion stage can compute dose from
-- the MATCHED claim. They are NULL on the DX side, which is correct:
-- a diagnosis has no supply or amount.
-- enctype/pdx carry the CARE SETTING. Risk-score codes can restrict on
-- it (RISKSCORECODES.caresettingprincipal), and without these columns
-- that restriction was parsed and silently dropped — a score computed
-- over every setting when the study asked for one.
SELECT patid, adate, adate AS expiredt, code, 'DX' AS codecat,
       NULL::INTEGER AS rxsup, NULL::DOUBLE AS rxamt,
       enctype, COALESCE(pdx, '') AS pdx
FROM cdm_diagnosis
UNION ALL
SELECT patid, adate, adate + CAST(rxsup - 1 AS INTEGER) AS expiredt,
       code, 'RX' AS codecat, rxsup, rxamt,
       -- A dispensing has no encounter type; '**'/'' is the wildcard,
       -- so an unrestricted rule still matches and a restricted one
       -- correctly does not.
       '**' AS enctype, '' AS pdx
FROM cdm_dispensing
UNION ALL
-- A procedure is a point event, like a diagnosis.
SELECT patid, adate, adate AS expiredt, code, 'PX' AS codecat,
       NULL::INTEGER AS rxsup, NULL::DOUBLE AS rxamt,
       -- procedures carry enctype but no principal-diagnosis flag
       enctype, '' AS pdx
FROM cdm_procedure
"""


@dataclass(frozen=True)
class Stage:
    """One pipeline stage: its name, its SQL, and when it runs.

    THE stage list. `plan_stages()` filters it and `run()` executes it,
    so the two cannot disagree about which stages exist or in what
    order.

    They used to be two parallel sequences — one in `plan_stages()`, one
    in `run()` — and the docstring on the former claimed to be the
    single source of truth while the latter quietly went its own way.
    Three stages were added during one session and each needed
    editing in both places; one was missed, and only a UI test comparing
    the two counts caught it.

    `gate` names a StudyConfig property. None means the stage always
    runs. `skip_note` is what verbose mode prints when it does not.
    """
    name: str
    script: str
    gate: str | None = None
    skip_note: str = ""

    def runs_for(self, study: StudyConfig) -> bool:
        return self.gate is None or bool(getattr(study, self.gate))


STAGES: tuple[Stage, ...] = (
    Stage("normalize",              "10_normalize.sql"),
    Stage("enrollment_spans",       "20_enrollment.sql"),
    Stage("exposure + stockpiling", "30_exposure.sql"),
    Stage("index dates",            "40_index.sql"),
    Stage("dose restrictions",      "42_dose.sql", "any_dose",
          "dose restrictions (no cohort sets a dose limit)"),
    Stage("pov1",                   "45_pov1.sql"),
    Stage("episodes + masterlist",  "50_episodes.sql"),
    Stage("inclusion criteria",     "52_inclusion.sql", "any_inclusions"),
    Stage("dose censoring",         "55_dose_censor.sql",
          "any_dose_censoring"),
    Stage("follow-up + events",     "60_followup.sql"),
    Stage("attrition + denominators", "70_outputs.sql"),
    Stage("code distribution",      "72_codedistribution.sql",
          "any_code_distribution"),
    Stage("most frequent use",      "78_mfu.sql", "any_mfu"),
    Stage("labs",                   "76_labs.sql", "any_labs"),
    Stage("utilization",            "74_utilization.sql", "any_utilization"),
    Stage("geography",              "47_geography.sql", "any_geography"),
    Stage("risk scores",            "85_riskscores.sql", "any_risk_scores"),
    Stage("covariates",             "80_covariates.sql", "any_covariates",
          "covariates (none defined)"),
    Stage("baseline",               "96_baseline.sql", "any_covariates"),
    Stage("cida denominators",      "92_cidadenom.sql", "any_cida_tables"),
    Stage("cida tables",            "90_cidatables.sql", "any_cida_tables"),
    Stage("follow-up time table",   "94_followuptime.sql", "any_followuptime"),
)


_STAGE_BY_NAME: dict[str, Stage] = {st.name: st for st in STAGES}


def plan_stages(study: StudyConfig) -> list[str]:
    """Stage names in order, for this study.

    Derived from STAGES, so it cannot disagree with what `run()`
    actually executes. A progress bar needs the total before the first
    stage starts, and the log needs it to write [3/10] rather than
    [3/0]; both the CLI and the UI read it from here.
    """
    return [st.name for st in STAGES if st.runs_for(study)]

@dataclass
class RunResult:
    study: StudyConfig
    engine: Engine
    seconds: float

    def table(self, name: str):
        return self.engine.con.table(name)

    def df(self, name: str):
        return self.engine.con.table(name).df()


# --------------------------------------------------------------------
# Config -> DuckDB tables
# --------------------------------------------------------------------
def _enr_cfg_id(c) -> str:
    """Enrollment configs are deduplicated across cohorts.

    Two cohorts sharing coverage/gap/chart share one enrollment build.
    In a per-cohort Python loop this saving is not expressible.
    """
    return f"{c.coverage}|{c.enrol_gap}|{int(c.chart_required)}"


def _denom_cfg_id(c) -> str:
    """Cohorts agreeing on this produce IDENTICAL denominators.

    `92_cidadenom.sql` builds one enrolled-member window per cohort per
    enrollment span, filters by demographics, then bands by age. Every
    parameter those steps read is in this key, so two cohorts sharing it
    can share one pass.

    Measured on a real study: 14 cohorts collapse to 5 configs, and the
    stage was 63% of warm runtime. The same trick `_enr_cfg_id` plays
    one level down.

    Derived from what the SQL reads, not guessed — a parameter missing
    here would silently merge two cohorts whose denominators differ.
    """
    demog = "|".join(
        f"{dim}={','.join(sorted(vals))}"
        for dim, vals in (("sex", c.sex), ("race", c.race),
                          ("hispanic", c.hispanic)) if vals)
    strata = ",".join(
        f"{lv.lo}-{lv.hi}-{lv.unit}"
        for lv in (c.age_strata.strata if c.age_strata else ()))
    return "|".join(str(x) for x in (
        _enr_cfg_id(c), c.enr_days, c.min_days_supp, c.min_epis_dur,
        c.req_days_aft_epi, c.req_days_aft_ind, c.blackout_per,
        strata, demog))


def register_config(eng: Engine, study: StudyConfig) -> None:
    cohorts = study.cohorts

    eng.register(
        "cfg_cohort",
        [
            {
                "cohortgrp": c.cohortgrp,
                "enr_cfg_id": _enr_cfg_id(c),
                "enr_days": c.enr_days,
                "wash_per": c.wash_per,
                "point": c.point,
                "episode_gap": c.episode_gap,
                "episode_gap_type": c.episode_gap_type,
                "exp_ext_per": c.exp_ext_per,
                "min_epis_dur": c.min_epis_dur,
                "max_epis_dur": c.max_epis_dur,
                "min_days_supp": c.min_days_supp,
                "at_risk_start": c.at_risk_start,
                "blackout_per": c.blackout_per,
                "fup_wash_per": c.fup_wash_per,
                "event_count": c.event_count,
                "req_days_aft_ind": c.req_days_aft_ind,
                "req_days_aft_epi": c.req_days_aft_epi,
                "censor_death": c.censor_death,
                "cum_dose_per": c.cum_dose_per,
                "min_cum_dose": c.min_cum_dose,
                "max_cum_dose": c.max_cum_dose,
                "min_cfdd": c.min_cfdd,
                "max_cfdd": c.max_cfdd,
            }
            for c in cohorts
        ],
        """cohortgrp VARCHAR, enr_cfg_id VARCHAR, enr_days INTEGER,
           wash_per INTEGER, point BOOLEAN, episode_gap INTEGER,
           episode_gap_type VARCHAR, exp_ext_per INTEGER,
           min_epis_dur INTEGER, max_epis_dur INTEGER,
           min_days_supp INTEGER, at_risk_start INTEGER,
           blackout_per INTEGER, fup_wash_per INTEGER,
           event_count INTEGER, req_days_aft_ind INTEGER,
           req_days_aft_epi INTEGER, censor_death BOOLEAN,
           cum_dose_per INTEGER, min_cum_dose DOUBLE,
           max_cum_dose DOUBLE, min_cfdd DOUBLE, max_cfdd DOUBLE""",
    )

    seen: dict[str, dict] = {}
    for c in cohorts:
        seen.setdefault(
            _enr_cfg_id(c),
            {
                "enr_cfg_id": _enr_cfg_id(c),
                "coverage": c.coverage,
                "enrol_gap": c.enrol_gap,
                "chart_required": c.chart_required,
            },
        )
    eng.register(
        "cfg_enrollment",
        list(seen.values()),
        """enr_cfg_id VARCHAR, coverage VARCHAR,
           enrol_gap INTEGER, chart_required BOOLEAN""",
    )

    # --- denominator config tables -------------------------------
    # DISTINCT per config, not per cohort. Relabelling the per-cohort
    # rows with a config id instead FANS OUT: three cohorts sharing a
    # config give three copies of every filter row, and the join
    # multiplies rather than deduplicates. That version spilled to disk
    # and ran out of space.
    eng.register(
        "cfg_denom_map",
        # Only cohorts SAS would compute a denominator for: OUTPUTDENOM
        # is not "N", and no inclusion rule uses minrxdays > 1.
        [{"denom_cfg_id": _denom_cfg_id(c), "cohortgrp": c.cohortgrp,
          # "M" = members only: DenNumMemDays is blanked
          # (ms_cidadenom.sas:1346). Carried per COHORT because two
          # cohorts can share a denominator config and still differ on
          # whether they report member-days.
          "output_denom": c.output_denom}
         for c in cohorts
         if c.cohortgrp in set(study.denominator_cohorts())],
        "denom_cfg_id VARCHAR, cohortgrp VARCHAR, output_denom VARCHAR",
    )
    eng.register(
        "cfg_denom_cohort",
        list({
            _denom_cfg_id(c): {
                "denom_cfg_id": _denom_cfg_id(c),
                "enr_cfg_id": _enr_cfg_id(c),
                "enr_days": c.enr_days,
                "min_days_supp": c.min_days_supp,
                "min_epis_dur": c.min_epis_dur,
                "req_days_aft_epi": c.req_days_aft_epi,
                "req_days_aft_ind": c.req_days_aft_ind,
                "blackout_per": c.blackout_per,
            } for c in cohorts
        }.values()),
        """denom_cfg_id VARCHAR, enr_cfg_id VARCHAR, enr_days INTEGER,
           min_days_supp INTEGER, min_epis_dur INTEGER,
           req_days_aft_epi INTEGER, req_days_aft_ind INTEGER,
           blackout_per INTEGER""",
    )
    eng.register(
        "cfg_denom_demog",
        list({
            (_denom_cfg_id(c), dim, v): {
                "denom_cfg_id": _denom_cfg_id(c), "dimension": dim,
                "value": v}
            for c in cohorts
            for dim, vals in (("sex", c.sex), ("race", c.race),
                              ("hispanic", c.hispanic))
            for v in vals
        }.values()),
        "denom_cfg_id VARCHAR, dimension VARCHAR, value VARCHAR",
    )
    eng.register(
        "cfg_denom_strata",
        list({
            (_denom_cfg_id(c), lv.ordinal): {
                "denom_cfg_id": _denom_cfg_id(c), "ordinal": lv.ordinal,
                "label": lv.label, "lo": lv.lo, "hi": lv.hi,
                "unit": lv.unit}
            for c in cohorts
            for lv in (c.age_strata.strata if c.age_strata else ())
        }.values()),
        """denom_cfg_id VARCHAR, ordinal INTEGER, label VARCHAR,
           lo INTEGER, hi INTEGER, unit VARCHAR""",
    )

    eng.register(
        "cfg_age_strata",
        [
            {
                "cohortgrp": c.cohortgrp,
                "ordinal": s.ordinal,
                "label": s.label,
                "lo": s.lo,
                "hi": s.hi,
                "unit": s.unit,
            }
            for c in cohorts
            for s in c.age_strata.strata
        ],
        """cohortgrp VARCHAR, ordinal INTEGER, label VARCHAR,
           lo INTEGER, hi INTEGER, unit VARCHAR""",
    )

    eng.register(
        "cfg_demog",
        [
            {"cohortgrp": c.cohortgrp, "dimension": dim, "value": v}
            for c in cohorts
            for dim, vals in (
                ("sex", c.sex),
                ("race", c.race),
                ("hispanic", c.hispanic),
            )
            for v in vals
        ],
        "cohortgrp VARCHAR, dimension VARCHAR, value VARCHAR",
    )

    # One row per USERSTRATA level, with a boolean per stratification
    # variable. Flags rather than a list because the SQL needs them in a
    # CASE per column, and this keeps the level set data rather than
    # generated SQL.
    eng.register(
        "cfg_mfu",
        [
            {"cohortgrp": g, "analysisnum": a, "codecat": cc,
             "countmethod": cm, "topxx": top, "mfufrom": f, "mfuto": t}
            for g, a, cc, cm, top, f, t in study.mfu
        ],
        """cohortgrp VARCHAR, analysisnum INTEGER, codecat VARCHAR,
           countmethod VARCHAR, topxx INTEGER,
           mfufrom INTEGER, mfuto INTEGER""",
    )

    eng.register(
        "cfg_labcodes",
        [
            {"cohortgrp": g, "code": c, "labdatetype": dt,
             "path": path, "resulttyp": rt,
             "ms_test_name": tn, "ms_test_sub_category": tsc,
             "specimen_source": ss, "ms_result_unit": ru,
             "map_result_type": mrt, "fast_ind": fi, "pt_loc": pl,
             "op": op, "bound_lo": lo, "bound_hi": hi}
            for (g, c, dt, path, rt, tn, tsc, ss, ru, mrt, fi, pl,
                 op, lo, hi) in study.lab_codes
        ],
        """cohortgrp VARCHAR, code VARCHAR, labdatetype VARCHAR,
           path VARCHAR, resulttyp VARCHAR,
           ms_test_name VARCHAR, ms_test_sub_category VARCHAR,
           specimen_source VARCHAR, ms_result_unit VARCHAR,
           map_result_type VARCHAR, fast_ind VARCHAR, pt_loc VARCHAR,
           op VARCHAR, bound_lo DOUBLE, bound_hi DOUBLE""",
    )

    eng.register(
        "cfg_utilization",
        [
            {"cohortgrp": g, "utiltype": t, "utilfrom": f, "utilto": to}
            for g, t, f, to in study.utilization
        ],
        """cohortgrp VARCHAR, utiltype VARCHAR,
           utilfrom INTEGER, utilto INTEGER""",
    )

    eng.register(
        "cfg_drugclass",
        [{"code": c, "classname": n} for c, n in study.drug_classes],
        "code VARCHAR, classname VARCHAR",
    )

    eng.register(
        "cfg_zipfile",
        [
            {"zip": z, "statecode": st, "hhs_region": hhs,
             "cb_region": cb, "sdi": sdi}
            for z, st, hhs, cb, sdi in study.zipfile
        ],
        """zip VARCHAR, statecode VARCHAR, hhs_region VARCHAR,
           cb_region VARCHAR, sdi DOUBLE""",
    )

    eng.register(
        "cfg_risk_codes",
        [
            {
                "riskscore": r.riskscore, "condid": r.condid,
                "codecat": r.codecat, "code": r.code, "weight": r.weight,
                "riskfrom": r.riskfrom, "riskto": r.riskto,
                "riskfromanchor": r.riskfromanchor,
                "risktoanchor": r.risktoanchor,
                # Care setting, from RISKSCORECODES.caresettingprincipal.
                # Parsed by parse_care_setting() and previously dropped
                # before it reached the SQL.
                "enctype": r.enctype, "pdx": r.pdx,
                "is_intercept": r.is_intercept,
            }
            for r in study.risk_scores
        ],
        """riskscore VARCHAR, condid VARCHAR, codecat VARCHAR,
           code VARCHAR, weight DOUBLE, riskfrom INTEGER,
           riskto INTEGER, riskfromanchor VARCHAR, risktoanchor VARCHAR,
           enctype VARCHAR, pdx VARCHAR,
           is_intercept BOOLEAN""",
    )

    eng.register(
        "cfg_strata",
        [
            {
                "level_id": lv.level_id or str(i + 1),
                "has_agegroup": "agegroup" in lv.levelvars,
                "has_sex": "sex" in lv.levelvars,
                "has_race": "race" in lv.levelvars,
                "has_hispanic": "hispanic" in lv.levelvars,
                "has_year": "index_year" in lv.levelvars
                            or "year" in lv.levelvars,
                # USERSTRATA levelvars beginning with "covar" stratify
                # the table by a COVARIATE — SAS's `&covarstrat.`
                # (ms_cidatables.sas:403-412). Stored as a space-joined
                # string so one row per level still describes the level
                # completely.
                "covarstrat": " ".join(
                    v for v in lv.levelvars if v.lower().startswith("covar")),
            }
            # Both output tables share the level shape; each is filtered
            # to its own tableid at registration time.
            for i, lv in enumerate(study.cida_levels()
                                   or study.followuptime_levels())
        ],
        """level_id VARCHAR, has_agegroup BOOLEAN, has_sex BOOLEAN,
           has_race BOOLEAN, has_hispanic BOOLEAN, has_year BOOLEAN,
           covarstrat VARCHAR""",
    )

    eng.register(
        "cfg_care_setting",
        [
            {"cohortgrp": c.cohortgrp, "code": code,
             "enctype": enctype, "pdx": pdx}
            for c in cohorts
            for code, enctype, pdx in c.event_care_settings
        ],
        "cohortgrp VARCHAR, code VARCHAR, enctype VARCHAR, pdx VARCHAR",
    )

    eng.register(
        "cfg_code_strength",
        [{"code": c, "strength": v} for c, v in study.code_strength],
        "code VARCHAR, strength DOUBLE",
    )

    eng.register(
        "cfg_inclusion",
        [
            {
                "cohortgrp": r.cohortgrp, "cond": r.cond,
                "subcond": r.subcond,
                "subcond_inclusion": r.subcond_inclusion,
                "condlevel": r.condlevel, "criteria": r.criteria,
                "codecat": r.codecat, "condfrom": r.condfrom,
                "condto": r.condto, "codedays": r.codedays,
                "condfromanchor": r.condfromanchor,
                "condtoanchor": r.condtoanchor,
                "mincumdose": r.mincumdose,
                "minafdd": r.minafdd,
                "maxafdd": r.maxafdd,
                # SAS resets minrxdays to 1 for non-RX codes rather than
                # failing (ms_processinputfiles.sas:33).
                "minrxdays": r.minrxdays if r.codecat == "RX" else 1,
            }
            for r in study.inclusions
        ],
        """cohortgrp VARCHAR, cond INTEGER, subcond INTEGER,
           subcond_inclusion BOOLEAN, condlevel INTEGER,
           criteria VARCHAR, codecat VARCHAR, condfrom INTEGER,
           condto INTEGER, codedays INTEGER, minrxdays INTEGER,
           condfromanchor VARCHAR, condtoanchor VARCHAR,
           mincumdose DOUBLE, minafdd DOUBLE, maxafdd DOUBLE""",
    )

    eng.register(
        "cfg_inclusion_codes",
        [
            # `criteria` is part of the key. SAS numbers cond within
            # (group, conduse), so an INC and an EXC rule can both be
            # cond 1 — without criteria here they would pick up each
            # other's codes, which is exactly what happened on a real
            # study using condlevel 1 for both.
            {"cohortgrp": r.cohortgrp, "criteria": r.criteria,
             "cond": r.cond, "subcond": r.subcond, "code": code}
            for r in study.inclusions
            for code in r.codes
        ],
        """cohortgrp VARCHAR, criteria VARCHAR, cond INTEGER,
           subcond INTEGER, code VARCHAR""",
    )

    eng.register(
        "cfg_covariates",
        [
            {
                "cohortgrp": c.cohortgrp,
                "covarnum": cov.covarnum,
                "covarname": cov.covarname,
                "codecat": cov.codecat,
                "covfrom": cov.covfrom,
                "covto": cov.covto,
                "covfromanchor": cov.covfromanchor,
                "covtoanchor": cov.covtoanchor,
                "dateonly": cov.dateonly,
            }
            for c in cohorts
            for cov in study.covariates
        ],
        """cohortgrp VARCHAR, covarnum INTEGER, covarname VARCHAR,
           codecat VARCHAR, covfrom INTEGER, covto INTEGER,
           covfromanchor VARCHAR, covtoanchor VARCHAR,
           dateonly BOOLEAN""",
    )

    eng.register(
        "cfg_covariate_codes",
        [
            {"covarnum": cov.covarnum, "code": code}
            for cov in study.covariates
            for code in cov.codes
        ],
        "covarnum INTEGER, code VARCHAR",
    )

    eng.register(
        "cfg_codes",
        [
            {"cohortgrp": c.cohortgrp, "role": role, "code": code,
             # codecat decides which claim domain the code is extracted
             # from. A study can define exposure across RX, PX and DX at
             # once; two cohorts in the real file seen are defined purely
             # by HCPCS procedure codes.
             "codecat": codecat,
             # CODESUPPLY: overrides the claim's RxSup when set.
             "code_supply": code_supply,
             "stockgroup": dict(c.exposure_stockgroups).get(code, "_default")
                           if role == "DEF" else "_default"}
            for c in cohorts
            for role, codes in (("DEF", c.exposure_codes),
                                ("EVENT", c.event_codes),
                                ("IOC", c.ioc_codes))
            for code, codecat, code_supply in codes
        ],
        """cohortgrp VARCHAR, role VARCHAR, code VARCHAR,
           codecat VARCHAR, code_supply INTEGER, stockgroup VARCHAR""",
    )


# --------------------------------------------------------------------
# Run
# --------------------------------------------------------------------


def run(
    study: StudyConfig,
    indata: str | Path,
    *,
    engine: Engine | None = None,
    output_dir: str | Path | None = None,
    csv: bool = False,
    layout: str = "split",
    names: str = "sas",
    table_map: dict[str, str] | None = None,
    threads: int | None = None,
    memory_limit: str | None = None,
    verbose: bool = True,
) -> RunResult:
    """Execute the Type 2 pipeline for every cohort in `study`."""
    eng = engine or Engine(
        threads=threads, memory_limit=memory_limit, verbose=verbose
    )
    t0 = time.perf_counter()

    # Emitted here rather than in RunHandle, so the CLI and the UI see
    # the same event stream. Previously only the UI path produced
    # RunStarted/RunFinished, which meant a --log-dir run was missing
    # both the header line and the outcome.
    stages = plan_stages(study)
    eng.plan(stages)
    eng.emit(RunStarted(
        run_id=study.run_id,
        cohorts=tuple(c.cohortgrp for c in study.cohorts),
        stages=tuple(stages),
        indata=str(indata),
        threads=threads if engine is None else eng.threads,
        # The EFFECTIVE limit, not the requested one. `None` means
        # DuckDB's default of 80% of physical RAM, which on a large
        # shared server is a lot to take without recording it.
        memory_limit=eng.effective_memory_limit,
    ))

    register_config(eng, study)

    # Resolve each SCDM table to a concrete read expression. Doing this
    # in Python rather than hardcoding a glob in SQL means the site's
    # directory layout is their choice, not our requirement.
    reads: dict[str, str] = {}
    missing: list[str] = []

    # Schema fingerprinting is the fallback for site-specific table
    # names, but it reads every file's metadata — measured at 0.8-1.6s,
    # which is most of a small run. Compute it only if a name lookup
    # actually fails, and only once.
    _fingerprints: dict[str, list[tuple[str, str]]] | None = None

    def fingerprints() -> dict[str, list[tuple[str, str]]]:
        nonlocal _fingerprints
        if _fingerprints is None:
            _fingerprints = identify_by_columns(indata)
        return _fingerprints

    for spec in SCDM:
        if not spec.used:
            continue
        pattern = resolve_table(indata, spec, table_map=table_map)
        if pattern is None:
            pattern = resolve_table(indata, spec, table_map=table_map,
                                    by_columns=fingerprints())
        if pattern is None:
            if spec.required and not spec.optional_table:
                missing.append(spec.name)
            # death may legitimately be absent; give SQL an empty relation
            reads[f"read_{spec.name}"] = _empty_relation(spec)
            continue
        reads[f"read_{spec.name}"] = f"{reader(pattern)}('{pattern}')"
    if missing:
        detail = []
        for name in missing:
            hits = fingerprints().get(name, [])
            if len(hits) > 1:
                detail.append(
                    f"  {name}: several files have the right columns "
                    f"({', '.join(n for n, _ in hits)}).\n"
                    f"      Pick one with --table-map "
                    f"'{name}=<filename>'."
                )
            else:
                detail.append(f"  {name}: nothing matched by name or by columns.")
        raise FileNotFoundError(
            "Could not identify these SCDM tables under "
            f"{indata}:\n" + "\n".join(detail) + "\n\n"
            "  Tables are found by name, then by their columns. If yours are "
            "named\n  differently and cannot be identified automatically, map "
            "them explicitly:\n"
            "      --table-map enrollment=dp042_elig_2024q1.parquet\n"
            "  Run `qrp inspect --indata <path>` to see what is there."
        )

    # Which column holds the dispensed code. SCDM says `rx`; some
    # extracts use `ndc`. Resolved from the file's actual schema rather
    # than assumed, because guessing wrong fails three stages in.
    disp = reads.get("read_dispensing", "")
    disp_cols = set()
    if disp and not disp.startswith("("):
        disp_cols = {c.lower() for c in columns_of(
            disp.split("('", 1)[1].rsplit("')", 1)[0]
        )}
    code_col = next((c for c in ("rx", "ndc", "rxcode", "code")
                     if c in disp_cols), "rx")

    fmt = {
        **reads,
        "dispensing_code": code_col,
        "indata": str(Path(indata)).rstrip("/"),
        "start_date": study.start_date.isoformat(),
        "end_date": study.end_date.isoformat(),
        "censor_date": study.effective_censor_date.isoformat(),
        # Claims outside the widest possible lookback can never matter.
        # Bounding the scan here is the one place a date filter is
        # applied, and DuckDB pushes it into the parquet reader — which
        # is exactly why getting it wrong is invisible: the rows never
        # enter the pipeline, so no downstream stage can notice they are
        # missing.
        #
        # Computed from the study rather than hardcoded. A two-year
        # constant silently truncated any washout over 730 days and any
        # unbounded covariate lookback. `widest_lookback_days` returns
        # None when some rule is unbounded, in which case no lower bound
        # is applied at all.
        #
        # timedelta, not date.replace(year=...): replace() raises
        # ValueError on 29 February. Both reported in review.
        "claims_from": (
            (study.start_date - timedelta(days=study.widest_lookback_days))
            .isoformat()
            if study.widest_lookback_days is not None
            else date.min.isoformat()
        ),
        "claims_to": study.effective_censor_date.isoformat(),
    }

    # Stages run from STAGES, so `run()` and `plan_stages()` cannot
    # disagree. Stages with side effects (a view to create, a rebuild to
    # trigger) declare them in AFTER_STAGE below rather than being
    # hand-written in sequence here.
    def _stage_params(name: str) -> dict:
        """Extra SQL parameters for a stage, beyond the common `fmt`.

        Only the CIDA table needs them: its covariate-stratum columns
        are generated from USERSTRATA and so cannot be static SQL.
        """
        if name == "cida tables":
            cbase, csel, cgrp, cfin = _covar_strata_sql(study)
            return {**fmt, "covar_base": cbase, "covar_select": csel,
                    "covar_group": cgrp, "covar_final": cfin}
        return fmt

    def _stage(name: str) -> bool:
        """Run the named stage if this study calls for it."""
        st = _STAGE_BY_NAME[name]
        if not st.runs_for(study):
            if verbose and st.skip_note:
                print(f"  {'[ skipped ]':>10} {st.skip_note}")
            return False
        eng.script_stage(st.name, st.script, **_stage_params(name))
        return True

    _stage("normalize")
    # covar_source is a VIEW over the claim domains, used by the
    # covariate, inclusion, risk-score and event-anchored stages. It was
    # created conditionally in two places, which meant adding a fourth
    # consumer broke at runtime. Defining it once, unconditionally,
    # costs nothing (nothing is materialised) and removes the ordering
    # bug entirely.
    eng.con.execute(COVAR_SOURCE_VIEW.format(**fmt))
    _stage("enrollment_spans")
    _stage("exposure + stockpiling")
    # claim_dose is a VIEW over exposure_claims with three consumers: the
    # dose restrictions, the dose censoring, and the per-subcondition
    # dose thresholds on inclusion rules. It used to live inside
    # 42_dose.sql and so existed only when a cohort set a dose limit —
    # adding the third consumer broke at runtime, the same failure
    # covar_source had for the same reason. Defined here, unconditionally,
    # as soon as its source table exists. A view materialises nothing.
    eng.con.execute(
        (Path(__file__).parent / "sql" / "_claim_dose_view.sql")
        .read_text().format(**fmt))

    # An empty exposure set means nothing downstream can produce rows.
    # Say so here, where the cause is still obvious, rather than letting
    # the run finish "successfully" with every output table empty.
    _check_exposure(eng, study, indata)
    _stage("index dates")

    # Conditional stage. The decision is a property over config, so an
    # unused stage costs nothing — not even the probe query the PySpark
    # port ran to find out whether it was needed.
    #
    # ORDER MATTERS: this rewrites index_candidates in place, so it must
    # run before 45_pov1.sql reads it.
    _stage("dose restrictions")

    _stage("pov1")

    _stage("episodes + masterlist")

    # Narrow the claim scan to cohort members, ONCE.
    #
    # covar_source spans every patient in the extract. Four stages read
    # it — covariates, inclusion, risk scores, event-anchored rules —
    # and each join has the episode side and the claim side BOTH growing
    # with the extract, so the work is proportional to their product:
    # doubling the extract quadruples the cost even though the answer
    # scales linearly. Measured at 4x on a production study, the
    # covariates stage went 2.1s -> 33.1s (15.5x) while every other
    # stage scaled ~3.8x.
    #
    # Materialised as a TABLE rather than filtered inline. An inline
    # `WHERE EXISTS` subquery was tried first and was WORSE — it blocks
    # predicate pushdown into the parquet reader, and 1x regressed from
    # 2.14s to 3.31s. Paying one explicit scan and reusing the result is
    # the shape that works.
    #
    # `cohort_claims` is ALWAYS defined — 60_followup.sql reads it
    # unconditionally — but it is only MATERIALISED when the copy can
    # repay itself. Two conditions, both necessary:
    #
    #   1. Something reads it heavily. A study with no covariates, no
    #      inclusion rules and no risk scores touches it once, so the
    #      copy is pure cost. Measured: gating on the share alone made
    #      such a study 3.05s -> 6.02s at 1x and 11.74s -> 26.91s at 4x.
    #   2. The cohort is a minority of the extract. Above that, the copy
    #      is nearly the whole table and saves nothing.
    #
    # Otherwise it is a VIEW, which costs nothing to define.
    heavy_readers = (len(study.covariates) + len(study.inclusions)
                     + len(study.risk_scores))
    materialise = False
    if heavy_readers:
        eng.con.execute("""
            CREATE OR REPLACE TABLE _cohort_patids AS
            SELECT DISTINCT patid FROM ptsmasterlist
        """)
        row = eng.con.execute("""
            SELECT (SELECT count(*) FROM _cohort_patids)::DOUBLE
                 / nullif((SELECT count(DISTINCT patid) FROM demographics), 0)
        """).fetchone()
        # fetchone() is Optional; treat "cannot tell" as "do not copy",
        # since the copy is the expensive choice.
        share = (row[0] if row and row[0] is not None else 1.0)
        materialise = share < 0.5

    if materialise:
        eng.con.execute("""
            CREATE OR REPLACE TABLE cohort_claims AS
            SELECT s.* FROM covar_source s
            SEMI JOIN _cohort_patids c ON c.patid = s.patid
        """)
    else:
        eng.con.execute(
            "CREATE OR REPLACE VIEW cohort_claims AS SELECT * FROM covar_source")
    if heavy_readers:
        eng.con.execute("DROP TABLE _cohort_patids")

    # Inclusion/exclusion criteria, evaluated against the master list —
    # which is where SAS evaluates them (ms_createpov3 is called with
    # _PtsMasterList). The master list carries episodeenddt, so
    # EPISODEENDDT-anchored windows work here and could not before.
    _stage("inclusion criteria")

    # maxcumdose censors the episode as well as excluding index dates.
    # Needs claim_dose from 42_dose.sql, hence the same gate.
    _stage("dose censoring")

    _stage("follow-up + events")
    _stage("attrition + denominators")

    _stage("code distribution")

    # Risk scores need covar_source, defined by the covariates stage or
    # created here when only risk scores need it.
    _stage("most frequent use")

    _stage("labs")

    _stage("utilization")

    _stage("geography")

    _stage("risk scores")

    if study.any_covariates:
        eng.script_stage("covariates", "80_covariates.sql", **fmt)
        # Combo covariates are derived from the ones just detected, so
        # they run after. The expression was parsed at load; only
        # integer covarnums reach the SQL, never study-supplied text.
        if study.any_combo_covariates:
            eng.con.execute(_combo_sql(study))
            # Rebuild prevalence: it is computed inside the covariates
            # stage, which runs BEFORE the combos are inserted, so it
            # would otherwise omit every combo covariate — 17 of 49 in
            # the real study seen. Reported in review.
            eng.con.execute(
                (Path(__file__).parent / "sql"
                 / "_covariate_prevalence.sql").read_text().format(**fmt))

        # The SAS baseline distribution table: wide, one row per group.
        eng.script_stage("baseline", "96_baseline.sql", **fmt)
        widen = _baseline_dummies(eng, study)
        if widen:
            eng.con.execute(widen)

    # The study output table. Only built when USERSTRATA defines t2cida
    # levels, which is SAS's own gate (`where lowcase(tableID)='t2cida'`
    # then `%if %eval(&nobs.>0)`).
    if study.any_cida_tables:
        # Denominators first: the CIDA table merges them onto the
        # numerators, matching SAS's order.
        eng.script_stage("cida denominators", "92_cidadenom.sql", **fmt)
        cbase, csel, cgrp, cfin = _covar_strata_sql(study)
        eng.script_stage("cida tables", "90_cidatables.sql",
                         covar_base=cbase, covar_select=csel,
                         covar_group=cgrp, covar_final=cfin, **fmt)

    # msoc.<runid>_followuptime_cida — requested via USERSTRATA
    # tableid='t2followuptime' (ms_cidanum.sas:2826). Gated separately
    # from t2cida: a study can ask for either, both, or neither.
    _stage("follow-up time table")

    if output_dir:
        out = Path(output_dir)
        tables = tables_for(study)

        if layout == "split":
            _write_split_layout(eng, study, out, tables, csv=csv,
                                names=names)
            if verbose:
                print(f"\n  outputs -> {out}"
                      f"  (dplocal = local only, msoc = shareable)")
            dt = time.perf_counter() - t0
            _finish(eng, study, dt, verbose)
            return RunResult(study=study, engine=eng, seconds=dt)

        for tbl in tables:
            part = ("cohortgrp"
                    if tbl in ("cohort_final", "ptsmasterlist") else None)
            # Unpartitioned tables get a .parquet extension. They were
            # previously written as extensionless files, so `results/`
            # contained a file called `attrition` next to a directory
            # called `cohort_final` — neither obviously openable, and
            # Windows would not know what to do with either.
            target = out / tbl if part else out / f"{tbl}.parquet"
            eng.write_parquet(tbl, target, partition_by=part)
        if csv:
            csv_dir = out / "csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            for tbl in tables:
                eng.write_csv(tbl, csv_dir / f"{tbl}.csv")
        if verbose:
            print(f"\n  outputs -> {out}")

    # `_finish` emits RunFinished and prints the summary. It was
    # duplicated inline here with `tables` rebound from a list[str] to a
    # dict[str, int] — harmless at runtime, but one name holding two
    # types in one function is exactly the confusion a checker exists to
    # catch.
    dt = time.perf_counter() - t0
    _finish(eng, study, dt, verbose)
    return RunResult(study=study, engine=eng, seconds=dt)
