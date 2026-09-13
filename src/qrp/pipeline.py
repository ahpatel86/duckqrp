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
from pathlib import Path

from .config import StudyConfig
from .engine import Engine, sql_str
from .events import EmptyResult, LogMessage, Level, RunFinished, RunStarted
from .scdm import SCDM, columns_of, identify_by_columns, reader, resolve_table

# Stages that produce a table the caller may want on disk.
OUTPUT_TABLES = (
    "cohort_final",
    "ptsmasterlist",
    "attrition",
    "denominators",
    "censoring",
)

OPTIONAL_OUTPUT_TABLES = ("covariates_long", "covariate_prevalence",
                          "baseline")

# Which outputs may leave the Data Partner site.
#
# This is the ONE thing worth taking from the SAS layout, and it is not
# a naming convention — it is a disclosure boundary:
#
#   dplocal  patient-level, stays behind the DP firewall
#   msoc     aggregate, returned to the Sentinel Operations Center
#
# A flat directory collapses it, putting patient-level output next to
# aggregate output with nothing to distinguish them, which makes "may
# this file leave the site" a thing a human has to remember per file.
#
# File naming is controlled by --names:
#
#   sas      (default) reproduce the SAS QRP dataset names, so outputs
#            drop into existing SOPs, parity tooling and review habits
#            unchanged. `<runid>_mstr`, `<runid>_denomcounts`, ...
#   logical  this pipeline's own table names, which are what `qrp show`
#            and the documentation call them.
#
# Both write the same data to the same two libraries, and both write a
# manifest, so `qrp show` and any downstream tool look tables up by
# logical name either way — the convention never reaches a reader.
#
# Outputs whose SAS name this package reproduces EXACTLY. msoc datasets
# go to the Operations Center, where tooling matches on dataset name, so
# a plausible-looking rename is a breakage rather than a cosmetic
# difference. Anything not listed here is either named differently in
# SAS (see SAS_NAME_NOTES) or is an addition this package makes.
SAS_CONTRACT: frozenset[str] = frozenset({
    "attrition", "censoring", "distindex", "distindexmap",
    "runtimes", "signature", "t2_cida", "followuptime",
    "cohort_final", "denomcounts", "numcounts",
})

# Where this package cannot reproduce the SAS name exactly, and why.
# Recorded in the manifest so a DP sending msoc onward can see it
# rather than discover it downstream.
SAS_NAME_NOTES: dict[str, str] = {
    "covariate_prevalence":
        "Not a SAS output. SAS's baseline table is wide and squared "
        "(one row per group, one column per category level); this is "
        "one row per covariate. Kept because the long form is easier "
        "to read when checking a single covariate definition.",
    "baseline":
        "SAS: <runid>_baseline<outcohort>_<i>. The _<i> suffix is a "
        "surveillance period index; this package does not model "
        "surveillance periods, so there is no value to supply. The "
        "shape and columns match.",
    "mfu":
        "SAS: <runid>_baseline_<mfu>_<i>. Same period-index caveat.",
    "lab_summary":
        "Not a SAS output. An aggregate summary this package adds.",
    "risk_score_summary":
        "Not a SAS output. An aggregate summary this package adds.",
    "utilization_summary":
        "Not a SAS output. An aggregate summary this package adds.",
    "risk_scores":
        "Not a SAS output. ms_computeriskscores attaches the score to "
        "the master list; there is no dplocal.<runid>_risk_scores. The "
        "&RUNID._RISKDIFFDATA_ datasets are a different analysis "
        "(risk differences), not comorbidity scores.",
    "risk_score_summary":
        "Not a SAS output. An aggregate summary this package adds.",
    "covariates_long":
        "Not a SAS output. SAS holds covariate detection in work "
        "datasets and emits only the derived baseline table; there is "
        "no dplocal.<runid>_covariates. Kept because the long form is "
        "what the CIDA and baseline stages read, and it is useful for "
        "checking a covariate definition.",
    "inclusion_excluded":
        "Not a SAS output. There is no dplocal.<runid>_inclexcl; SAS "
        "records exclusions in the attrition table. Kept because it "
        "names WHICH condition excluded each episode, which attrition "
        "counts but does not identify.",
    "ptsmasterlist":
        "Not a SAS output. SAS has one master list and it is the "
        "FINALISED one (emitted here as cohort_final -> <runid>_mstr). "
        "This is the pre-follow-up intermediate, kept for debugging "
        "attrition.",
    "geography":
        "Not a SAS output. SAS carries zip3/state/hhs_reg/cb_reg/"
        "zip_uncertain as COLUMNS ON mstr (ms_geographicvars.sas:158) "
        "and has no &RUNID._geography dataset. Those columns are now on "
        "mstr; this standalone table is kept as a convenience.",
    "denominators":
        "Not a SAS output under this name. SAS has exactly one "
        "<runid>_denomcounts, which is ms_cidadenom's and is emitted as "
        "`denomcounts`; this is a separate per-stratum aggregate.",
}

