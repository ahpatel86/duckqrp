"""
Terminal UI.

Why a TUI rather than a browser app
-----------------------------------
This runs at Data Partner sites. A TUI needs no open port, no browser,
no localhost binding and no security review of a bundled web server, and
it works unchanged over SSH into a locked-down analysis VM — which is
frequently how these runs actually happen. `pip install textual` is the
whole deployment story.

The tradeoff is honest: a web UI is friendlier for an epidemiologist who
has never used a terminal, and can render charts. See docs/UI.md for the
comparison. Nothing here is load-bearing — this consumes the same
`RunHandle` event stream a Streamlit or FastAPI frontend would, so
swapping it out touches no pipeline code.

    python -m qrp ui --study <file>.json --indata <scdm root>
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .browse import BrowseScreen
from .config import StudyConfig, load_study
from .events import (
    LogMessage,
    RunFinished,
    RunStarted,
    StageFinished,
    StageProgress,
    StageSkipped,
    StageStarted,
)
from .inputfile import describe, load as load_inputfile
from .events import EmptyResult, MemoryStatus, StatementFinished
from .runlog import RunLog
from .runner import RunHandle
from .scdm import format_report, probe
from .sysinfo import (HostInfo, cpu_count, human,
                       suggest_memory_limit)

CSS = """
Screen { layout: vertical; }
#config { height: auto; padding: 1 2; border: round $primary; }
#config Label { width: 16; content-align: right middle; padding-right: 1; }
/* The path Inputs must leave room for their Browse button: unconstrained
   they take the whole row and push it off-screen. */
