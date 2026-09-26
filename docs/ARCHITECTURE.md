# How it works — architecture and threading

## The threading question

Textual **does** need threads for background work, and this application
creates them. What Textual removes is the *bookkeeping*, not the
threads. Observed live during a run, there are four:

```
MainThread          Textual's event loop — draws, handles keys
asyncio_0           the @work(thread=True) event pump
Thread-1 (_work)    RunHandle: the pipeline
Thread-2 (poll)     Engine: progress and memory telemetry
```

If you looked at `tui.py` and saw no `threading.Thread(...)`, that is
because of one decorator:

```python
@work(thread=True, exclusive=True)
def _pump(self) -> None:
    for ev in self.handle.events():
        self.call_from_thread(self._apply, ev)
```

`@work(thread=True)` runs the method on a worker thread and tracks it,
so Textual can cancel it on exit and `exclusive=True` guarantees a
second run cannot start while one is in flight. That is the same manual
bookkeeping you would otherwise write, moved into a decorator.

### Why an async framework needs threads at all

Textual is built on asyncio, which is **concurrency without
parallelism**: one thread, cooperatively switching between tasks at
`await` points. That is ideal for I/O — a task waiting on a socket
yields and others run.

DuckDB is not I/O-bound in that sense. `con.execute()` is a blocking
call into compiled C++. There is no `await` inside it, so it cannot
yield, and running it on the event loop would freeze the interface for
the entire query — no repaints, no keystrokes, no cancel button.

So the work goes on a real thread. The saving grace is that DuckDB
**releases the GIL** while executing, so the UI thread genuinely runs in
parallel rather than fighting for the interpreter.

### The rule the whole design rests on

> Textual widgets may only be touched from the thread that owns the
> event loop.

Mutating a widget from a worker thread is a data race against the
renderer. So every UI update crosses back explicitly:

```python
self.call_from_thread(self._apply, ev)
```

`call_from_thread` schedules the call on the event loop and waits for
it. This is why `_apply` — which touches the progress bar, the log and
the tables — is the *only* place widgets are written, and why it never
runs on the worker.

### Why the pipeline is not just `@work`

`RunHandle` could have been another Textual worker. Keeping it a plain
`threading.Thread` behind a queue means **nothing below `tui.py` imports
Textual**. The same runner drives the CLI, a Streamlit app, a FastAPI
backend or a test, and the UI is genuinely swappable.

```
Engine ──emit(event)──▶ multi_sink ──▶ queue.Queue ──▶ RunHandle.events()
                            │                              │
                            └──▶ RunLog (.log/.jsonl)      └──▶ TUI / CLI
```

`queue.Queue` is the thread-safe handoff. The worker only ever `put`s;
consumers only ever `get`. No shared mutable state, so no locks.

### Threads, not processes

DuckDB releasing the GIL is what makes threads sufficient. Processes
would add IPC and put the result tables out of reach — they live in the
connection. The trade is that a hard crash takes the UI with it, which
for a local analysis tool is the right side to err on.

---

## Cancellation, and a bug this review found

Cancellation has two mechanisms because one is not enough.

`con.interrupt()` raises `InterruptException` in the thread blocked on
`execute()`. Measured: 0.3 s from keypress to stopped, mid-query.

But **`interrupt()` is a silent no-op when no query is running** — during
engine construction, or in the gap between two stages. The original code
relied on it alone, so:

```python
h = RunHandle(study, data)
h.cancel()      # before the engine exists
h.start()
# ... ran all ten stages and returned full results
```

The flag was set, `interrupt()` fired on an idle connection and did
nothing, and the pipeline proceeded to completion.

**The test did not catch it because the test asserted the flag, not the
behaviour** — `assert h.cancelled` was true the whole time. A test that
checks the thing you just set will always pass.

The fix is `Engine.raise_if_cancelled()`, called at every stage boundary
and between statements within a stage, raising `CancelledRun`. Now:

| | before | after |
|---|---|---|
| stages completed after early cancel | 10 | **0** |
| `RunFinished.cancelled` | `False` | `True` |

The tests now assert observable behaviour: no `StageFinished` events,
and the run reports itself cancelled. A second test covers cancelling
*between* stages, which is the window `interrupt()` cannot see.

---

## Data flow

```
study JSON ─▶ config.py ────────▶ frozen dataclasses ─▶ DuckDB config tables
                (validated once)                             │
SCDM parquet ─▶ scdm.py ────────▶ read patterns ─────────────┤
                (name, then columns)                         ▼
                                              pipeline.run ──▶ 10 SQL stages
                                                    │
                        events ◀───────────────────┘
                          ├──▶ console
                          ├──▶ RunLog  (.log + .jsonl)
                          └──▶ RunHandle queue ──▶ TUI
```