SAS_NAMES: dict[str, str] = {
    # SAS has ONE master list. `DPLocal.&RUNID._mstr` is set from
    # `_PtsMasterList` AFTER ms_finalizeptsmasterlist has attached the
    # event and censoring columns (ms_createmicohorts.sas:2117), so
    # SAS's mstr is the FINALISED list — this package's `cohort_final`.
    #
    # There is no `&RUNID._mstr_final` anywhere in the macro library.
    # Mapping the pre-follow-up intermediate to `mstr` meant a DP
    # reading `<runid>_mstr` got 6,687 extra episodes that had not been
    # through the follow-up washout, and no eventdt/has_event columns.
    "cohort_final":         "mstr",
    # The pre-follow-up list is an intermediate SAS does not emit. Kept
    # because it is useful for debugging attrition, under a name that
    # does not claim to be a SAS output.
    "ptsmasterlist":        "mstr_episodes",
    "covariates_long":      "covariates",
    # NOT "denomcounts". SAS has exactly one &RUNID._denomcounts and it
    # is ms_cidadenom's output, which this package emits as the
    # `denomcounts` table. `denominators` is a separate per-stratum
    # aggregate from 70_outputs.sql; mapping both to the same SAS name
    # meant the second write silently overwrote the first and one of the
    # two outputs simply vanished. Reported in review.
    "denominators":         "denomstrata",
    # NOT "baseline". SAS's baseline is a WIDE, squared, one-row-per-
    # group table (ms_createdistbaselinetable.sas:524); this is one row
    # per covariate. A different table, not a renaming.
    "covariate_prevalence": "covariate_prevalence",
    "inclusion_excluded":   "inclexcl",
    "attrition":            "attrition",
    # SAS calls this censor_cida (ms_createcensortable.sas:22, 200).
    # The msoc outputs go to the Operations Center, where downstream
    # tooling matches on dataset name, so a plausible-looking rename is
    # a breakage rather than a cosmetic difference.
    "censoring":            "censor_cida",
    "followuptime":         "followuptime_cida",
    "signature":            "signature",
    "runtimes":             "runtimes",
}

DISCLOSURE: dict[str, str] = {
    "ptsmasterlist":        "dplocal",   # one row per patient-episode
    "cohort_final":         "dplocal",   # one row per patient-episode
    "covariates_long":      "dplocal",   # one row per patient-covariate
    "inclusion_excluded":   "dplocal",   # one row per excluded episode
    "denominators":         "dplocal",   # small cells can be identifying
    "attrition":            "msoc",      # counts per step
    "censoring":            "msoc",      # counts per exit reason
    "covariate_prevalence": "msoc",      # counts per covariate
    "baseline":             "msoc",      # the SAS distribution table
    "t2_cida":              "msoc",
    "followuptime":         "msoc",      # aggregate, per SAS      # the study output table
    "denomcounts":          "dplocal",   # SAS puts this in dplocal
    "numcounts":            "dplocal",   # numerator detail, per SAS
    "distindex":            "msoc",      # counts per code combination
    "distindexmap":         "msoc",      # code -> ID map
    "mfu":                  "msoc",      # aggregate code counts
    "lab_results":          "dplocal",   # patient-level results
    "lab_summary":          "msoc",      # distribution only
    "utilization":          "dplocal",   # one row per patient-episode
    "utilization_summary":  "msoc",      # distribution only
    "geography":            "dplocal",   # zip-level, identifying
    "risk_scores":          "dplocal",   # one row per patient-episode
    "risk_score_summary":   "msoc",      # distribution only
    "signature":            "msoc",      # run provenance
    "runtimes":             "msoc",      # per-stage timings
}


