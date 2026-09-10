"""
Tests for the event stream, the background runner, and the TUI.

The TUI tests drive the real app headlessly through Textual's `Pilot`.
That matters: they already caught a bug a manual click-through would
have missed — plain-letter keybindings (`r` for Run) were being
swallowed as text while a path Input had focus, so the shortcuts
silently did nothing.
"""

from __future__ import annotations

import asyncio
import time
import warnings
from pathlib import Path

import pytest

from qrp import load_study, run
from qrp.events import (
    RunFinished,
    RunStarted,
    StageFinished,
    StageProgress,
    StageStarted,
    jsonl_sink,
)
from qrp.runner import RunHandle

STUDY = Path(__file__).resolve().parents[1] / "study" / "demo_full.json"
SMALL = Path("/tmp/qrp_data/100k")
BIG = Path("/tmp/qrp_data/2m")

pytestmark = pytest.mark.skipif(
    not SMALL.exists(), reason="run tools/gen_synthetic.py first"
)


@pytest.fixture(scope="module")
def study():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_study(STUDY)


# ---------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------


def test_event_stream_is_well_formed(study):
    """Every stage must emit exactly one start and one finish, in order."""
    h = RunHandle(study, SMALL, threads=1).start()
    starts, finishes, progress = [], [], 0
    started = finished = None

    for ev in h.events():
        if isinstance(ev, RunStarted):
            started = ev
        elif isinstance(ev, StageStarted):
            starts.append(ev.name)
        elif isinstance(ev, StageProgress):
            progress += 1
        elif isinstance(ev, StageFinished):
            finishes.append(ev.name)
        elif isinstance(ev, RunFinished):
            finished = ev

    assert started is not None and finished is not None
    assert finished.ok
    assert starts == finishes, "start/finish events are not paired in order"
    assert list(started.stages) == starts, (
        "the declared plan must match what actually ran, or a progress "
        "bar sized from it will be wrong"
    )
    assert progress > 0, "no intra-query progress was reported"
    assert finished.tables["cohort_final"] > 0
    h.close()


def test_plan_is_known_before_the_run_starts(study):
    """A progress bar needs the total up front, not after the fact."""
    h = RunHandle(study, SMALL)
    plan = h.plan()
    assert "dose restrictions" in plan     # demo_full sets a dose limit
    assert "covariates" in plan
    assert plan[0] == "normalize"


def test_plan_omits_stages_config_disables():
    simple = load_study(STUDY.parent / "demo_type2.json")
    plan = RunHandle(simple, SMALL).plan()
    assert "dose restrictions" not in plan
    assert "covariates" not in plan


def test_progress_percentages_are_sane(study):
    h = RunHandle(study, SMALL, threads=1).start()
    pcts = [ev.percent for ev in h.events() if isinstance(ev, StageProgress)]
    assert pcts, "expected progress events"
    assert all(0.0 <= p <= 100.0 for p in pcts)
    h.close()


def test_jsonl_sink_writes_a_replayable_log(study, tmp_path):
    import json

    path = tmp_path / "run.jsonl"
    h = RunHandle(study, SMALL, threads=1, extra_sink=jsonl_sink(str(path)))
    h.start()
    list(h.events())
    h.close()

    rows = [json.loads(l) for l in path.read_text().splitlines()]
    kinds = {r["type"] for r in rows}
    assert {"StageStarted", "StageFinished", "RunFinished"} <= kinds
    assert all("at" in r for r in rows)


def test_broken_sink_does_not_fail_the_run(study):
    """A UI bug must never take down a five-hour job."""
    def explode(ev):
        raise RuntimeError("sink is broken")

    h = RunHandle(study, SMALL, threads=1, extra_sink=explode).start()
    finished = [ev for ev in h.events() if isinstance(ev, RunFinished)]
    assert finished and finished[0].ok
    h.close()


# ---------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------


@pytest.mark.skipif(not BIG.exists(), reason="needs the 2m dataset")
def test_cancel_stops_mid_query(study):
    """Cancellation must interrupt the running query, not wait it out."""
    h = RunHandle(study, BIG, threads=1).start()
    time.sleep(3.0)
    assert h.running
    t0 = time.time()
    h.cancel()
    assert h.wait(timeout=30), "run did not stop within 30s of cancel"
    elapsed = time.time() - t0
    assert elapsed < 10, f"cancel took {elapsed:.1f}s — not interrupting"

    finished = [ev for ev in h.drain() if isinstance(ev, RunFinished)]
    assert finished and finished[0].cancelled
    h.close()


