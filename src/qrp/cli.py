"""Command-line entry point.

`python -m qrp run --study s.json --indata data/ --out results/`

Contrast with the PySpark package, where `python main.py` executed 4,000
lines of module-level code at import, took no arguments, and was steered
by 30 environment variables.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import inputfile as inputfile_mod
from . import scdm
from .config import load_study
from .errors import explain
from .events import console_sink, multi_sink
from .engine import Engine
from .parity import ParityDumper
from .pipeline import run


def _parse_table_map(values: list[str] | None) -> dict[str, str] | None:
    """Parse repeated --table-map TABLE=PATH arguments."""
    if not values:
        return None
    out: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(
                f"--table-map expects TABLE=PATH, got {item!r}"
            )
        key, path = item.split("=", 1)
        out[key.strip().lower()] = path.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qrp", description="Sentinel QRP Type 2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the pipeline")
    r.add_argument("--study", required=True, help="study definition JSON")
    r.add_argument("--indata", required=True, help="CDM parquet root")
    r.add_argument("--out", help="write output tables here")
    r.add_argument("--threads", type=int)
    r.add_argument(
        "--memory-limit",
        help=("DuckDB memory ceiling, e.g. 8GB. Defaults to 8GB, or less "
              "on a smaller host. Leaving DuckDB to its own default would "
              "take 80%% of physical RAM — ~102GB on a 128GB server — "
              "which measurement shows is unnecessary: above 1GB, more "
              "memory buys about 2%%. Below the limit the pipeline spills "
              "to disk rather than failing."))
    r.add_argument("--db", default=":memory:",
                   help="persist to a .duckdb file; keeps intermediates "
                        "buffer-managed on disk instead of in RAM")
    r.add_argument("--temp-dir", help="spill directory for "
                                      "larger-than-memory operations")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--log-dir", default=None,
                   help="write <run>_<timestamp>.log and .jsonl here")
    r.add_argument("--table-map", action="append", metavar="TABLE=PATH",
                   help="map an SCDM table to a specific file or folder, "
                        "e.g. enrollment=dp042_elig_2024q1.parquet. Repeatable. "
                        "Overrides name and column matching.")
    r.add_argument("--layout", choices=("split", "flat"), default="split",
                   help="split (default): dplocal/ and msoc/, named "
                        "<runid>_<table>. dplocal is patient-level and "
                        "stays behind the DP firewall; msoc is aggregate "
                        "and is what goes to the Operations Center. "
                        "flat: one directory, no runid prefix.")
    r.add_argument("--names", choices=("sas", "logical"), default="sas",
                   help="sas (default): SAS QRP dataset names "
                        "(<runid>_mstr, <runid>_denomcounts) for parity "
                        "with existing SOPs and tooling. logical: this "
                        "pipeline's table names. Either way a manifest "
                        "maps logical names to files.")
    r.add_argument("--csv", action="store_true",
                   help="also write CSV copies under <out>/csv for Excel")
    r.add_argument("--no-jsonl", action="store_true",
                   help="write only the human-readable log")
    r.add_argument("--parity-dump", help="<dplocal> root for parity CSVs")
    r.add_argument("--pt", default="pt001")
    r.add_argument("--iter", dest="iteration", default="00")

    u = sub.add_parser("ui", help="interactive terminal UI")
    u.add_argument("--study", default="", help="prefill the study field")
    u.add_argument("--indata", default="", help="prefill the SCDM root")
    u.add_argument("--out", default="", help="prefill the output dir")

    sv = sub.add_parser(
        "serve",
        help="serve the terminal UI to a browser (for Git Bash, legacy "
             "cmd.exe, or users who would rather not use a terminal)",
    )
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    sh = sub.add_parser("show", help="print result tables from a finished run")
    sh.add_argument("table", nargs="?", help="table name; omit to list them")
    sh.add_argument("--out", required=True, help="the --out folder of the run")
    sh.add_argument("--limit", type=int, default=50)
    sh.add_argument("--where", help="SQL filter, e.g. \"cohortgrp='x'\"")
    sh.add_argument("--csv", metavar="DIR",
                    help="export tables to CSV here instead of printing")

    v = sub.add_parser("validate", help="check a study definition and exit")
    v.add_argument("--study", required=True)

    i = sub.add_parser(
        "inspect",
        help="report what is in an input file and/or an SCDM parquet root",
    )
    i.add_argument("--study", help="QRP input file JSON")
    i.add_argument("--lookup", help="qrp_inputfiles_lookup.json")
    i.add_argument("--indata", help="SCDM parquet root")
    i.add_argument("--table-map", action="append", metavar="TABLE=PATH",
                   help="map a table explicitly; repeatable")
    i.add_argument("--no-count", action="store_true",
                   help="skip row counts (faster on large data)")

    a = ap.parse_args(argv)

    if a.cmd == "ui":
        try:
            from .tui import main as tui_main
        except ImportError:
            print("The terminal UI needs textual:  pip install 'qrp-duckdb[ui]'")
            return 1
        tui_main(a.study, a.indata, a.out)
        return 0

    if a.cmd == "show":
        from . import show as show_mod

        if a.csv:
            files = show_mod.to_csv(a.out, a.csv,
                                    [a.table] if a.table else None)
            print(f"wrote {len(files)} CSV file(s) to {a.csv}")
            for f in files:
                print(f"  {f}")
            return 0
        print(show_mod.show(a.out, a.table, a.limit, a.where))
        return 0

    if a.cmd == "serve":
        try:
            from textual_serve.server import Server
        except ImportError:
            print("Serving needs textual-serve:  pip install textual-serve")
            return 1
        print(f"QRP UI at http://{a.host}:{a.port}   (ctrl+c to stop)")
        Server("python -m qrp ui", host=a.host, port=a.port,
               title="QRP Type 2").serve()
        return 0

    if a.cmd == "inspect":
        if not a.study and not a.indata:
            ap.error("inspect needs --study and/or --indata")
        if a.study:
            inp = inputfile_mod.load(a.study, a.lookup)
            print(inputfile_mod.describe(inp))
            print()
        if a.indata:
            print(f"scdm root: {a.indata}\n")
            print(scdm.format_report(
                scdm.probe(a.indata, sample_rows=not a.no_count,
                           table_map=_parse_table_map(a.table_map))))
        return 0

    if a.cmd == "validate":
        s = load_study(a.study)
        print(f"OK: type {s.study_type}, {len(s.cohorts)} cohort(s), "
              f"{s.start_date} to {s.end_date}")
        for c in s.cohorts:
            print(f"  {c.cohortgrp:<20} washout={c.wash_per} "
                  f"gap={c.episode_gap} dose={c.needs_dose} "
                  f"fupwash={c.fup_wash_per} strata={len(c.age_strata.strata)}")
        return 0

    # The log is opened BEFORE load_study so that its warnings — the
    # unimplemented-table and unresolved-name ones — land in the file
    # rather than only on stderr.
    runlog = None
    if a.log_dir:
        from .runlog import RunLog

        runlog = RunLog(a.log_dir, run_id="qrp",
                        write_jsonl=not a.no_jsonl).capture()

    try:
        study = load_study(a.study)
        if runlog is not None:
            runlog.run_id = study.run_id

        eng = Engine(
            database=a.db, threads=a.threads,
            memory_limit=a.memory_limit,
            temp_directory=getattr(a, "temp_dir", None),
            verbose=not a.quiet,
            on_event=multi_sink(
                console_sink(not a.quiet),
                runlog.sink() if runlog else None,
            ),
        )

        # Header AFTER the engine exists, so it records the EFFECTIVE
        # settings rather than what was asked for. `memory_limit=None`
        # printed as "auto", which told an operator nothing — the
        # package default is 8GB (less on a small host) and a log that
        # says "auto" cannot be used to explain what a job consumed.
        if runlog is not None:
            runlog.header(study, a.indata, {
                "threads": eng.effective_threads,
                "memory_limit": eng.effective_memory_limit,
                "temp_dir": getattr(a, "temp_dir", None) or "auto",
                "database": a.db,
                "output_dir": a.out,
            })
        run(study, a.indata, engine=eng, output_dir=a.out,
            csv=a.csv, layout=a.layout, names=a.names,
            table_map=_parse_table_map(a.table_map),
            verbose=not a.quiet)

        if a.parity_dump:
            files = ParityDumper(Path(a.parity_dump), a.pt,
                                 a.iteration).dump(eng)
            print(f"  parity dump: {len(files)} file(s) under {a.parity_dump}")
        if runlog is not None:
            runlog.stage_summary(eng)
        eng.close()
    except Exception as exc:
        # The runbook promises actionable errors. That promise has to
        # hold on the CLI, not only in the UI.
        message = explain(exc)
        if runlog is not None:
            runlog.write(f"  FAILED: {message}")
        print(f"\nFailed:\n  {message}", file=sys.stderr)
        return 1
    finally:
        if runlog is not None:
            runlog.close()
            for pth in runlog.paths():
                print(f"  log: {pth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