# covar_source is the union of claim domains that both the inclusion and
# covariate stages match against. Defined here because either stage can
# be the first to need it, and it must not be created twice.
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


def _run_metadata(eng: Engine, study: StudyConfig, seconds: float) -> None:
    """Build the SAS `_signature` and `_runtimes` tables.

    Both are real SAS QRP outputs and both are MSOC — they are the
    provenance that travels with the results. The data already exists in
    the event log; this just materialises it in the expected shape.
    """
    import platform
    import sys
    from datetime import date, datetime

    import duckdb as _ddb

    eng.con.execute("""
        CREATE OR REPLACE TABLE signature (
            runid VARCHAR, study_type INTEGER, run_datetime TIMESTAMP,
            start_date DATE, end_date DATE, censor_date DATE,
            n_cohorts INTEGER, n_covariates INTEGER, n_inclusion_rules INTEGER,
            engine VARCHAR, engine_version VARCHAR,
            python_version VARCHAR, platform VARCHAR, wall_seconds DOUBLE,
            -- The EFFECTIVE limit and thread count, not what was asked
            -- for. Omitting memory_limit means DuckDB's own default of
            -- 80% of physical RAM: 3.1 GiB on a 4 GB box, ~102 GB on a
            -- 128 GB server. A run record that does not say which is
            -- not a record of what the job took.
            memory_limit VARCHAR, threads INTEGER
        )""")
    eng.con.execute(
        "INSERT INTO signature VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [study.run_id, study.study_type, datetime.now(),
         study.start_date, study.end_date, study.effective_censor_date,
         len(study.cohorts), len(study.covariates), len(study.inclusions),
         "duckdb", _ddb.__version__, sys.version.split()[0],
         platform.platform(), round(seconds, 3),
         eng.effective_memory_limit, eng.effective_threads],
    )

    eng.con.execute("""
        CREATE OR REPLACE TABLE runtimes (
            runid VARCHAR, step INTEGER, process VARCHAR,
            seconds DOUBLE, rows BIGINT
        )""")
    eng.con.executemany(
        "INSERT INTO runtimes VALUES (?,?,?,?,?)",
        [[study.run_id, i, r.name, round(r.seconds, 3), r.rows]
         for i, r in enumerate(eng.log, start=1)],
    )