def test_cancel_before_engine_exists_actually_stops_the_run(study):
    """A fast click must not be lost to a startup race.

    This test used to assert only `h.cancelled`, which is the flag the
    caller set — so it passed while the run went on to complete all ten
    stages and return full results. `interrupt()` called before any
    query is running is a silent no-op, so the flag alone stopped
    nothing.

    Assert the observable behaviour instead: no stage completes, and
    the run reports itself cancelled.
    """
    from qrp.events import StageFinished

    h = RunHandle(study, SMALL, threads=1)
    h.cancel()                      # before start(): no engine yet
    h.start()
    events = list(h.events())
    assert h.wait(timeout=120)

    finished = [e for e in events if isinstance(e, RunFinished)][0]
    assert finished.cancelled, "run did not report itself cancelled"
    assert not finished.ok
    stages = [e for e in events if isinstance(e, StageFinished)]
    assert not stages, f"{len(stages)} stage(s) ran after cancellation"
    h.close()


def test_cancel_between_stages_is_honoured(study):
    """Cancelling while no query is in flight must still stop the run.

    interrupt() only affects a running query; between stages there is
    none. The stage-boundary check is what covers this window.
    """
    from qrp.events import StageFinished

    h = RunHandle(study, SMALL, threads=1)
    h.start()
    seen = []
    for ev in h.events():
        seen.append(ev)
        if isinstance(ev, StageFinished) and ev.index == 2:
            h.cancel()
    finished = [e for e in seen if isinstance(e, RunFinished)][0]
    assert finished.cancelled
    completed = [e for e in seen if isinstance(e, StageFinished)]
    assert len(completed) < len(h.plan()), "run completed despite cancel"
    h.close()


def test_cancel_is_idempotent(study):
    h = RunHandle(study, SMALL, threads=1).start()
    h.cancel()
    h.cancel()
    assert h.wait(timeout=120)
    h.close()


# ---------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------

textual = pytest.importorskip("textual")


def _run_app(coro):
    return asyncio.run(coro())


def test_tui_runs_a_study_to_completion():
    from textual.widgets import DataTable

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp(str(STUDY), str(SMALL))
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+r")
            for _ in range(300):
                await pilot.pause()
                await asyncio.sleep(0.2)
                if not app.running and app.query_one(
                    "#stages", DataTable
                ).row_count:
                    break
            stages = app.query_one("#stages", DataTable)
            results = app.query_one("#results", DataTable)
            assert stages.row_count == len(
                RunHandle(app.study, str(SMALL)).plan()
            )
            assert results.row_count > 0
            assert not app.running

    _run_app(scenario)


def test_tui_shortcuts_work_while_an_input_has_focus():
    """Regression: plain-letter bindings were eaten by the Input.

    Ctrl-combinations with priority=True fire regardless of focus, and
    typing into a path field must not trigger a run.
    """
    from textual.widgets import Input

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp(str(STUDY), str(SMALL))
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#study", Input).focus()
            await pilot.pause()
            await pilot.press("r", "u", "n")     # plain letters = text
            await pilot.pause()
            assert not app.running, "typing 'run' should not start a run"
            assert app.query_one("#study", Input).value.endswith("run")

    _run_app(scenario)


def test_tui_requires_both_paths():
    from textual.widgets import RichLog

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp("", "")
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert not app.running
            text = " ".join(str(l) for l in app.query_one("#log", RichLog).lines)
            assert "required" in text

    _run_app(scenario)


def test_tui_inspect_reports_without_running():
    from textual.widgets import DataTable, RichLog

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp(str(STUDY), str(SMALL))
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()
            await asyncio.sleep(2.0)
            log = app.query_one("#log", RichLog)
            assert len(log.lines) > 5
            assert app.query_one("#stages", DataTable).row_count == 0
            assert not app.running

    _run_app(scenario)


# ---------------------------------------------------------------------
# Memory, spilling and host detection
# ---------------------------------------------------------------------


def test_memory_telemetry_is_reported(study):
    """Spilling must be observable, or a slow run is unexplainable."""
    from qrp.events import MemoryStatus

    h = RunHandle(study, SMALL, threads=1, memory_limit="500MB").start()
    stats = [ev for ev in h.events() if isinstance(ev, MemoryStatus)]
    assert stats, "no memory telemetry emitted"
    assert all(m.used_bytes >= 0 for m in stats)
    assert stats[-1].limit_bytes > 0, "memory limit was not resolved to bytes"
    h.close()