#study, #indata, #output, #tempdir, #logdir { width: 1fr; }
.browse { width: 12; min-width: 12; margin-left: 1; }
.row { height: 3; }
#actions { height: auto; padding: 0 2; }
#actions Button { margin-right: 2; }
#progress { height: auto; padding: 1 2; }
#stage_label { text-style: bold; }
RichLog { border: round $secondary; }
DataTable { height: 1fr; }
.muted { color: $text-muted; }
#hostinfo { color: $text-muted; padding: 0 2; height: 1; }
#memline { height: 1; padding: 0 2; }
#memline.spilling { color: $warning; text-style: bold; }
#memline.ok { color: $text-muted; }
#storage { width: 22; }
#memory { width: 16; }
"""


class QRPApp(App):
    """Configure, run and watch a QRP Type 2 job."""

    CSS = CSS
    TITLE = "QRP Type 2 — DuckDB"
    # Ctrl-combinations with priority=True, so they fire even while a
    # path Input has focus. Plain letters would be swallowed as text —
    # found by driving the app headlessly, which is exactly the class of
    # bug that never shows up in a manual click-through.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("ctrl+t", "inspect", "Inspect", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    handle: RunHandle | None = None
    study: StudyConfig | None = None
    _runlog: RunLog | None = None
    _empty_reason: str = ""
    running: reactive[bool] = reactive(False)

    def __init__(self, study_path: str = "", indata: str = "",
                 output_dir: str = "") -> None:
        super().__init__()
        self._study_path = study_path
        self._indata = indata
        self._output = output_dir

    # ---------------- layout ----------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="config"):
            with Horizontal(classes="row"):
                yield Label("Study JSON")
                yield Input(value=self._study_path, id="study",
                            placeholder="qrp_inputfiles_....json")
                yield Button("Browse", id="browse_study", classes="browse")
            with Horizontal(classes="row"):
                yield Label("SCDM root")
                yield Input(value=self._indata, id="indata",
                            placeholder="/path/to/scdm_parquet")
                yield Button("Browse", id="browse_indata", classes="browse")
            with Horizontal(classes="row"):
                yield Label("Output dir")
                yield Input(value=self._output, id="output",
                            placeholder="(optional)")
                yield Button("Browse", id="browse_output", classes="browse")
            with Horizontal(classes="row"):
                yield Label("Threads")
                yield Select(
                    [(x, x) for x in
                     ("auto", *(str(n) for n in (1, 2, 4, 8, 16, 32)
                                if n <= max(HostInfo.detect().cpus, 1)))],
                    value="auto", id="threads", allow_blank=False,
                )
                # A free-text Input, not a fixed dropdown: someone on a
                # 512GB server should be able to type 384GB, and someone
                # who wants 6GB should not have to pick 4 or 8.
                yield Label("Memory")
                yield Input(value=suggest_memory_limit(), id="memory",
                            placeholder="auto")
                yield Label("Storage")
                yield Select(
                    [("in-memory (fast)", "mem"),
                     ("on-disk (low RAM)", "disk")],
                    value="mem", id="storage", allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Label("Spill dir")
                yield Input(value="", id="tempdir",
                            placeholder="(system temp) — used when RAM runs out")
            with Horizontal(classes="row"):
                yield Label("Log dir")
                yield Input(value="logs", id="logdir",
                            placeholder="writes <run>_<time>.log and .jsonl")
        with Horizontal(id="actions"):
            yield Button("Inspect", id="btn_inspect", variant="default")
            yield Button("Run", id="btn_run", variant="success")
            yield Button("Cancel", id="btn_cancel", variant="error",
                         disabled=True)
        yield Static(HostInfo.detect().summary(), id="hostinfo")
        with Vertical(id="progress"):
            yield Static("idle", id="stage_label")
            yield ProgressBar(total=100, show_eta=False, id="bar")
            yield Static("", id="memline", classes="ok")
        with TabbedContent(initial="tab_log"):
            with TabPane("Log", id="tab_log"):
                yield RichLog(highlight=True, markup=True, wrap=True,
                              id="log")
            with TabPane("Stages", id="tab_stages"):
                yield DataTable(id="stages")
            with TabPane("Results", id="tab_results"):
                yield DataTable(id="results")
        yield Footer()

    # per-stage peak/spill, filled from StatementFinished and consumed
    # when the stage finishes
    _stage_peak: dict[str, int] = {}
    _stage_spill: dict[str, int] = {}

    def on_mount(self) -> None:
        self._stage_peak = {}
        self._stage_spill = {}
        st = self.query_one("#stages", DataTable)
        # peak and spill per STAGE. The RAM line shows live pressure,
        # which says a run is spilling but not which step caused it —
        # and that is the question once the run is over.
        st.add_columns("#", "stage", "rows", "seconds", "peak", "spilled")
        rs = self.query_one("#results", DataTable)
        rs.add_columns("table", "rows")
        self.log_line("[dim]Set the study file and SCDM root, then "
                      "Inspect (ctrl+t) or Run (ctrl+r). Escape cancels."
                      "[/dim]")

    # ---------------- helpers ---------------------------------------

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _field(self, name: str) -> str:
        return self.query_one(f"#{name}", Input).value.strip()

    def _choice(self, name: str) -> str | None:
        v = self.query_one(f"#{name}", Select).value
        return None if v == "auto" else str(v)

    def _choice_raw(self, name: str) -> str:
        return str(self.query_one(f"#{name}", Select).value)

    # ---------------- actions ---------------------------------------

    # -- browse -------------------------------------------------------

    @on(Button.Pressed, ".browse")
    def _browse(self, event: Button.Pressed) -> None:
        """Open a picker for the field the button sits beside.

        Textual has no native file dialog — there is no OS picker in a
        terminal — so this is a DirectoryTree in a ModalScreen. Typing a
        path still works and is faster when you know it.
        """
        field = str(event.button.id).removeprefix("browse_")
        wants_file = field == "study"
        screen = BrowseScreen(
            start=self._field(field) or ".",
            dirs_only=not wants_file,
            suffixes=(".json",) if wants_file else (),
            title=("Select the study JSON" if wants_file else
                   "Select the SCDM root folder" if field == "indata" else
                   "Select the output folder"),
        )

        def apply(path: str | None) -> None:
            if path:
                self.query_one(f"#{field}", Input).value = path

        self.push_screen(screen, apply)

    @on(Button.Pressed, "#btn_inspect")
    def _inspect_pressed(self) -> None:
        self.action_inspect()

    @on(Button.Pressed, "#btn_run")
    def _run_pressed(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#btn_cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_inspect(self) -> None:
        """Check inputs before committing to a run.

        Cheap, and it is where a mis-shaped input file or a schema
        mismatch should surface — not three stages into a job.
        """
        log = self.query_one("#log", RichLog)
        study, indata = self._field("study"), self._field("indata")
        if study:
            try:
                log.write("[bold]— input file —[/bold]")
                log.write(describe(load_inputfile(study)))
            except Exception as exc:
                log.write(f"[red]input file: {type(exc).__name__}: {exc}[/red]")
        if indata:
            try:
                log.write("[bold]— scdm —[/bold]")
                log.write(format_report(probe(indata)))
            except Exception as exc:
                log.write(f"[red]scdm: {type(exc).__name__}: {exc}[/red]")
        if not study and not indata:
            log.write("[yellow]nothing to inspect[/yellow]")

    def action_run(self) -> None:
        if self.running:
            return
        study_path, indata = self._field("study"), self._field("indata")
        if not study_path or not indata:
            self.log_line("[yellow]study file and SCDM root are both "
                          "required[/yellow]")
            return

        # Warnings from load_study (unimplemented tables, unresolved
        # names) go to the log rather than stderr, where a TUI user would
        # never see them.
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.study = load_study(study_path)
            for w in caught:
                self.log_line(f"[yellow]warning:[/yellow] {w.message}")
        except Exception as exc:
            self.log_line(f"[red]{type(exc).__name__}: {exc}[/red]")
            return

        for table_id in ("stages", "results"):
            self.query_one(f"#{table_id}", DataTable).clear()

        mem = self._field("memory") or ""
        mem = None if mem.lower() in ("", "auto") else mem
        threads = self._choice("threads")
        tempdir = self._field("tempdir") or None

        # On-disk storage puts intermediates under DuckDB's buffer
        # manager instead of in RAM. Measured on the 2m study: 3807MB
        # peak in-memory vs 687MB on-disk with a 512MB limit, for a 33%
        # time cost. This is the control that makes a small VM viable,
        # so it is a visible choice rather than a hidden flag.
        if self._choice_raw("storage") == "disk":
            db_dir = Path(self._field("output") or ".")
            db_dir.mkdir(parents=True, exist_ok=True)
            database = str(db_dir / f"{self.study.run_id}.duckdb")
            self.log_line(f"[dim]on-disk storage: {database}[/dim]")
        else:
            database = ":memory:"

        self._peak_mem = self._peak_spill = 0

        # The log records the same event stream the UI renders, so what
        # you read on screen and what lands in the file cannot diverge.
        self._runlog = None
        logdir = self._field("logdir")
        if logdir:
            try:
                self._runlog = RunLog(logdir, run_id=self.study.run_id)
                # Resolve what will ACTUALLY apply. The Memory field is
                # prefilled, but a user who clears it still gets the
                # package default (8GB, less on a small host), and a log
                # reading "auto" cannot explain what a job consumed.
                self._runlog.header(self.study, indata, {
                    "threads": threads or f"{cpu_count()} (auto)",
                    "memory_limit": mem or f"{suggest_memory_limit()} (default)",
                    "temp_dir": tempdir, "database": database,
                    "output_dir": self._field("output") or None,
                })
                self.log_line(f"[dim]logging to {self._runlog.log_path}[/dim]")
            except Exception as exc:
                self.log_line(f"[yellow]could not open log: {exc}[/yellow]")

        self.handle = RunHandle(
            study=self.study,
            indata=indata,
            output_dir=self._field("output") or None,
            threads=int(threads) if threads else None,
            memory_limit=mem,
            temp_directory=tempdir,
            database=database,
            extra_sink=self._runlog.sink() if self._runlog else None,
        ).start()
        self.running = True
        self._pump()

    def action_cancel(self) -> None:
        if self.handle and self.running:
            self.log_line("[yellow]cancelling…[/yellow]")
            self.handle.cancel()

    def watch_running(self, running: bool) -> None:
        self.query_one("#btn_run", Button).disabled = running
        self.query_one("#btn_cancel", Button).disabled = not running
        self.query_one("#btn_inspect", Button).disabled = running

    # ---------------- event pump ------------------------------------

    @work(thread=True, exclusive=True)
    def _pump(self) -> None:
        """Consume the RunHandle event stream.

        Runs on a worker thread and marshals every UI mutation back to
        the main thread with call_from_thread, which is what keeps the
        interface responsive while DuckDB is busy.
        """
        assert self.handle is not None
        for ev in self.handle.events():
            self.call_from_thread(self._apply, ev)
        if self._runlog is not None:
            if self.handle.engine is not None:
                self._runlog.stage_summary(self.handle.engine)
            self._runlog.close()
            self.call_from_thread(
                self.log_line,
                f"[dim]log written: {self._runlog.log_path}[/dim]",
            )
        self.call_from_thread(setattr, self, "running", False)

    def _apply(self, ev) -> None:
        bar = self.query_one("#bar", ProgressBar)
        label = self.query_one("#stage_label", Static)

        if isinstance(ev, RunStarted):
            self.log_line(
                f"[bold green]run[/bold green] {ev.run_id} — "
                f"{len(ev.cohorts)} cohort(s): {', '.join(ev.cohorts)}"
            )
            self.log_line(
                f"[dim]threads={ev.threads or 'auto'} "
                f"memory={ev.memory_limit or 'auto'} indata={ev.indata}[/dim]"
            )
            bar.update(total=100, progress=0)

        elif isinstance(ev, StageStarted):
            label.update(f"[{ev.index}/{ev.total}] {ev.name}")
            bar.update(progress=100 * (ev.index - 1) / max(ev.total, 1))

        elif isinstance(ev, StageProgress):
            # Fill within the current stage's slice of the bar.
            total = max(self.handle.plan().__len__(), 1) if self.handle else 1
            done = self.query_one("#stages", DataTable).row_count
            bar.update(progress=100 * (done + ev.percent / 100) / total)
            label.update(
                f"{ev.name} — {ev.percent:.0f}% ({ev.elapsed:.1f}s)"
            )

        elif isinstance(ev, EmptyResult):
            # The Results tab showing 0 rows everywhere is indistinguishable
            # from a successful tiny cohort, so say why on the Log tab and
            # switch to it — the user is looking at Results and has no
            # reason to check elsewhere.
            self.log_line(f"[bold yellow]No results:[/bold yellow] {ev.reason}")
            if ev.hint:
                self.log_line(f"[yellow]{ev.hint}[/yellow]")
            self._empty_reason = ev.reason
            try:
                self.query_one("TabbedContent").active = "tab_log"
            except Exception:
                pass

        elif isinstance(ev, StatementFinished):
            # Attribute to the stage, so a slow stage can be opened up
            # without turning on statement detail.
            if ev.peak_bytes:
                self._stage_peak[ev.stage] = max(
                    self._stage_peak.get(ev.stage, 0), ev.peak_bytes)
            if ev.spilled_bytes:
                self._stage_spill[ev.stage] = (
                    self._stage_spill.get(ev.stage, 0) + ev.spilled_bytes)

        elif isinstance(ev, MemoryStatus):
            line = self.query_one("#memline", Static)
            used = human(ev.used_bytes)
            limit = human(ev.limit_bytes) if ev.limit_bytes else "auto"
            if ev.spilling:
                # Spilling is not an error — DuckDB never fails on a
                # memory limit, it goes to disk. But it IS the reason a
                # run is slow, and without saying so the user just sees
                # sluggishness and cannot connect it to the limit.
                line.set_classes("spilling")
                line.update(
                    f"RAM {used} / {limit}  ·  spilled {human(ev.spilled_bytes)}"
                    f" to disk — raise Memory to avoid this"
                )
            else:
                line.set_classes("ok")
                gauge = "█" * int(ev.pressure * 20) if ev.limit_bytes else ""
                line.update(f"RAM {used} / {limit}  {gauge}")
            self._peak_mem = max(getattr(self, "_peak_mem", 0), ev.used_bytes)
            self._peak_spill = max(getattr(self, "_peak_spill", 0),
                                   ev.spilled_bytes)

        elif isinstance(ev, StageFinished):
            rows = f"{ev.rows:,}" if ev.rows >= 0 else "—"
            peak = self._stage_peak.pop(ev.name, 0)
            spill = self._stage_spill.pop(ev.name, 0)
            self.query_one("#stages", DataTable).add_row(
                str(ev.index), ev.name, rows, f"{ev.seconds:.3f}",
                human(peak) if peak else "—",
                f"[red]{human(spill)}[/red]" if spill else "—",
            )
            self.log_line(
                f"  [green]✓[/green] {ev.name} "
                f"[dim]{ev.seconds:.2f}s  {rows}[/dim]"
            )
            bar.update(progress=100 * ev.index / max(ev.total, 1))

        elif isinstance(ev, StageSkipped):
            self.log_line(f"  [dim]— {ev.name} skipped ({ev.reason})[/dim]")

        elif isinstance(ev, LogMessage):
            colour = {"warning": "yellow", "error": "red"}.get(
                ev.level.value, "dim"
            )
            self.log_line(f"[{colour}]{ev.level.value}:[/{colour}] {ev.message}")

        elif isinstance(ev, RunFinished):
            if ev.cancelled:
                label.update("cancelled")
                self.log_line(
                    f"[yellow]cancelled after {ev.seconds:.1f}s[/yellow]"
                )
            elif ev.ok:
                label.update(f"done in {ev.seconds:.1f}s")
                bar.update(progress=100)
                self.log_line(
                    f"[bold green]finished[/bold green] in {ev.seconds:.1f}s"
                )
                if ev.peak_spill_bytes:
                    self.log_line(
                        f"[yellow]peak RAM {human(ev.peak_memory_bytes)}, "
                        f"spilled {human(ev.peak_spill_bytes)} to disk.[/yellow] "
                        f"Raising Memory would trade disk I/O for RAM."
                    )
                else:
                    self.log_line(
                        f"[dim]peak RAM {human(ev.peak_memory_bytes)} — "
                        f"no spilling, the limit was never reached.[/dim]"
                    )
                rs = self.query_one("#results", DataTable)
                for name, n in ev.tables.items():
                    rs.add_row(name, f"{n:,}")
                if ev.tables and not any(ev.tables.values()):
                    # Every table empty. Without this the Results tab is a
                    # column of zeros with no explanation.
                    reason = getattr(self, "_empty_reason", "")
                    label.update("finished — but every result is empty")
                    self.log_line(
                        "[bold yellow]Every output table is empty.[/bold yellow] "
                        + (reason or "Check that the study's codes appear in "
                                     "this data — see the Log tab.")
                    )
            else:
                label.update("failed")
                self.log_line(f"[bold red]failed:[/bold red] {ev.error}")
                if ev.peak_spill_bytes:
                    self.log_line(
                        f"[dim]had spilled {human(ev.peak_spill_bytes)} to "
                        f"disk before failing.[/dim]"
                    )
                if "memory" in ev.error.lower():
                    self.query_one("#memory", Input).focus()


def main(study: str = "", indata: str = "", output: str = "") -> None:
    QRPApp(study, indata, output).run()