def _write_split_layout(eng: Engine, study: StudyConfig, out: Path,
                        tables: list[str], csv: bool = False,
                        names: str = "sas") -> None:
    """Write outputs under dplocal/ and msoc/.

    `names="sas"` reproduces the SAS QRP dataset names; `"logical"` uses
    this pipeline's own. The manifest maps logical -> file either way,
    so readers never have to know which was used.
    """
    _run_metadata(eng, study, sum(r.seconds for r in eng.log))
    run = study.run_id.lower()

    # Clear THIS run's previous outputs before writing.
    #
    # Without it, a rerun into a populated directory leaves stale files
    # that the current run did not produce, and nothing marks them as
    # stale. Two ways that bites, both verified:
    #
    #   * Rerunning a study with a feature REMOVED — drop the
    #     covariates, and covariates / baseline / covariate_prevalence
    #     survive from the previous run. A reader gets covariate results
    #     for a study that defines no covariates.
    #   * Switching --names between `sas` and `logical` — the same table
    #     is written under both names, so censor_cida and censoring sit
    #     side by side with no indication they are one table.
    #
    # Scoped to this run_id's prefix, not the whole directory: a data
    # partner may legitimately keep several runs' outputs together, and
    # wiping a sibling run's results would be worse than the staleness
    # this fixes.
    for lib in ("dplocal", "msoc"):
        d = out / lib
        if not d.is_dir():
            continue
        for existing in d.glob(f"{run}_*"):
            if existing.is_dir():
                _shutil.rmtree(existing)
            else:
                existing.unlink()

    for tbl in [*tables, "signature", "runtimes"]:
        # Default to dplocal for anything unmapped: a new output should
        # have to be declared shareable, never become so by omission.
        lib = DISCLOSURE.get(tbl, "dplocal")
        try:
            eng.count(tbl)
        except Exception:
            continue
        target_dir = out / lib
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = SAS_NAMES.get(tbl, tbl) if names == "sas" else tbl
        name = f"{run}_{suffix}"
        part = "cohortgrp" if tbl in ("cohort_final", "ptsmasterlist") else None
        dest = target_dir / name if part else target_dir / f"{name}.parquet"
        eng.write_parquet(tbl, dest, partition_by=part)
        if csv:
            csv_dir = out / lib / "csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            eng.write_csv(tbl, csv_dir / f"{name}.csv")

    # A manifest, so `qrp show` and any downstream tool can find tables by
    # their logical name without having to parse the SAS convention.
    import json as _json

    manifest = {
        "runid": study.run_id,
        "layout": "split",
        "names": names,
        "tables": {
            t: {
                "library": DISCLOSURE.get(t, "dplocal"),
                "file": f"{run}_"
                        f"{SAS_NAMES.get(t, t) if names == 'sas' else t}",
                # Whether this file's name matches the SAS output it
                # corresponds to. False means either the SAS name
                # carries a suffix this package cannot supply, or the
                # output is an addition — `note` says which.
                "sas_contract": t in SAS_CONTRACT,
                **({"note": SAS_NAME_NOTES[t]} if t in SAS_NAME_NOTES
                   else {}),
            }
            for t in [*tables, "signature", "runtimes"]
        },
    }
    # Written LAST, deliberately. The write sequence is
    # clear -> tables -> manifest, so a failure partway through leaves
    # the tables without a manifest. Its PRESENCE is therefore the
    # signal that a run completed and its outputs are the full set.
    #
    # A failure during the write loop does lose the previous run's
    # outputs, since they were cleared first. The alternative — writing
    # to a temp tree and swapping — doubles peak disk, and disk is
    # already the binding constraint at scale (the pipeline spills
    # roughly 2.6x the input size at its memory floor), so the cure
    # would risk causing the disease. Re-running regenerates the
    # outputs; a half-full disk does not.
    (out / "manifest.json").write_text(_json.dumps(manifest, indent=1))


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
    for column, prefix in (("sex", "Sex"), ("race", "Race"),
                           ("hispanic", "Hispanic"), ("agegroup", "Age")):
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

    for cov in study.covariates:
        dummies.append(
            f"sum(CASE WHEN EXISTS (SELECT 1 FROM covariates_long x "
            f"WHERE x.cohortgrp = c.cohortgrp AND x.patid = c.patid "
            f"AND x.indexdt = c.indexdt AND x.covarnum = {int(cov.covarnum)})"
            f" THEN 1 ELSE 0 END) AS \"covar{int(cov.covarnum)}\""
        )

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


