"""
Structured run events.

Why this exists before any UI
-----------------------------
The pipeline currently reports itself with `print()`. That is fine for a
terminal and useless for anything else: a UI cannot render a progress
bar from a string, cannot tell a warning from a row count, and cannot
show which stage is running *now* rather than which one just finished.

So the first thing a UI needs is not a UI. It is an event stream. Once
the pipeline emits typed events, the frontend becomes a swappable
detail — terminal, TUI, web, notebook, or a log file — and none of them
require touching pipeline code.

This is also the fix for the 316 `print()` calls and single `logging`
reference in the PySpark package. The lesson there is that observability
retrofitted after the fact stays shallow; emitted events are structured
at the point where the information exists.

Consumers register a callback:

    def on_event(ev: Event) -> None: ...
    engine = Engine(on_event=on_event)

Callbacks must not raise and must not block — `_emit` swallows
exceptions so a broken UI cannot take down a run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeAlias


class Level(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    """Base event. `at` is a monotonic timestamp for ordering."""

    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RunStarted(Event):
    run_id: str = ""
    cohorts: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()      # planned stage names, for a progress bar
    indata: str = ""
    threads: int | None = None
    memory_limit: str | None = None


@dataclass(frozen=True)
class StageStarted(Event):
    name: str = ""
    index: int = 0
    total: int = 0


@dataclass(frozen=True)
class StageProgress(Event):
    """Intra-stage progress, polled from DuckDB.

    DuckDB reports a percentage for the query currently executing on a
    connection. It is genuinely live for scans, joins and aggregations;
    it can sit at 0 for operators that cannot estimate cardinality, so a
    UI should treat this as advisory and never as a completion signal.
    """

    name: str = ""
    percent: float = 0.0
    elapsed: float = 0.0


@dataclass(frozen=True)
class StatementFinished(Event):
    """One SQL statement inside a stage.

    Stages used to report a single timing and the word "script", because
    each .sql file was executed as one call. With 47 statements across
    11 files that meant no row counts and no way to tell which statement
    in a 30-second stage was the slow one.
    """

    stage: str = ""
    target: str = ""          # table/view being created
    kind: str = ""            # TABLE | VIEW | MACRO | DROP
    rows: int = -1
    columns: int = -1
    seconds: float = 0.0
    delta_rows: int | None = None    # change vs the input it derives from
    # Memory attributed to THIS statement. The watcher thread samples
    # continuously and reports live status, which answers "are we
    # spilling right now" but not "which statement caused it" — and
    # after a 40-second stage that is the only question that matters.
    #
    # peak_bytes is the high-water mark observed while this statement
    # ran; spilled_bytes is how much MORE went to disk during it, not
    # the running total, so the figure points at a culprit rather than
    # accumulating across the run.
    peak_bytes: int = 0
    spilled_bytes: int = 0

    @property
    def spilled(self) -> bool:
        return self.spilled_bytes > 0


@dataclass(frozen=True)
class MemoryStatus(Event):
    """Live memory and spill telemetry.

    `spilled_bytes` is the answer to "is this run swapping to disk?".
    DuckDB never fails on a memory limit — it spills — so without this
    the user sees a slow run and cannot tell whether the limit is the
    cause. Sourced from duckdb_memory(), polled on a separate cursor
    because the executing connection is blocked.
    """

    used_bytes: int = 0
    spilled_bytes: int = 0
    limit_bytes: int = 0

    @property
    def spilling(self) -> bool:
        return self.spilled_bytes > 0

    @property
    def pressure(self) -> float:
        """0-1 fraction of the configured limit in use."""
        return (self.used_bytes / self.limit_bytes) if self.limit_bytes else 0.0


@dataclass(frozen=True)
class StageFinished(Event):
    name: str = ""
    index: int = 0
    total: int = 0
    table: str | None = None
    rows: int = -1
    seconds: float = 0.0


@dataclass(frozen=True)
class EmptyResult(Event):
    """A stage produced no rows where rows were expected.

    Reported prominently because a run that completes with an empty
    cohort looks like a success. The usual cause is a study whose codes
    do not appear in the data at all — a mismatch between the study
    definition and the extract, not a pipeline failure, and one the tool
    should name rather than leave the user to discover.
    """

    stage: str = ""
    table: str = ""
    reason: str = ""
    hint: str = ""


@dataclass(frozen=True)
class StageSkipped(Event):
    name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LogMessage(Event):
    level: Level = Level.INFO
    message: str = ""
    detail: str = ""


@dataclass(frozen=True)
class RunFinished(Event):
    seconds: float = 0.0
    ok: bool = True
    error: str = ""
    cancelled: bool = False
    tables: dict[str, int] = field(default_factory=dict)
    peak_memory_bytes: int = 0
    peak_spill_bytes: int = 0


# A sink is any callable taking an Event. This was a Protocol with a
# single __call__ member, which is the same thing written the long way
# and which mypy will not accept a plain function for without a cast.
EventSink: TypeAlias = Callable[[Event], None]


# ---------------------------------------------------------------------
# Built-in sinks
# ---------------------------------------------------------------------


def console_sink(verbose: bool = True) -> EventSink:
    """The default terminal renderer — what `print` used to do."""

    def sink(ev: Event) -> None:
        if not verbose:
            return
        if isinstance(ev, RunStarted):
            print(f"QRP Type 2 — {len(ev.cohorts)} cohort(s), "
                  f"{len(ev.stages)} stage(s)")
            print(f"  indata: {ev.indata}\n")
        elif isinstance(ev, StageFinished):
            shown = f"{ev.rows:>10,}" if ev.rows >= 0 else f"{'script':>10}"
            print(f"  [{ev.seconds:7.3f}s] {ev.name:<34} {shown}")
        elif isinstance(ev, StatementFinished) and verbose == "statements":
            if ev.rows >= 0:
                print(f"      {ev.seconds:6.3f}s  {ev.target:<26}"
                      f"{ev.rows:>12,} x {ev.columns}")
        elif isinstance(ev, StageSkipped):
            print(f"  {'[ skipped ]':>10} {ev.name} ({ev.reason})")
        elif isinstance(ev, EmptyResult):
            print(f"\n  !! {ev.reason}")
            if ev.hint:
                print(f"     {ev.hint}")
        elif isinstance(ev, LogMessage) and ev.level in (Level.WARNING, Level.ERROR):
            print(f"  {ev.level.value.upper()}: {ev.message}")
        elif isinstance(ev, RunFinished):
            if ev.peak_spill_bytes:
                print(f"  peak RAM {ev.peak_memory_bytes / 1e6:,.0f} MB, "
                      f"spilled {ev.peak_spill_bytes / 1e6:,.0f} MB to disk")
            if ev.cancelled:
                print(f"\ncancelled after {ev.seconds:.2f}s")
            elif ev.ok:
                print(f"\nwall clock: {ev.seconds:.2f}s")
            else:
                print(f"\nFAILED after {ev.seconds:.2f}s: {ev.error}")

    return sink


def jsonl_sink(path: str) -> EventSink:
    """Append every event as JSON, one per line.

    A machine-readable run log costs almost nothing once events exist,
    and makes 'why was this run slower than last Tuesday' answerable
    without re-running anything.
    """
    import json
    from dataclasses import asdict

    fh = open(path, "a", buffering=1)

    def sink(ev: Event) -> None:
        row: dict[str, Any] = {"type": type(ev).__name__, **asdict(ev)}
        for k, v in row.items():
            if isinstance(v, Enum):
                row[k] = v.value
        fh.write(json.dumps(row, default=str) + "\n")

    return sink


def multi_sink(*sinks: EventSink | None) -> EventSink:
    active = [s for s in sinks if s is not None]

    def sink(ev: Event) -> None:
        for s in active:
            try:
                s(ev)
            except Exception:
                pass          # a broken sink must never fail a run

    return sink
