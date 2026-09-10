"""
Per-run log files.

`jsonl_sink` already existed, but a JSONL event stream is not what
someone attaches to a parity report or a bug — they want a file they can
open and read, that says what was run, on what, with which settings, and
what happened.

`RunLog` writes both:

    <log-dir>/<run>_<timestamp>.log     human-readable
    <log-dir>/<run>_<timestamp>.jsonl   one JSON event per line

The two are complementary. The `.log` is for a person; the `.jsonl` is
for answering "which stage regressed since last Tuesday" across many
runs without re-running anything.

Provenance is the point of the header
-------------------------------------
A log that records only what happened, and not what was asked for, is
half useless six weeks later. The header captures the study parameters,
the resolved per-cohort settings, the input paths, the engine settings
and the library versions — enough to reconstruct the run, or to explain
why two runs disagreed.

Capturing what would otherwise escape
-------------------------------------
Three things were previously going only to a terminal:

* `warnings.warn` from `load_study` (unimplemented tables, unresolved
  names) went to stderr,
* `print()` in the console sink went to stdout,
* stdlib `logging` records from any library went nowhere.

`RunLog.capture()` routes all three into the same file, so the log is
complete rather than merely mostly complete.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from .config import StudyConfig
from .events import (
    Event,
    LogMessage,
    MemoryStatus,
    RunFinished,
    RunStarted,
    StageFinished,
    StageSkipped,
    StageStarted,
    StatementFinished,
)

def _versions() -> dict[str, str]:
    import duckdb

    out = {
        "python": sys.version.split()[0],
        "duckdb": duckdb.__version__,
        "platform": platform.platform(),
    }
    try:
        from importlib.metadata import version

        out["qrp"] = version("qrp-duckdb")
    except Exception:
        out["qrp"] = "dev"
    return out


@dataclass
class RunLog:
    """Writes a human-readable and a machine-readable log for one run."""

    directory: str | Path = "logs"
    run_id: str = "qrp"
    write_jsonl: bool = True

    log_path: Path = field(init=False)
    jsonl_path: Path | None = field(init=False, default=None)

    _fh: TextIO = field(init=False, repr=False)
    _jfh: TextIO | None = field(init=False, default=None, repr=False)
    _handler: logging.Handler | None = field(init=False, default=None, repr=False)
    _old_showwarning: Any = field(init=False, default=None, repr=False)
    _was_spilling: bool = field(init=False, default=False)
    _closed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        d = Path(self.directory)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{self.run_id}_{stamp}"
        self.log_path = d / f"{base}.log"
        self._fh = open(self.log_path, "w", buffering=1, encoding="utf-8")
        if self.write_jsonl:
            self.jsonl_path = d / f"{base}.jsonl"
            self._jfh = open(self.jsonl_path, "w", buffering=1, encoding="utf-8")

    # ---------------- writing ---------------------------------------

    def write(self, text: str = "") -> None:
        if self._closed:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        for line in (text.split("\n") if text else [""]):
            self._fh.write(f"{ts}  {line}\n" if line else "\n")

    def _json(self, obj: dict[str, Any]) -> None:
        if self._jfh is None or self._closed:
            return
        self._jfh.write(json.dumps(obj, default=str) + "\n")

    # ---------------- header ----------------------------------------

    def header(self, study: StudyConfig, indata: str | Path,
               settings: dict[str, Any] | None = None) -> None:
        """Record what was asked for, not just what happened."""
        v = _versions()
        s = settings or {}
        self.write("=" * 78)
        self.write(f"QRP Type {study.study_type} run — {study.run_id}")
        self.write(f"started {datetime.now().isoformat(timespec='seconds')}")
        self.write("=" * 78)
        self.write()
        self.write("environment")
        for k in ("qrp", "duckdb", "python", "platform"):
            self.write(f"  {k:<12} {v[k]}")
        self.write()
        self.write("inputs")
        self.write(f"  {'indata':<12} {indata}")
        self.write(f"  {'period':<12} {study.start_date} to {study.end_date}")
        self.write(f"  {'censor':<12} {study.effective_censor_date}")
        self.write()
        if s:
            self.write("engine settings")
            for k, val in s.items():
                self.write(f"  {k:<12} {val if val is not None else 'auto'}")
            self.write()
        self.write(f"cohorts ({len(study.cohorts)})")
        for c in study.cohorts:
            self.write(
                f"  {c.cohortgrp}: washout={c.wash_per} gap={c.episode_gap}"
                f"{c.episode_gap_type} minepis={c.min_epis_dur} "
                f"maxepis={c.max_epis_dur} fupwash={c.fup_wash_per} "
                f"enrdays={c.enr_days} coverage={c.coverage} "
                f"point={c.point} dose={c.needs_dose} "
                f"strata={len(c.age_strata.strata)} "
                f"defcodes={len(c.exposure_codes)} "
                f"eventcodes={len(c.event_codes)}"
            )
        if study.covariates:
            self.write(f"covariates ({len(study.covariates)})")
            for cov in study.covariates[:20]:
                self.write(
                    f"  {cov.covarnum:>4} {cov.covarname:<28} {cov.codecat} "
                    f"[{cov.covfrom},{cov.covto}] dateonly={cov.dateonly} "
                    f"codes={len(cov.codes)}"
                )
            if len(study.covariates) > 20:
                self.write(f"  … and {len(study.covariates) - 20} more")
        self.write()
        self.write("-" * 78)
        self._json({
            "type": "Header",
            "at": datetime.now().timestamp(),
            "run_id": study.run_id,
            "indata": str(indata),
            "versions": v,
            "settings": s,
            "cohorts": [c.cohortgrp for c in study.cohorts],
            "n_covariates": len(study.covariates),
        })

    # ---------------- event sink ------------------------------------

    def sink(self) -> Any:
        """An EventSink that writes both files."""

        def _sink(ev: Event) -> None:
            self._json({"type": type(ev).__name__,
                        **{k: (v.value if isinstance(v, Enum) else v)
                           for k, v in asdict(ev).items()}})

            if isinstance(ev, RunStarted):
                self.write(f"RUN START  {len(ev.cohorts)} cohort(s), "
                           f"{len(ev.stages)} stage(s)")
            elif isinstance(ev, StageStarted):
                self.write(f"  [{ev.index}/{ev.total}] {ev.name} …")
            elif isinstance(ev, StageFinished):
                rows = f"{ev.rows:,} rows" if ev.rows >= 0 else "script"
                self.write(f"  [{ev.index}/{ev.total}] {ev.name} "
                           f"— {ev.seconds:.3f}s, {rows}")
            elif isinstance(ev, StatementFinished):
                if not ev.target:
                    return
                if ev.rows >= 0:
                    shape = f"{ev.rows:>13,} rows x {ev.columns:>2} cols"
                    if ev.delta_rows is not None:
                        shape += f"  ({ev.delta_rows:+,})"
                else:
                    shape = f"{'view':>13}      x {ev.columns:>2} cols"
                self.write(f"      {ev.seconds:7.3f}s  {ev.kind:<5} "
                           f"{ev.target:<26}{shape}")
            elif isinstance(ev, StageSkipped):
                self.write(f"  skipped: {ev.name} ({ev.reason})")
            elif isinstance(ev, LogMessage):
                self.write(f"  {ev.level.value.upper()}: {ev.message}")
                if ev.detail:
                    self.write(f"      {ev.detail}")
            elif isinstance(ev, MemoryStatus):
                # Only log the transition into and out of spilling.
                if ev.spilling and not self._was_spilling:
                    self.write(
                        f"  SPILLING — RAM {ev.used_bytes / 1e6:,.0f}MB of "
                        f"{ev.limit_bytes / 1e6:,.0f}MB limit, "
                        f"{ev.spilled_bytes / 1e6:,.0f}MB now on disk"
                    )
                    self._was_spilling = True
                elif not ev.spilling and self._was_spilling:
                    self.write("  spilling stopped")
                    self._was_spilling = False
            elif isinstance(ev, RunFinished):
                self.write("-" * 78)
                if ev.cancelled:
                    self.write(f"CANCELLED after {ev.seconds:.2f}s")
                elif ev.ok:
                    self.write(f"OK in {ev.seconds:.2f}s")
                else:
                    self.write(f"FAILED after {ev.seconds:.2f}s")
                    self.write(f"  {ev.error}")
                if ev.peak_memory_bytes:
                    line = f"peak RAM {ev.peak_memory_bytes / 1e6:,.0f}MB"
                    if ev.peak_spill_bytes:
                        line += (f", spilled {ev.peak_spill_bytes / 1e6:,.0f}MB"
                                 f" to disk")
                    else:
                        line += " (no spilling)"
                    self.write(line)
                if ev.tables:
                    self.write("output tables")
                    for name, n in ev.tables.items():
                        self.write(f"  {name:<24} {n:>14,}")

        return _sink

    # ---------------- capture stray output ---------------------------

    def capture(self) -> "RunLog":
        """Route stdlib logging and `warnings` into this log too.

        Without this the load-time warnings about unimplemented tables
        go to stderr and never reach the file — which is exactly the
        information you want when a parity comparison disagrees.
        """
        fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s",
                                datefmt="%H:%M:%S")
        self._handler = logging.StreamHandler(self._fh)
        self._handler.setFormatter(fmt)
        root = logging.getLogger()
        root.addHandler(self._handler)
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)

        self._old_showwarning = warnings.showwarning

        def _show(message, category, filename, lineno, file=None, line=None):
            self.write(f"  WARNING: {category.__name__}: {message}")
            self._json({"type": "Warning", "category": category.__name__,
                        "message": str(message),
                        "at": datetime.now().timestamp()})
            if self._old_showwarning:
                self._old_showwarning(message, category, filename, lineno,
                                      file, line)

        warnings.showwarning = _show
        return self

    def stage_summary(self, engine) -> None:
        """Append the per-stage timing table."""
        self.write()
        self.write("stage timings")
        for line in engine.summary().split("\n"):
            if line.strip():
                self.write("  " + line)

    # ---------------- lifecycle -------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        if self._old_showwarning is not None:
            warnings.showwarning = self._old_showwarning
            self._old_showwarning = None
        self.write()
        self.write(f"log closed {datetime.now().isoformat(timespec='seconds')}")
        self._closed = True
        self._fh.close()
        if self._jfh is not None:
            self._jfh.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def paths(self) -> list[Path]:
        return [p for p in (self.log_path, self.jsonl_path) if p]
