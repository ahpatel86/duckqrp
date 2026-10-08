"""
Running the pipeline behind a UI.

A UI cannot call `run()` directly: it blocks for minutes, so the
interface freezes and there is no way to cancel. `RunHandle` puts the
run on a worker thread and exposes it as three things a frontend can
actually use — an event queue, a cancel button, and a result.

    handle = RunHandle(study, indata, threads=4, memory_limit="8GB")
    handle.start()
    for event in handle.events():      # blocks only until the next event
        render(event)
    handle.cancel()                    # safe from any thread

Deliberately framework-agnostic. Nothing here imports a UI toolkit, so
the same runner drives the terminal, the TUI, a web backend, or a
notebook. Choosing a UI framework should not be an architectural
decision, and if it is, the architecture is wrong.

Threads, not processes
----------------------
DuckDB releases the GIL during query execution, so a worker thread does
not starve the UI, and `interrupt()` works across threads (verified).
A subprocess would add IPC and make the result tables unreachable —
they live in the connection. The tradeoff is that a hard crash takes the
UI with it; for a local analysis tool that is the right side to err on.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .config import StudyConfig
from .engine import Engine
from .events import (
    Event,
    EventSink,
    Level,
    LogMessage,
    RunFinished,
    multi_sink,
)
from .sysinfo import suggest_memory_limit  # noqa: F401  (re-exported)
from .pipeline import plan_stages, run


@dataclass
class RunHandle:
    """A pipeline run that can be watched and cancelled."""

    study: StudyConfig
    indata: str | Path
    output_dir: str | Path | None = None
    threads: int | None = None
    memory_limit: str | None = None
    temp_directory: str | None = None
    database: str = ":memory:"
    # Parity with the CLI. The UI had neither, so a table kept outside
    # --indata could not be pointed at, and diagnostics could not be
    # requested, from the interface a non-programmer is most likely to
    # use.
    table_map: dict[str, str] | None = None
    debug: bool = False
    # Output shaping, also previously CLI-only.
    csv: bool = False
    layout: str = "split"
    names: str = "sas"
    text: bool = True
    extra_sink: EventSink | None = None

    engine: Engine | None = field(default=None, init=False)
    error: str = field(default="", init=False)
    seconds: float = field(default=0.0, init=False)

    _q: "queue.Queue[Event | None]" = field(
        default_factory=queue.Queue, init=False
    )
    _thread: threading.Thread | None = field(default=None, init=False)
    _done: threading.Event = field(default_factory=threading.Event, init=False)
    _cancel_requested: bool = field(default=False, init=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _sink: EventSink | None = field(default=None, init=False)

    # ---------------- lifecycle -------------------------------------

    def start(self) -> "RunHandle":
        if self._thread is not None:
            raise RuntimeError("run already started")
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()
        return self

    def _work(self) -> None:
        t0 = time.perf_counter()
        try:
            # One sink for everything. Run-level events used to be put
            # straight on the queue, which meant extra sinks (the JSONL
            # run log) silently never saw RunStarted or RunFinished —
            # the two records you most want when asking why a run was
            # slower than last week. Emit through the same path instead.
            self._sink = multi_sink(self._q.put, self.extra_sink)
            self.engine = Engine(
                database=self.database,
                threads=self.threads,
                memory_limit=self.memory_limit,
                temp_directory=self.temp_directory,
                verbose=False,
                on_event=self._sink,
            )
            self._ready.set()          # cancel() is safe from here on

            if self._cancel_requested:  # cancelled during startup
                self.engine.cancel()

            # pipeline.run emits RunStarted and RunFinished itself, so
            # the CLI and the UI observe an identical stream. Nothing to
            # emit here on the happy path.
            run(self.study, self.indata, engine=self.engine,
                output_dir=self.output_dir, table_map=self.table_map,
                debug=self.debug, csv=self.csv, layout=self.layout,
                names=self.names, text=self.text, verbose=False)
            self.seconds = time.perf_counter() - t0
        except Exception as exc:
            self.seconds = time.perf_counter() - t0
            cancelled = (
                self._cancel_requested
                or type(exc).__name__ in ("InterruptException", "CancelledRun")
            )
            self.error = "" if cancelled else self._explain(exc)
            if not cancelled:
                self._emit(LogMessage(level=Level.ERROR, message=self.error))
            self._emit(RunFinished(
                seconds=self.seconds, ok=False, error=self.error,
                cancelled=cancelled,
                peak_memory_bytes=getattr(self.engine, "peak_memory_bytes", 0),
                peak_spill_bytes=getattr(self.engine, "peak_spill_bytes", 0),
            ))
        finally:
            self._ready.set()
            self._done.set()
            self._q.put(None)          # sentinel: closes events()

    def _emit(self, event: Event) -> None:
        """Emit through the shared sink, falling back to the queue.

        The fallback matters: if the run fails while constructing the
        Engine, `_sink` is still None but the UI must still receive a
        RunFinished or it will wait forever.
        """
        if self._sink is not None:
            self._sink(event)
        else:
            self._q.put(event)

    @staticmethod
    def _explain(exc: Exception) -> str:
        """Delegates to qrp.errors.explain, shared with the CLI."""
        from .errors import explain

        return explain(exc)

    # ---------------- control ---------------------------------------

    def plan(self) -> list[str]:
        """Stage names in order (delegates to pipeline.plan_stages)."""
        return plan_stages(self.study)

    def cancel(self) -> None:
        """Request cancellation. Safe from any thread, and idempotent.

        If the worker has not built its Engine yet, the flag is honoured
        as soon as it does — otherwise a fast click would be lost.
        """
        self._cancel_requested = True
        if self.engine is not None:
            self.engine.cancel()

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._done.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # ---------------- consumption -----------------------------------

    def events(self, timeout: float | None = None) -> Iterator[Event]:
        """Yield events until the run ends.

        Blocks only until the next event, so a UI loop stays responsive.
        """
        while True:
            try:
                ev = self._q.get(timeout=timeout)
            except queue.Empty:
                return
            if ev is None:
                return
            yield ev

    def drain(self) -> list[Event]:
        """Non-blocking: every event available right now.

        For UIs that poll on a timer (Textual, Streamlit) rather than
        iterating a generator.
        """
        out: list[Event] = []
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return out
            if ev is None:
                return out
            out.append(ev)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()
