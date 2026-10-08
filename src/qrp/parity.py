"""
Parity dump: emit stage tables in the layout the existing SAS↔PySpark
harness already expects.

The parity harness is the most valuable asset in the current repository,
and it is engine-agnostic — it compares dumped files, not DataFrames. So
this implementation plugs into it unchanged, which is what makes a
DuckDB bake-off cheap to evaluate rather than an act of faith.

Layout (unchanged from tools/parity/parity_scaffold.py):
    <dplocal>/_dbg_<stage>/<pt>/<cohortgrp>/iter_<NN>/<table>/

Dumping is opt-in and costs nothing when off — unlike the ~540 lines of
QRP_DUMP_* scaffolding interleaved through the PySpark main.py, this is
one function called at the end of a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .engine import Engine

# Stage name -> tables to dump. Mirrors tests/parity/manifest.STAGES so
# the existing comparison manifest keeps working.
STAGE_TABLES: dict[str, tuple[str, ...]] = {
    # Intermediate stages: where a divergence is easiest to localise.
    "STOCKPILING": ("stockpiled",),
    "POV1": ("index_candidates", "pov1"),
    "PTSMASTERLIST": ("ptsmasterlist",),
    "POV56": ("cohort_final",),
    # Deliverables. These were missing, and they are the tables a
    # sweep against the SAS macros found to be wrong in EVERY case —
    # wrong shape, wrong column names, missing columns, and in one case
    # (mstr) the wrong table entirely. Comparing only the intermediates
    # would have caught none of it.
    "ATTRITION": ("attrition",),
    "CENSOR": ("censoring",),
    "CIDA": ("t2_cida", "numcounts", "denomcounts"),
    "CODEDIST": ("distindex", "distindexmap"),
    "FOLLOWUPTIME": ("followuptime",),
}

# Columns excluded from parity comparison: derived diagnostics that have
# no SAS counterpart. Declared here rather than discovered at diff time.
IGNORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "stockpiled": ("orig_adate",),
    # cohort_final carries BOTH this package's own column names and the
    # SAS ones (group/FEventDt/Event/followuptime/...). The internal
    # names have no SAS counterpart, so comparing them would report a
    # difference for every row of every run.
    "cohort_final": ("exit_reason", "cohortgrp", "eventdt", "has_event",
                     "episode_days", "person_days", "episode",
                     "episodestartdt", "episode_rxsup", "dataavail_dt",
                     "atriskindexdt"),
    # attrition carries EXACTLY the SAS columns now, so nothing to
    # exclude. The extras it used to append were removed as
    # non-contract.
}


@dataclass
class ParityDumper:
    dplocal: Path
    pt: str = "pt001"
    iteration: str = "00"
    fmt: str = "csv"          # csv matches SAS PROC EXPORT; parquet is faster

    def dump(self, eng: Engine, stages: tuple[str, ...] | None = None) -> list[Path]:
        written: list[Path] = []
        for stage in stages or tuple(STAGE_TABLES):
            for table in STAGE_TABLES.get(stage, ()):
                # Optional stages produce no table. Dumping what exists
                # and skipping the rest is the whole point: a parity run
                # compares the tables BOTH sides produced. Raising here
                # meant the dump died on any study without USERSTRATA —
                # so the first thing a data partner tried would fail,
                # and the comparison they were setting up never ran.
                try:
                    eng.count(table)
                except Exception:
                    continue
                written += self._dump_table(eng, stage, table)
        return written

    def _cohorts(self, eng: Engine, table: str) -> list[str]:
        cols = [
            r[0] for r in eng.con.execute(
                f"SELECT column_name FROM duckdb_columns() "
                f"WHERE table_name = '{table}'"
            ).fetchall()
        ]
        if "cohortgrp" not in cols:
            return ["all"]
        return [
            r[0] for r in eng.con.execute(
                f"SELECT DISTINCT cohortgrp FROM {table} ORDER BY 1"
            ).fetchall()
        ]

    def _dump_table(self, eng: Engine, stage: str, table: str) -> list[Path]:
        out: list[Path] = []
        drop = IGNORE_COLUMNS.get(table, ())
        select = f"* EXCLUDE ({', '.join(drop)})" if drop else "*"

        for cohort in self._cohorts(eng, table):
            d = (
                self.dplocal
                / f"_dbg_{stage.lower()}"
                / self.pt
                / cohort
                / f"iter_{self.iteration}"
                / table
            )
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{table}.{self.fmt}"
            where = "" if cohort == "all" else f"WHERE cohortgrp = '{cohort}'"
            # Deterministic ordering so a textual diff is meaningful.
            key = eng.con.execute(
                f"SELECT column_name FROM duckdb_columns() "
                f"WHERE table_name = '{table}' "
                f"AND column_name IN ('patid','indexdt','cohortgrp','step_no') "
                f"ORDER BY column_index"
            ).fetchall()
            order = f"ORDER BY {', '.join(k[0] for k in key)}" if key else ""
            opts = ("FORMAT CSV, HEADER" if self.fmt == "csv"
                    else "FORMAT PARQUET, COMPRESSION ZSTD")
            eng.con.execute(
                f"COPY (SELECT {select} FROM {table} {where} {order}) "
                f"TO '{path}' ({opts})"
            )
            out.append(path)
        return out