@pytest.mark.skipif(not BIG.exists(), reason="needs the 2m dataset")
def test_spilling_is_detected_and_reported():
    """A tight limit on a large dataset must surface as spill, not silence."""
    from qrp.events import MemoryStatus, RunFinished

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = load_study(STUDY)
    h = RunHandle(s, BIG, threads=1, memory_limit="600MB",
                  temp_directory="/tmp/qrp_spill_test",
                  database="/tmp/qrp_spill_test.duckdb").start()
    events = list(h.events())
    spilled = [e for e in events if isinstance(e, MemoryStatus) and e.spilling]
    finished = [e for e in events if isinstance(e, RunFinished)][0]
    assert spilled, "expected spilling under a 600MB limit on 2m patients"
    assert finished.peak_spill_bytes > 0
    h.close()


def test_oom_produces_actionable_advice():
    """A raw OutOfMemoryException tells the user nothing they can act on.

    DuckDB spills, but not without a minimum working set — measured on
    the 2m study, 400MB fails and 512MB succeeds. The message must name
    the setting to change.
    """
    OOM = type("OutOfMemoryException", (Exception,), {})
    msg = RunHandle._explain(
        OOM("Out of Memory Error: failed to pin block of size 256KiB")
    )
    assert "--memory-limit" in msg and "--temp-dir" in msg
    # and the real DuckDB exception type, not just a look-alike
    import duckdb

    msg2 = RunHandle._explain(
        duckdb.OutOfMemoryException("Out of Memory Error: failed to pin block")
    )
    assert "--memory-limit" in msg2


def test_memory_limit_parses_to_bytes():
    from qrp import Engine

    for given, low, high in [
        ("512MB", 500e6, 550e6),
        ("2GB", 1.9e9, 2.2e9),
    ]:
        e = Engine(memory_limit=given, verbose=False)
        assert low < e._limit_bytes < high, f"{given} -> {e._limit_bytes}"
        e.close()


def test_host_detection_respects_container_limits():
    """Suggesting 64GB inside a 4GB container would be worse than useless."""
    from qrp.sysinfo import HostInfo, memory_choices, suggest_memory_limit

    h = HostInfo.detect()
    assert h.cpus >= 1
    if h.memory_bytes:
        gb = h.memory_bytes / 1000**3
        assert gb < 2000, "implausible RAM — cgroup limit not honoured?"
        offered = [c for c in memory_choices() if c != "auto"]
        assert all(int(c.rstrip("GB")) <= gb for c in offered)
    assert suggest_memory_limit(8 * 10**9) == "4GB"
    assert suggest_memory_limit(0) == "auto"


def test_tui_exposes_memory_as_free_text():
    """A fixed dropdown cannot express 384GB or 6GB."""
    from textual.widgets import Input, Select

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp(str(STUDY), str(SMALL))
        async with app.run_test(size=(126, 48)) as pilot:
            await pilot.pause()
            mem = app.query_one("#memory", Input)
            assert mem.value, "memory should default to a host-derived value"
            mem.value = "384GB"
            await pilot.pause()
            assert app.query_one("#memory", Input).value == "384GB"
            assert app.query_one("#storage", Select).value in ("mem", "disk")
            app.query_one("#tempdir", Input)

    _run_app(scenario)


# ---------------------------------------------------------------------
# Run logs
# ---------------------------------------------------------------------


def test_runlog_writes_both_files(study, tmp_path):
    import json

    from qrp import Engine
    from qrp.events import console_sink, multi_sink
    from qrp.runlog import RunLog

    rl = RunLog(tmp_path, run_id=study.run_id)
    rl.header(study, SMALL, {"threads": 1, "memory_limit": "1GB"})
    eng = Engine(threads=1, memory_limit="1GB", verbose=False,
                 on_event=multi_sink(console_sink(False), rl.sink()))
    run(study, SMALL, engine=eng, verbose=False)
    rl.stage_summary(eng)
    rl.close()
    eng.close()

    text = rl.log_path.read_text()
    # provenance: what was asked for, not only what happened
    assert study.run_id in text
    assert "duckdb" in text and "python" in text
    assert str(SMALL) in text
    for c in study.cohorts:
        assert c.cohortgrp in text
    # outcome
    assert "OK in" in text
    assert "peak RAM" in text
    assert "stage timings" in text
    assert "cohort_final" in text

    rows = [json.loads(l) for l in rl.jsonl_path.read_text().splitlines()]
    kinds = {r["type"] for r in rows}
    assert {"Header", "RunStarted", "StageFinished", "RunFinished"} <= kinds