def plan_stages(study: StudyConfig) -> list[str]:
    """Stage names in order.

    Single source of truth: a progress bar needs the total before the
    first stage starts, and the log needs it to write [3/10] rather than
    [3/0]. Both the CLI and the UI read it from here, so the two paths
    cannot drift.
    """
    stages = ["normalize", "enrollment_spans", "exposure + stockpiling",
              "index dates"]
    if study.any_dose:
        stages.append("dose restrictions")
    stages.append("pov1")
    stages.append("episodes + masterlist")
    if study.any_inclusions:
        stages.append("inclusion criteria")
    if study.any_dose_censoring:
        stages.append("dose censoring")
    stages += ["follow-up + events", "attrition + denominators"]
    if study.any_code_distribution:
        stages.append("code distribution")
    if study.any_mfu:
        stages.append("most frequent use")
    if study.any_labs:
        stages.append("labs")
    if study.any_utilization:
        stages.append("utilization")
    if study.any_geography:
        stages.append("geography")
    if study.any_risk_scores:
        stages.append("risk scores")
    if study.any_covariates:
        stages.append("covariates")
        # The SAS baseline distribution table runs with the covariates,
        # since its dummy columns come from covariates_long.
        stages.append("baseline")
    if study.any_cida_tables:
        stages += ["cida denominators", "cida tables"]
    if study.any_followuptime:
        stages.append("follow-up time table")
    return stages


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
            }
            # Both output tables share the level shape; each is filtered
            # to its own tableid at registration time.
            for i, lv in enumerate(study.cida_levels()
                                   or study.followuptime_levels())
        ],
        """level_id VARCHAR, has_agegroup BOOLEAN, has_sex BOOLEAN,
           has_race BOOLEAN, has_hispanic BOOLEAN, has_year BOOLEAN""",
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

    eng.script_stage("normalize", "10_normalize.sql", **fmt)
    # covar_source is a VIEW over the claim domains, used by the
    # covariate, inclusion, risk-score and event-anchored stages. It was
    # created conditionally in two places, which meant adding a fourth
    # consumer broke at runtime. Defining it once, unconditionally,
    # costs nothing (nothing is materialised) and removes the ordering
    # bug entirely.
    eng.con.execute(COVAR_SOURCE_VIEW.format(**fmt))
    eng.script_stage("enrollment_spans", "20_enrollment.sql", **fmt)
    eng.script_stage("exposure + stockpiling", "30_exposure.sql", **fmt)
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
    eng.script_stage("index dates", "40_index.sql", **fmt)

    # Conditional stage. The decision is a property over config, so an
    # unused stage costs nothing — not even the probe query the PySpark
    # port ran to find out whether it was needed.
    #
    # ORDER MATTERS: this rewrites index_candidates in place, so it must
    # run before 45_pov1.sql reads it.
    if study.any_dose:
        eng.script_stage("dose restrictions", "42_dose.sql", **fmt)
    elif verbose:
        print(f"  {'[ skipped ]':>10} dose restrictions "
              f"(no cohort sets a dose limit)")

    eng.script_stage("pov1", "45_pov1.sql", **fmt)

    eng.script_stage("episodes + masterlist", "50_episodes.sql", **fmt)

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
    if study.any_inclusions:
        eng.script_stage("inclusion criteria", "52_inclusion.sql", **fmt)

    # maxcumdose censors the episode as well as excluding index dates.
    # Needs claim_dose from 42_dose.sql, hence the same gate.
    if study.any_dose_censoring:
        eng.script_stage("dose censoring", "55_dose_censor.sql", **fmt)

    eng.script_stage("follow-up + events", "60_followup.sql", **fmt)
    eng.script_stage("attrition + denominators", "70_outputs.sql", **fmt)

    if study.any_code_distribution:
        eng.script_stage("code distribution", "72_codedistribution.sql",
                         **fmt)

    # Risk scores need covar_source, defined by the covariates stage or
    # created here when only risk scores need it.
    if study.any_mfu:
        eng.script_stage("most frequent use", "78_mfu.sql", **fmt)

    if study.any_labs:
        eng.script_stage("labs", "76_labs.sql", **fmt)

    if study.any_utilization:
        eng.script_stage("utilization", "74_utilization.sql", **fmt)

    if study.any_geography:
        eng.script_stage("geography", "47_geography.sql", **fmt)

    if study.any_risk_scores:
        eng.script_stage("risk scores", "85_riskscores.sql", **fmt)

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
        eng.script_stage("cida tables", "90_cidatables.sql", **fmt)
    elif verbose:
        print(f"  {'[ skipped ]':>10} covariates (none defined)")

    # msoc.<runid>_followuptime_cida — requested via USERSTRATA
    # tableid='t2followuptime' (ms_cidanum.sas:2826). Gated separately
    # from t2cida: a study can ask for either, both, or neither.
    if study.any_followuptime:
        eng.script_stage("follow-up time table", "94_followuptime.sql",
                         **fmt)

    if output_dir:
        out = Path(output_dir)
        tables = list(OUTPUT_TABLES)
        if study.any_covariates:
            tables += list(OPTIONAL_OUTPUT_TABLES)
        if study.any_inclusions:
            tables.append("inclusion_excluded")
        if study.any_cida_tables:
            tables += ["t2_cida", "denomcounts", "numcounts"]
        if study.any_followuptime:
            tables.append("followuptime")
        if study.any_risk_scores:
            tables += ["risk_scores", "risk_score_summary"]
        if study.any_geography:
            tables.append("geography")
        if study.any_code_distribution:
            tables += ["distindex", "distindexmap"]
        if study.any_utilization:
            tables += ["utilization", "utilization_summary"]
        if study.any_labs:
            tables += ["lab_results", "lab_summary"]
        if study.any_mfu:
            tables.append("mfu")

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
