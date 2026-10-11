# Putting a UI on this

Short answer: yes, and most of the work is not the UI.

## The part that matters

A UI needs four things the pipeline did not previously provide:

1. **Structured events, not printed strings.** You cannot render a
   progress bar from `print()`. A frontend needs to distinguish "stage 6
   of 10 started" from "row count 340,112" from "warning".
2. **Progress inside a stage.** Stage-level granularity is not enough
   when one stage runs for two minutes.
3. **Cancellation.** A UI with no stop button is a UI that lies about
   being interactive.
4. **Non-blocking execution.** If `run()` blocks the main thread, the
   interface freezes and cancellation is impossible anyway.

All four now exist, and none of them depend on a UI framework:

| Module | Role |
|---|---|
| `qrp/events.py` | Typed events (`StageStarted`, `StageProgress`, `RunFinished`, …) plus console and JSONL sinks |
| `qrp/engine.py` | Emits events; polls DuckDB for intra-query progress; `cancel()` |
| `qrp/runner.py` | `RunHandle` — background thread, event queue, cancel, result |
| `qrp/tui.py` | One frontend, consuming the above |

**This ordering is the actual recommendation.** Build the event layer
first and the UI becomes a swappable detail; build the UI first and the
observability stays shallow. That is roughly what happened to the
PySpark package — 316 `print()` calls, one `logging` reference, and 30
`QRP_DUMP_*` environment variables doing the work a UI would want to do
properly.

## What DuckDB gives you (verified, not assumed)

Both of these were tested against real queries before anything was built
on them:

* **Live intra-query progress.** `con.query_progress()` returns a
  percentage for the query currently running on that connection. A
  polling thread sees a clean 0→100 during parquet scans, joins and
  aggregations. Requires `SET enable_progress_bar = true`, plus
  `SET enable_progress_bar_print = false` — otherwise DuckDB writes its
  own bar to stdout and the carriage returns corrupt a TUI.
* **Real cancellation.** `con.interrupt()` raises `InterruptException` in
  the thread blocked on `execute()`. Measured: a run on the 2m-patient
  dataset stopped **0.3 s** after the keypress, mid-query.

Two caveats worth knowing. Progress sits at 0 for operators that cannot
estimate cardinality, so treat it as advisory and never as a completion
signal. And progress is per-connection — poll the connection running the
query, not a cursor derived from it.

## The four approaches

| | Deploy | Real-time | Over SSH | Charts | Effort |
|---|---|---|---|---|---|
| **CLI + `rich`** | pip | good | yes | no | hours |
| **Textual TUI** | pip | good | yes | sparklines only | 1–2 days |
| **Streamlit** | pip + port | awkward | no | yes | 1–2 days |
| **FastAPI + HTMX/SSE** | pip + port | excellent | port-forward | yes | 1–2 weeks |

### Textual TUI — what I built

No open port, no browser, no localhost binding, no security review of a
bundled web server, and it works unchanged over SSH into a locked-down
analysis VM — which is frequently how these runs actually happen at a
Data Partner site. `pip install textual` is the whole deployment story.

Honest downside: less approachable for an epidemiologist who has never
used a terminal, and no real charting.

### Streamlit — the friendliest fast option

Genuinely quick to build and the most familiar to analysts. The problem
is the execution model: Streamlit reruns the whole script on every
interaction, so a long-running job needs `st.session_state` plus a
background thread anyway, and live log streaming fights the framework
rather than using it. The `RunHandle.drain()` method exists specifically
for this polling style. Also needs a port, which at some sites means a
conversation with IT.

Reach for this if the audience is non-technical and the environment is
permissive.

### FastAPI + HTMX or a small JS frontend

The right answer if this ever becomes multi-user, needs a run history, or
must be driven remotely. Server-Sent Events map onto the event stream
almost exactly — `events.py` was designed with that in mind. It is also
several times the work, and a single-analyst local tool does not need it.

### CLI + `rich`

Worth mentioning because it is nearly free: a `rich.progress` sink is
~30 lines against the existing event stream and gives a live progress
bar in an ordinary terminal, with no new architecture. If the TUI feels
like too much, start here.

## What the TUI does today

```bash
pip install -e ".[ui]"
python -m qrp ui --study study/demo_full.json --indata /tmp/qrp_data/100k
```

* Study file, SCDM root, output directory as editable fields
* **Threads** and **memory limit** as dropdowns (`auto`, `1`–`16`;
  `auto`, `2GB`–`64GB`) — passed straight to DuckDB's `threads` and
  `memory_limit` settings
* **Inspect** (`ctrl+t`) runs the input-file and SCDM probes without
  starting a job, so a mis-shaped file surfaces in seconds rather than
  three stages in
* **Run** (`ctrl+r`) with a live per-stage progress bar
* **Escape** cancels, mid-query
* Tabs: scrolling log, per-stage timing table, final row counts
* `load_study` warnings (unimplemented tables, unresolved names) are
  routed into the log instead of stderr, where a TUI user would never
  see them

## Testing a UI

The TUI is driven headlessly through Textual's `Pilot` in
`tests/test_ui.py`, which is worth doing because it caught two bugs a
manual click-through would have missed:

* **Keybindings were dead.** Plain letters (`r` for Run) were swallowed
  as text whenever a path Input had focus. Fixed with Ctrl-combinations
  and `priority=True`; there is now a regression test that types "run"
  into a field and asserts no run starts.
* **The JSONL run log was incomplete.** `RunStarted` and `RunFinished`
  were being put directly on the queue, bypassing extra sinks — so the
  machine-readable log was missing exactly the two records you want when
  asking why a run was slower than last week.

There is also a test asserting that a sink which raises cannot fail a
run. A UI bug should never take down a five-hour job.

## Suggested order

1. **Ship the event layer and the JSONL run log now.** Useful with no UI
   at all: `run.jsonl` makes "which stage regressed" answerable without
   re-running anything.
2. **Use the TUI** while the real question is still parity and
   performance. It is enough for the people doing that work.
3. **Decide on a web UI once you know the audience.** If analysts who
   have never opened a terminal are running studies unsupervised,
   Streamlit is a weekend. If it needs to be multi-user with run
   history, budget properly for FastAPI.

Do not start at step 3.