def test_runlog_captures_warnings(tmp_path):
    """Load-time warnings must reach the file, not only stderr.

    These are exactly the warnings you want when a parity comparison
    disagrees — 'this study supplies inclusioncodes, which we ignore'.
    """
    from qrp import load_study
    from qrp.runlog import RunLog

    rl = RunLog(tmp_path, run_id="warntest").capture()
    try:
        load_study(STUDY.parent / "realistic_inputfile.json")
    finally:
        rl.close()
    text = rl.log_path.read_text()
    assert "WARNING" in text
    # riskscorefile is named in qrp_parameters but absent from the JSON.
    # (inclusioncodes used to appear here; it is now implemented.)
    assert "riskscorefile" in text


def test_runlog_records_spill_transitions(study, tmp_path):
    """The log must name spilling, not just imply it via slowness."""
    from qrp.events import MemoryStatus
    from qrp.runlog import RunLog

    rl = RunLog(tmp_path, run_id="spill")
    sink = rl.sink()
    sink(MemoryStatus(used_bytes=400_000_000, spilled_bytes=0,
                      limit_bytes=400_000_000))
    sink(MemoryStatus(used_bytes=400_000_000, spilled_bytes=120_000_000,
                      limit_bytes=400_000_000))
    sink(MemoryStatus(used_bytes=400_000_000, spilled_bytes=130_000_000,
                      limit_bytes=400_000_000))
    sink(MemoryStatus(used_bytes=100_000_000, spilled_bytes=0,
                      limit_bytes=400_000_000))
    rl.close()
    text = rl.log_path.read_text()
    assert text.count("SPILLING") == 1, "should log the transition, not every sample"
    assert "spilling stopped" in text


def test_cli_and_ui_emit_the_same_events(study):
    """Regression: only the UI path used to emit RunStarted/RunFinished.

    A --log-dir run was therefore missing its header line, its outcome,
    and its output table counts, and stage lines read '[3/0]'.
    """
    from qrp import Engine
    from qrp.events import RunFinished, RunStarted, StageStarted

    seen = []
    eng = Engine(threads=1, verbose=False, on_event=seen.append)
    run(study, SMALL, engine=eng, verbose=False)
    eng.close()

    started = [e for e in seen if isinstance(e, RunStarted)]
    finished = [e for e in seen if isinstance(e, RunFinished)]
    assert started and finished, "pipeline.run must emit run-level events"
    assert len(started[0].stages) > 0
    for ev in (e for e in seen if isinstance(e, StageStarted)):
        assert ev.total == len(started[0].stages), "stage totals must be set"
    assert finished[0].tables.get("cohort_final", 0) > 0


