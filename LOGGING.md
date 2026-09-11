# Run logs

Every run can write two files:

```
<log-dir>/<run_id>_<timestamp>.log      human-readable
<log-dir>/<run_id>_<timestamp>.jsonl    one JSON event per line
```

```bash
python -m qrp run --study s.json --indata scdm/ --log-dir logs/
python -m qrp run ... --log-dir logs/ --no-jsonl     # text only
```

In the TUI there is a **Log dir** field, defaulting to `logs`. Leave it
blank to disable.

The two files are complementary. The `.log` is what you attach to a
parity report or a bug. The `.jsonl` is what answers "which stage got
slower since last week" across many runs.

## What the .log contains

A provenance header — **what was asked for**, not only what happened,
because a log that omits the settings is half useless six weeks later:

```
QRP Type 2 run — mpl1r
environment
  qrp          dev
  duckdb       1.5.5
  python       3.12.3
  platform     Linux-6.18.44-...
inputs
  indata       /tmp/qrp_data/500k
  period       2011-01-01 to 2015-06-30
engine settings
  threads      auto
  memory_limit 600MB
  temp_dir     /tmp/qspill3
  database     /tmp/l.duckdb
cohorts (2)
  lisinopril: washout=183 gap=15F minepis=1 maxepis=365 fupwash=183 ...
  beta_blocker: washout=183 gap=30F ...
covariates (12)
     1 cov_01   DX [-365,-1] dateonly=True codes=40
```

Then the run itself, spill transitions included:

```
  [3/10] exposure + stockpiling …
  SPILLING — RAM 599MB of 600MB limit, 8MB now on disk
  [3/10] exposure + stockpiling — 31.031s, script
  spilling stopped
------------------------------------------------------------
OK in 115.94s
peak RAM 600MB, spilled 243MB to disk
output tables
  cohort_final                    1,316,063
  ptsmasterlist                   1,453,785
```

Ending with the per-stage timing table.

Spilling is logged as a **transition**, not per sample — memory is polled
several times a second, and writing every sample would bury everything
else. The `.jsonl` keeps them all.

## Things that used to escape

Three sources of information were previously terminal-only. `RunLog.capture()`
routes all three into the file:

* `warnings.warn` from `load_study` — the unimplemented-table and
  unresolved-name warnings — went to stderr. These are exactly what you
  want when a parity comparison disagrees.
* `print()` from the console sink went to stdout.
* stdlib `logging` records from any library went nowhere.

## A bug this found

Adding the log surfaced a real inconsistency: `RunStarted` and
`RunFinished` were emitted by `RunHandle` (the UI path) but not by
`pipeline.run` (the CLI path). So a `--log-dir` run was missing its
header line, its outcome, and its output table counts, and stage lines
read `[3/0]` because the stage total was never set.

Both now come from `pipeline.run`, with `plan_stages()` as the single
source for the stage list. `test_cli_and_ui_emit_the_same_events` pins it
so the paths cannot drift again.

## Comparing runs

```
$ python tools/compare_runs.py logs/ --last 3

                                20260829_044739  20260829_044451  20260829_044525
---------------------------------------------------------------------------------
normalize                                 0.09s      1.28s +1267%      3.98s +4158%
exposure + stockpiling                    0.68s      8.44s +1147%     31.03s +4485%
covariates                                0.40s      2.16s + 438%      9.11s +2167%
---------------------------------------------------------------------------------
TOTAL                                     3.82s           29.17s          115.94s
peak RAM                                 215MB           420MB           600MB
spilled                                    0MB             0MB           243MB
outcome                                      ok               ok               ok
```

(Those three runs are 100k, 500k and 2m patients, so the percentages are
scale, not regression — but that is the shape a regression would take.)

## Programmatic use

```python
from qrp import Engine, load_study, run
from qrp.runlog import RunLog

study = load_study("study.json")
with RunLog("logs", run_id=study.run_id).capture() as rl:
    rl.header(study, "scdm/", {"memory_limit": "8GB"})
    eng = Engine(memory_limit="8GB", verbose=False, on_event=rl.sink())
    run(study, "scdm/", engine=eng, verbose=False)
    rl.stage_summary(eng)
```

`RunLog.sink()` is an ordinary `EventSink`, so it composes with any other
via `multi_sink` — console, log file and a UI at once.