Two decisions shape everything else:

**Config is data, not string interpolation.** Study parameters become
rows in `cfg_cohort`, `cfg_codes`, `cfg_age_strata`, `cfg_demog`, and the
SQL joins to them. That is what lets every cohort run in one pass instead
of a Python loop, and it is why no untrusted string is ever concatenated
into SQL.

**Materialise on fan-out, stay lazy on single consumption.** A `TABLE` is
an optimiser barrier — right where several stages read the same result,
wrong for a staging step read once, where the copy is waste *and* the
barrier blocks filter pushdown. Getting one of those wrong cost 7.4× in
the covariates stage, and three more single-consumer temp tables
(`_sameday`, `_nochart`, `_spans`) were worth another ~4s at 2m
patients once folded into CTEs.
`test_no_single_consumer_temp_tables` now guards this.

The exception is the POV1 pair, materialised deliberately so that peak
memory is the largest hash table rather than the sum of ten. That is a
memory trade, and the memory-floor test is what keeps it honest.

**Measure, do not assume.** Covariate detection *looks* like a semi-join
— the question is "did any qualifying claim occur", never "how many" —
so `SELECT DISTINCT` over a join was rewritten as `WHERE EXISTS`. It was
**2.7× slower** (8.5s → 23.1s at 2m patients): DuckDB plans the flat
join with a hash aggregate well and does not turn a correlated subquery
containing a range predicate into anything as good. The join is left in
place with a comment saying so, because the "improvement" is an obvious
one for the next reader to try.

---

## Types and classes

Config objects are **frozen dataclasses** (`StudyConfig`, `CohortConfig`,
`InclusionRule`, `Covariate`): parsed once, validated once, immutable
after. Mutable state lives only where it must — `Engine` and `RunHandle`
are mutable dataclasses with `field(init=False)` for internals, and
`field(default_factory=...)` so each instance gets its own containers.

`EventSink` is a `TypeAlias` for `Callable[[Event], None]`, not a
Protocol. A Protocol whose only member is `__call__` is the same thing
written the long way, and mypy will not accept a plain function for it
without a cast.

`mypy` runs clean apart from third-party stub imprecision (DuckDB types
`.description` and `.fetchone()` as Optional even where a preceding
query guarantees a result; Textual's `query_one` returns the base
`Widget`). Configuration in `pyproject.toml` deliberately stops short of
strict mode: strict would bury the ~10 real findings under ~200 that say
nothing.

Three things the checker found that were worth fixing, all of them mine:

* **`_int()` returned `int | None`** while ~20 call sites passed a
  non-None default and assigned to an `int` field. Now overloaded, so
  the return type follows the default.
* **`column_aliases: dict = None  # type: ignore[assignment]`** — three
  faults in one line: untyped container, wrong default for the
  annotation, and a silenced checker instead of a fix. Every reader had
  to write `spec.column_aliases or {}`.
* **`tables` rebound from `list[str]` to `dict[str, int]`** in one
  function, and a `bar` local shadowing a `ProgressBar` handle. Neither
  was a runtime bug; both are the confusion a checker exists to catch.

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Parse and validate the study once, into frozen dataclasses |
| `inputfile.py` | Real QRP JSON, incl. the `qrp_parameters` name indirection |
| `scdm.py` | Locate SCDM tables by name, then by column signature |
| `engine.py` | DuckDB connection, stage boundaries, telemetry, cancellation |
| `pipeline.py` | Config → tables, then the stage sequence |
| `sql/*.sql` | The actual logic, 47 statements across 11 files |
| `events.py` | Typed events + console/JSONL sinks |
| `runner.py` | Background execution, cancellable, framework-agnostic |
| `runlog.py` | Per-run `.log` and `.jsonl`, warning capture |
| `errors.py` | Exceptions → advice, shared by CLI and UI |
| `show.py` | Read result tables (no pandas) |
| `tui.py` | The only module that imports Textual |

The dependency direction is one-way: `tui.py` → `runner.py` → `engine.py`
→ SQL. Nothing lower knows a UI exists.


---

## The stage table

`STAGES` is a tuple of `Stage(name, script, gate, skip_note)`.
`plan_stages()` filters it; `run()` executes it. Neither enumerates
stages independently, so they cannot disagree about which exist or in
what order.