def test_compare_runs_tool(study, tmp_path):
    import subprocess
    import sys

    from qrp import Engine
    from qrp.runlog import RunLog

    for _ in range(2):
        rl = RunLog(tmp_path, run_id="cmp")
        rl.header(study, SMALL, {})
        eng = Engine(threads=1, verbose=False, on_event=rl.sink())
        run(study, SMALL, engine=eng, verbose=False)
        rl.close()
        eng.close()

    tool = Path(__file__).resolve().parents[1] / "tools" / "compare_runs.py"
    out = subprocess.run([sys.executable, str(tool), str(tmp_path)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "TOTAL" in out.stdout and "normalize" in out.stdout


def test_statement_level_detail_is_reported(study):
    """Stages must report per-statement rows and columns.

    Previously each .sql file ran as one call, so a 30-second stage
    logged the word "script" and nothing else — no row counts, no way
    to tell which statement was slow.
    """
    from qrp import Engine
    from qrp.events import StatementFinished

    seen = []
    eng = Engine(threads=1, verbose=False, on_event=seen.append)
    run(study, SMALL, engine=eng, verbose=False)
    eng.close()

    stmts = [e for e in seen if isinstance(e, StatementFinished) and e.target]
    assert len(stmts) > 20, f"only {len(stmts)} statements reported"
    by_target = {s.target: s for s in stmts}
    # the POV1 funnel must be visible as three narrowing steps
    for name in ("_pov1_demog", "_pov1_enrolled", "pov1"):
        assert name in by_target, f"{name} not reported"
    assert (by_target["_pov1_demog"].rows
            >= by_target["_pov1_enrolled"].rows
            >= by_target["pov1"].rows), "POV1 funnel is not narrowing"
    # tables report rows and columns; views report columns only
    assert by_target["pov1"].rows > 0 and by_target["pov1"].columns > 10
    assert by_target["covar_source"].rows == -1, "views must not be counted"


def test_statement_detail_can_be_disabled(study):
    from qrp import Engine
    from qrp.events import StatementFinished

    seen = []
    eng = Engine(threads=1, verbose=False, statement_detail=False,
                 on_event=seen.append)
    run(study, SMALL, engine=eng, verbose=False)
    assert eng.count("cohort_final") > 0
    eng.close()
    assert not [e for e in seen if isinstance(e, StatementFinished)]


def test_sql_splitter_handles_comments_and_strings():
    """Naive splitting on ';' is wrong here.

    An audit found semicolons inside `--` comments in 7 of 11 SQL files
    and inside string literals in 8 of 11.
    """
    from qrp.sqlsplit import split_statements

    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a AS SELECT ';' AS s, 1 AS n;
    /* block; comment */
    CREATE VIEW b AS SELECT * FROM a WHERE s = ';';
    DROP TABLE a;
    """
    st = split_statements(sql)
    assert [s.kind for s in st] == ["TABLE", "VIEW", "DROP"]
    assert [s.target for s in st][:2] == ["a", "b"]


def test_every_shipped_sql_file_splits_cleanly():
    from qrp.sqlsplit import split_statements

    sql_dir = Path(__file__).resolve().parents[1] / "src" / "qrp" / "sql"
    total = 0
    for path in sorted(sql_dir.glob("*.sql")):
        stmts = split_statements(path.read_text())
        assert stmts, f"{path.name} produced no statements"
        for s in stmts:
            assert s.sql.strip(), f"{path.name} produced an empty statement"
        total += len(stmts)
    assert total > 40, f"expected 40+ statements across the package, got {total}"


# ---------------------------------------------------------------------
# Browse dialog
# ---------------------------------------------------------------------


def test_browse_opens_and_applies_a_selection():
    """Textual has no native file dialog; this is DirectoryTree in a modal."""
    from textual.widgets import DirectoryTree, Input

    from qrp.browse import BrowseScreen
    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp("", "")
        async with app.run_test(size=(126, 50)) as pilot:
            await pilot.pause()
            await pilot.click("#browse_indata")
            await pilot.pause()
            await asyncio.sleep(0.5)
            assert isinstance(app.screen, BrowseScreen)
            app.screen.query_one("#tree", DirectoryTree)

            app.screen.query_one("#path", Input).value = str(SMALL)
            await pilot.pause()
            await pilot.click("#choose")
            await pilot.pause()
            await asyncio.sleep(0.4)
            assert app.query_one("#indata", Input).value == str(SMALL)

    _run_app(scenario)


def test_browse_cancel_leaves_the_field_untouched():
    from textual.widgets import Input

    from qrp.tui import QRPApp

    async def scenario():
        app = QRPApp("keepme.json", "")
        async with app.run_test(size=(126, 50)) as pilot:
            await pilot.pause()
            await pilot.click("#browse_study")
            await pilot.pause()
            await asyncio.sleep(0.5)
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.3)
            assert app.query_one("#study", Input).value == "keepme.json"

    _run_app(scenario)


def test_browse_starts_from_an_existing_ancestor():
    """A half-typed path must not open an empty dialog."""
    from qrp.browse import BrowseScreen

    screen = BrowseScreen(start=str(SMALL / "does" / "not" / "exist"))
    assert Path(screen._start).exists()


def test_browse_hides_non_matching_files():
    from qrp.browse import _Tree

    # Call the filter unbound: instantiating a DirectoryTree outside a
    # running app leaves an un-awaited watch_path coroutine.
    tree = object.__new__(_Tree)
    tree._dirs_only = False
    tree._suffixes = (".json",)
    paths = [SMALL / "a.json", SMALL / "b.parquet", SMALL / ".hidden.json"]
    kept = {p.name for p in _Tree.filter_paths(tree, paths)}
    assert kept == {"a.json"}
