"""Output naming, disclosure routing and writing.

Separated from the pipeline because they answer a different question.
The pipeline decides WHAT to compute; this decides what each result is
called, which library it may go to, and how it reaches disk.

Keeping them together made `pipeline.py` 1,500 lines and buried the
output contract inside orchestration code, where a mismatch between a
SAS name and the table it pointed at was not visible in any one place.
That is how `mstr` came to name the wrong table.
"""
from __future__ import annotations

import json as _json
import platform
import shutil as _shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import StudyConfig
from .engine import Engine


@dataclass(frozen=True)
class Output:
    """One output table: what it is called, where it goes, and whether
    that name is a SAS contract.

    THE declaration. `OUTPUT_TABLES`, `OPTIONAL_OUTPUT_TABLES`,
    `SAS_NAMES`, `DISCLOSURE`, `SAS_CONTRACT` and `SAS_NAME_NOTES` are
    all derived from it below.

    They used to be six independent dicts over the same 25 tables, with
    tests holding them in agreement. That is what produced the two worst
    output bugs found in review: `mstr` named the wrong table, because
    nothing tied the SAS name to the table that actually holds the
    finalised cohort; and `geography` claimed a SAS name that does not
    exist anywhere in the macro library, because `SAS_CONTRACT` was
    populated from what this package emitted rather than from what SAS
    does.

    One entry per output makes those mismatches unrepresentable rather
    than tested for.

    name      the table in DuckDB
    sas_name  the SAS dataset name, minus the <runid>_ prefix
    library   "msoc" (shareable aggregate) or "dplocal" (stays at site)
    contract  True when sas_name is a dataset SAS really produces
    emit      when this output is written:
                "always"    every study
                "covariate" only when the study defines covariates
                "gated"     the stage that builds it decides, and
                            run() adds it to the list explicitly
    note      why it is NOT a contract output; required when contract
              is False, so an addition has to explain itself
    """
    name: str
    sas_name: str
    library: str
    contract: bool = False
    emit: str = "gated"
    note: str = ""

    def __post_init__(self) -> None:
        if not self.contract and not self.note:
            raise ValueError(
                f"output {self.name!r} is not a SAS contract output and "
                f"has no note explaining why — an addition must say what "
                f"it is, or a reader cannot tell it from an omission")


OUTPUTS: tuple[Output, ...] = (
    Output("attrition", "attrition", "msoc", contract=True, emit="always"),
    Output("baseline", "baseline", "msoc", emit="covariate",
           note="SAS: <runid>_baseline<outcohort>_<i>. The _<i> suffix is a"
                "surveillance period index; this package does not model"
                "surveillance periods, so there is no value to supply. The"
                "shape and columns match."),
    Output("censoring", "censor_cida", "msoc", contract=True, emit="always"),
    Output("cohort_final", "mstr", "dplocal", contract=True, emit="always"),
    Output("covariate_prevalence", "covariate_prevalence", "msoc", emit="covariate",
           note="Not a SAS output. SAS's baseline table is wide and squared"
                "(one row per group, one column per category level); this"
                "is one row per covariate. Kept because the long form is"
                "easier to read when checking a single covariate"
                "definition."),
    Output("covariates_long", "covariates", "dplocal", emit="covariate",
           note="Not a SAS output. SAS holds covariate detection in work"
                "datasets and emits only the derived baseline table; there"
                "is no dplocal.<runid>_covariates. Kept because the long"
                "form is what the CIDA and baseline stages read, and it is"
                "useful for checking a covariate definition."),
    Output("denomcounts", "denomcounts", "dplocal", contract=True),
    Output("denominators", "denomstrata", "dplocal", emit="always",
           note="Not a SAS output under this name. SAS has exactly one"
                "<runid>_denomcounts, which is ms_cidadenom's and is"
                "emitted as `denomcounts`; this is a separate per-stratum"
                "aggregate."),
    Output("distindex", "distindex", "msoc", contract=True),
    Output("distindexmap", "distindexmap", "msoc", contract=True),
    Output("followuptime", "followuptime_cida", "msoc", contract=True),
    Output("geography", "geography", "dplocal",
           note="Not a SAS output. SAS carries"
                "zip3/state/hhs_reg/cb_reg/zip_uncertain as COLUMNS ON mstr"
                "(ms_geographicvars.sas:158) and has no &RUNID._geography"
                "dataset. Those columns are now on mstr; this standalone"
                "table is kept as a convenience."),
    Output("inclusion_excluded", "inclexcl", "dplocal",
           note="Not a SAS output. There is no dplocal.<runid>_inclexcl;"
                "SAS records exclusions in the attrition table. Kept"
                "because it names WHICH condition excluded each episode,"
                "which attrition counts but does not identify."),
    Output("lab_results", "lab_results", "dplocal",
           note="SAS has DPLocal.<runid>_Claims_lab, which is probably "
                "the counterpart, but its columns have NOT been "
                "compared — claiming a SAS name without checking the "
                "shape is what produced the mstr and geography bugs. "
                "Treated as an addition until verified."),
    Output("lab_summary", "lab_summary", "msoc",
           note="Not a SAS output. An aggregate summary this package adds."),
    Output("mfu", "mfu", "msoc",
           note="SAS: <runid>_baseline_<mfu>_<i>. Same period-index caveat."),
    Output("numcounts", "numcounts", "dplocal", contract=True),
    Output("ptsmasterlist", "mstr_episodes", "dplocal", emit="always",
           note="Not a SAS output. SAS has one master list and it is the"
                "FINALISED one (emitted here as cohort_final ->"
                "<runid>_mstr). This is the pre-follow-up intermediate,"
                "kept for debugging attrition."),
    Output("risk_score_summary", "risk_score_summary", "msoc",
           note="Not a SAS output. An aggregate summary this package adds."),
    Output("risk_scores", "risk_scores", "dplocal",
           note="Not a SAS output. ms_computeriskscores attaches the score"
                "to the master list; there is no"
                "dplocal.<runid>_risk_scores. The &RUNID._RISKDIFFDATA_"
                "datasets are a different analysis (risk differences), not"
                "comorbidity scores."),
    Output("runtimes", "runtimes", "msoc", contract=True),
    Output("signature", "signature", "msoc", contract=True),
    Output("t2_cida", "t2_cida", "msoc", contract=True),
    Output("utilization", "utilization", "dplocal",
           note="Not a SAS output. There is no utilization dataset in "
                "the macro library; SAS feeds these counts straight "
                "into the baseline table. Kept because the per-episode "
                "detail is useful for checking a baseline figure."),
    Output("utilization_summary", "utilization_summary", "msoc",
           note="Not a SAS output. An aggregate summary this package adds."),
)