They used to be two parallel sequences. `plan_stages()`'s docstring
claimed to be the single source of truth while `run()` went its own way,
and three stages added in one session each needed editing in both
places. One was missed — caught only because a UI test compared the two
counts.

`plan_stages()` is now 8 lines instead of 40, and adding a stage is one
tuple entry.

Two tests enforce the structure rather than the symptom:

* every stage names a SQL file that exists, and a gate that is a real
  `StudyConfig` property (a typo'd gate would mean the stage silently
  never runs)
* every numbered SQL file is referenced by some stage, so a file no
  stage runs is caught as dead code

Stages with side effects — a view to create, a rebuild to trigger,
generated SQL to pass — keep explicit code in `run()`; the table still
decides whether they run. `cida tables` is the only stage needing extra
SQL parameters, because its covariate-stratum columns come from
USERSTRATA and cannot be static.

## One declaration per output

`OUTPUTS` is a tuple of `Output(name, sas_name, library, contract, emit,
note)`. `OUTPUT_TABLES`, `OPTIONAL_OUTPUT_TABLES`, `SAS_NAMES`,
`DISCLOSURE`, `SAS_CONTRACT` and `SAS_NAME_NOTES` are all projections of
it.

They used to be six independent dicts over the same 25 tables, held in
agreement by tests. That is what produced the two worst output bugs
found in review: `mstr` named the wrong table, because nothing tied the
SAS name to the table that actually holds the finalised cohort; and
`geography` claimed a SAS name that does not exist anywhere in the macro
library, because `SAS_CONTRACT` had been populated from what this
package emits rather than from what SAS does.

**The constructor refuses a non-contract output with no note.** An
addition has to say what it is, or a reader cannot tell it from an
omission. That check found two outputs — `lab_results` and
`utilization` — that were neither verified as SAS names nor documented
as additions. `lab_results` may correspond to
`DPLocal.<runid>_Claims_lab`, but its columns have not been compared, so
it is recorded as unverified rather than claimed.

### `emit` is three-valued, not a boolean

An early version used `always: bool` and derived the optional list as
its complement. That was wrong: `OPTIONAL_OUTPUT_TABLES` means
specifically the COVARIATE-gated outputs, not everything that is not
always written, and the complement would have made every covariate study
try to write all twenty optional tables. `emit` is `"always"`,
`"covariate"` or `"gated"`.

## Parsers split out of `load_study_dict`

`load_study_dict` was 356 lines: one block per input table. Each block
was readable; together they were not.

The two largest are now module-level functions — `_parse_cohort_codes`
(COHORTCODES, the most consequential parser, and the one a review found
was dropping `codecat`) and `_parse_inclusions` (three levels of
nesting derived from character columns). The function is down to 259
lines and the remaining blocks are 5-20 lines each, which is about the
size at which a block stops being worth a name.

## Debt ledger

| debt | status |
|---|---|
| stage list duplicated between `plan_stages()` and `run()` | **fixed** — one `STAGES` table, `plan_stages()` 40 lines to 8 |
| output contract spread across six dicts | **fixed** — one `Output` per table, the dicts are projections |
| `pipeline.py` mixing orchestration with output concerns | **fixed** — `outputs.py`, 1,518 to 1,184 lines |
| `load_study_dict` at 356 lines | **fixed** — 259, two parsers extracted |
| `run()` at ~365 lines | open — mostly the side-effect stages, which resist a table |
| `config.py` at ~1,600 lines | open — but it is now a flat list of parsers and dataclasses, not a tangle |

Every refactor was verified the same way: outputs compared before and
after on the production study, and the full suite run. The one that
nearly went wrong — deriving `OPTIONAL_OUTPUT_TABLES` as the complement
of "always" — was caught by diffing the projections against a snapshot,
not by the tests.


## CLI and UI parity is tested

`build_parser()` constructs the CLI outside `main()` so it can be
inspected. `test_every_cli_run_option_reaches_the_ui_or_is_exempted`
compares every `run` option against `RunHandle`'s fields — `RunHandle`
being what the terminal UI drives.

Each option must either have a `RunHandle` field or appear in
`CLI_ONLY` **with a reason**. A second assertion rejects exemptions for
options that no longer exist, so the excuse list cannot rot.

It exists because `--table-map` and `--debug` both landed in the CLI
alone, and a data partner using the UI simply could not use them.
Writing it surfaced three more (`--csv`, `--layout`, `--names`). And it
caught `--no-text` the moment that flag was added, before `RunHandle`
had the field — which is the case it was built for.