# ---- derived views over OUTPUTS -------------------------------------
# Kept as module-level names because the rest of the package and the
# tests read them. They are projections now, not declarations.
OUTPUT_TABLES: tuple[str, ...] = tuple(
    o.name for o in OUTPUTS if o.emit == "always")
# NOT "everything that is not always". These are specifically the
# COVARIATE-gated outputs: run() appends them when a study defines
# covariates. Treating the two as complements would have made every
# covariate study try to write all twenty optional tables.
OPTIONAL_OUTPUT_TABLES: tuple[str, ...] = tuple(
    o.name for o in OUTPUTS if o.emit == "covariate")
SAS_NAMES: dict[str, str] = {
    o.name: o.sas_name for o in OUTPUTS if o.sas_name != o.name}
DISCLOSURE: dict[str, str] = {o.name: o.library for o in OUTPUTS}
SAS_CONTRACT: frozenset[str] = frozenset(
    o.name for o in OUTPUTS if o.contract)
SAS_NAME_NOTES: dict[str, str] = {
    o.name: o.note for o in OUTPUTS if o.note}



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

# Where this package cannot reproduce the SAS name exactly, and why.
# Recorded in the manifest so a DP sending msoc onward can see it
# rather than discover it downstream.




# covar_source is the union of claim domains that both the inclusion and
# covariate stages match against. Defined here because either stage can
# be the first to need it, and it must not be created twice.



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
                        text: bool = True,
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
    # Both the library directory AND its csv/ subdirectory. `--csv`
    # writes copies under {lib}/csv/, which the library-level glob
    # never reached: a rerun that switched naming mode left
    # `<run>_censor_cida.csv` sitting beside the current
    # `<run>_censoring.csv`, where it reads as a second result rather
    # than a leftover.
    for lib in ("dplocal", "msoc"):
        for d in (out / lib, out / lib / "csv"):
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
        # A plain-text view of every msoc table, written just before its
        # parquet, so an output can be read without a parquet viewer —
        # which most of the people reading these do not have.
        #
        # msoc ONLY. Those are aggregates, cleared for sharing, and the
        # text copy changes nothing about what leaves the site. dplocal
        # is patient-level; a plain-text copy of it would be one more
        # unencrypted, greppable file holding patient rows.
        if text and lib == "msoc":
            from .show import table_text
            (target_dir / f"{name}.txt").write_text(
                table_text(eng.con, tbl, name))
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


def tables_for(study: StudyConfig, debug: bool = False) -> list[str]:
    """Which outputs this study writes.

    The gating lives here rather than inside `run()` because it is an
    output question, not an orchestration one: it says which results
    exist for this study shape, and `OUTPUTS` is the declaration it
    reads from.
    """
    tables = list(OUTPUT_TABLES)
    if study.any_covariates:
        tables += list(OPTIONAL_OUTPUT_TABLES)
    if study.any_inclusions:
        tables.append("inclusion_excluded")
    if study.any_cida_tables:
        tables += ["t2_cida", "denomcounts", "numcounts"]
    if study.any_followuptime:
        tables.append("followuptime")
    if study.any_code_distribution:
        tables += ["distindex", "distindexmap"]
    if study.any_geography:
        tables.append("geography")
    if study.any_labs:
        tables += ["lab_results", "lab_summary"]
    if study.any_mfu:
        tables.append("mfu")
    if study.any_risk_scores:
        tables += ["risk_scores", "risk_score_summary"]
    if study.any_utilization:
        tables += ["utilization", "utilization_summary"]

    # Mirrors SAS's QRP_DEBUG. Without it, write only what SAS writes
    # to dplocal: the contract outputs (mstr, denomcounts, numcounts).
    # The patient-level ADDITIONS — covariates_long, inclusion_excluded,
    # mstr_episodes and the rest — are diagnostics, the same role SAS's
    # per-step exclusion lists play under QRP_DEBUG
    # (ms_attrition.sas:201-209).
    #
    # Scoped to dplocal deliberately. msoc additions are small
    # aggregates a data partner may be asked for; dplocal additions are
    # patient-level files that sit at the site, cost disk, and were
    # never part of the request.
    if not debug:
        by_name = {o.name: o for o in OUTPUTS}
        tables = [t for t in tables
                  if not (t in by_name
                          and by_name[t].library == "dplocal"
                          and not by_name[t].contract)]
    return tables
