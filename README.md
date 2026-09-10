# qrp-duckdb — Sentinel QRP Type 2 on DuckDB

A ground-up implementation of the QRP Type 2 pipeline, written the way
DuckDB wants to be written rather than transcribed from the SAS
architecture.

**New here?** Analysts start with **[docs/RUNBOOK.md](docs/RUNBOOK.md)**;
developers with **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — a
plain-language, five-step guide for an analyst at a Data Partner site.
For security review, **[docs/SECURITY.md](docs/SECURITY.md)**.

```bash
python3 -m venv .venv && source .venv/bin/activate   # see note below
pip install -e ".[dev]"

python -m qrp validate --study study/demo_type2.json
python -m qrp run --study study/demo_type2.json \
                  --indata /path/to/cdm_parquet \
                  --out results/

# Outputs split into dplocal/ (patient-level, stays local) and msoc/
# (aggregate, shareable), named <runid>_<table>. --layout flat for one
# directory.
python -m qrp show --out results/                 # list result tables
python -m qrp show --out results/ attrition       # read one

pytest                       # 75 tests

# A virtual environment is required, not optional: modern Linux and
# Homebrew mark the system Python "externally managed" (PEP 668) and a
# bare `pip install` is refused. `pipx install qrp-duckdb` also works.
python tools/gen_synthetic.py --out /tmp/qrp_data/100k --patients 100000
python tools/bench.py --scales 100k 500k 2m
```

## What this is and isn't

**Is:** the Type 2 spine, end to end and running — normalize, enrollment
spans, exposure extraction, stockpiling, dose restrictions, washout/index
dates, POV1, episode construction, censoring, patient master list,
follow-up washout, event attribution, baseline covariate detection,
attrition and stratified denominators.

**Isn't:** risk scores, utilization, code distribution, combo covariates
(`codecat='CC'`), lab result extras, or the Type 1/3/4/5/6 branches.
Those are additive — they attach to `cohort_final` or `covariates_long` —
but they aren't here.

The point is to make the architecture concrete and measurable, not to
retire the PySpark package on the strength of one file.

## Measured

Synthetic CDM, 2 cohorts, on **1 CPU core / 3 GB RAM**.

`demo_type2.json` — spine only:

| patients | input | wall clock | throughput | final episodes |
|---|---|---|---|---|
| 100k | 15.9 MB | 2.40 s | 6.6 MB/s | 70,383 |
| 500k | 79.1 MB | 12.51 s | 6.3 MB/s | 349,878 |
| 2m | 316.0 MB | 56.20 s | 5.6 MB/s | 1,402,970 |

`demo_full.json` — adds dose restrictions and 12 baseline covariates:

| patients | input | wall clock | throughput | final episodes |
|---|---|---|---|---|
| 100k | 16.3 MB | 4.24 s | 3.8 MB/s | 68,845 |
| 500k | 79.1 MB | 21.59 s | 3.7 MB/s | 349,878 |
| 2m | 316.0 MB | 120.63 s | 2.6 MB/s | 1,402,970 |

The spine scales linearly across a 20× range. The full study is mildly
superlinear (3.8 → 2.6 MB/s), and the cause is visible in the stage
table: covariate detection is ~45% of runtime and is a range join whose
cost grows with both episodes and covariate count. That is the stage to
watch as covariate counts rise toward the realistic few hundred.

The constraint worth noting: this ran on **one core**. DuckDB parallelises
hash joins, aggregation and sorts, so these are a floor rather than a
representative number.

Treat the comparison to the PySpark figures (0.4 GB in 333 s on a 28 GB
multi-core machine) as indicative only. The workloads are not identical:
different data, and this covers the spine rather than every covariate
stage. It is a reason to run the real bake-off, not a substitute for it.

## The six design decisions

### 1. Config is data, so there is no cohort loop

The input JSON is parsed once into frozen dataclasses (`config.py`),
validated up front, then registered as DuckDB tables (`cfg_cohort`,
`cfg_codes`, `cfg_age_strata`, `cfg_demog`, `cfg_enrollment`). The SQL
joins to those tables.

Because cohort membership is a *column*, every cohort is evaluated in the
same pass. That removes, together: the per-cohort re-scan of shared claim
tables, the `proc append` write/read round trip through `<dplocal>/mstr`,
the INT-vs-DOUBLE schema drift that round trip caused, and the
cross-iteration accumulator variables.

It also enables a saving that a per-cohort loop cannot express: cohorts
sharing `(coverage, enrol_gap, chart_required)` share one enrollment
build, deduplicated by `enr_cfg_id`.

### 2. Branch decisions never touch data

`CohortConfig.needs_dose`, `needs_stockpiling`, `needs_fup_wash` are
properties over config. The PySpark port answered the same questions with
`df.limit(1).count() > 0` — six such probes per cohort iteration against
small config tables, each one a full Spark job. Config questions get
config answers.

### 3. Stockpiling in closed form

The SAS algorithm is sequential — push each dispensing forward so
supplies never overlap:

```
s(1) = a(1);   e(i) = s(i) + r(i) - 1;   s(i) = max(a(i), e(i-1) + 1)
```

The PySpark port drove the classification with `F.udf(...)`, forcing a
JVM→Python round trip per row and blocking pushdown. But the recurrence
has an exact closed form. With `C(i)` the running sum of supply:

```
e(i) = C(i) - 1 + max over j<=i of ( a(j) - C(j-1) )
```

A cumulative sum and a running max — two ordered window functions over
one sort, fully vectorised, no UDF and no recursion. Proven by induction,
and tested against the sequential reference on 200 random patients
(`tests/test_rewrites.py`).

### 4. Washout is a running max, not a range self-join

`%ms_findgap` applies the washout twice: a LAG, then a range self-join
against *all* claims via `%ms_periodsoverlap`. The port reproduced both.

But a candidate index date `a` can only ever be blocked by a *prior*
claim (claims at or after `a` fail `inc.adate <= a - 1`), and among prior
claims the condition reduces to `expiredt >= a - washout` — which holds
for some prior claim exactly when it holds for the one with the latest
`expiredt`. So:

```sql
keep iff prior_max_expiredt IS NULL OR prior_max_expiredt < adate - washout
```

One window function replaces a range self-join that fans out before it
filters. It also subsumes the LAG check, since the running max is always
≥ the immediately prior `expiredt`. Tested against an O(n²) brute-force
scan across five washout values.

### 5. One schema contract, established once

`10_normalize.sql` is the only file that touches raw CDM parquet. After
it, every downstream stage may assume lowercase names, DATE-typed dates,
and one demographic row per patient.

That single file replaces the 306 `x in df.columns` guards, the ~130
lowercase-map rebuilds, and the runtime-dtype branch in `ms_loopenc.py`
that existed because dates were sometimes `DateType` and sometimes raw
SAS day-numbers depending on call path — a live correctness hazard, not
just verbosity.

### 6. Covariates stay long; wide is a view

`ms_cidacov` was the stage that produced the documented Spark perf
regression. The port squared covariates into `covar1..covarN` columns as
the *primary* result — one column per covariate, so a few hundred
covariates give a very wide table that every downstream stage then
carries.

Here detection produces one row per `(cohort, patient, index, covarnum)`.
Long format joins and filters better, and adding a covariate becomes a
config change rather than a schema change. The wide form is a `PIVOT`
materialised only when something actually needs it.

The window bounds (`covfrom`/`covto`) come from the config table, so one
range join covers every covariate instead of a per-covarnum branch. NULL
bounds are resolved to sentinels *at config load*, not with a `COALESCE`
inside the join predicate — which is what keeps the predicate simple
enough for DuckDB to push down.

### 7. Materialisation is one policy, not 50 decisions

`Engine.stage()` decides what "materialise" means in one place, and
records rows and wall time for each stage. Compare the PySpark port's 50
hand-placed `localCheckpoint(eager=True)` calls, tuned by trial and error
to one dataset size, with a documented TODO conceding the approach had
stopped working above 2 GB.

DuckDB helps here: `CREATE OR REPLACE TABLE ... AS SELECT` is eager and
bounded — the automatic boundary SAS gave for free and Spark did not —
and larger-than-memory operations spill to disk rather than OOM.

## Other things DuckDB does better than the port's Spark

| Construct | Port | Here |
|---|---|---|
| `row_number()` then filter then drop | 3 statements | `QUALIFY` |
| Reusable predicates | Python fn returning `Column` | `CREATE MACRO`, inlined |
| Squared table shell | `ms_squaredtableshell.py` (568 lines) + cross join + broadcast | `GROUPING SETS` |
| "Most recent event before X" | 3 range self-joins + 3 anti-joins | one `ASOF JOIN` |
| Age banding | generated `CASE` ladder | range join on a config table |
| Dose lookback window | range self-join | `RANGE BETWEEN INTERVAL n DAY PRECEDING` |
| Column pruning | explicit `.select()` per stage | pushdown into `read_parquet` |

## Interactive UI

```bash
pip install -e ".[ui]"
python -m qrp ui --study study/demo_full.json --indata /tmp/qrp_data/100k
```

Works in Windows Terminal, VS Code, WSL, macOS and any Linux terminal.
**Git Bash and legacy `cmd.exe` are the exceptions** — use
`python -m qrp serve` there, which streams the identical app to a browser
with no code changes. See
**[docs/PERFORMANCE_AND_TERMINALS.md](docs/PERFORMANCE_AND_TERMINALS.md)**.

Screenshots (real `export_screenshot()` renders, not mockups) are in
[docs/screenshots/](docs/screenshots/) — including the spilling and
out-of-memory states.

A terminal UI with editable paths, a free-text Memory limit
(defaulted from detected host RAM, cgroup-aware), an in-memory/on-disk
Storage toggle, a spill directory, an Inspect button that checks inputs
without running, live per-stage progress, a live RAM/spill readout, and
Escape to cancel mid-query (measured: 0.3 s to stop).

The UI is a thin consumer of `qrp/events.py` and `qrp/runner.py` — a
typed event stream and a cancellable background runner. Any frontend
(TUI, Streamlit, FastAPI+SSE, or a plain `rich` progress bar) plugs into
the same interface without touching pipeline code. See
**[docs/UI.md](docs/UI.md)** for the framework comparison and why the
event layer comes first.

## Run logs

```bash
python -m qrp run --study s.json --indata scdm/ --log-dir logs/
```

Writes `<run_id>_<timestamp>.log` (human-readable, with a provenance
header recording every setting and cohort parameter) and `.jsonl` (one
event per line). Spill transitions, load-time warnings, stage timings and
output row counts all land in both. The TUI has a **Log dir** field.

```bash
python tools/compare_runs.py logs/ --last 3   # which stage regressed?
```

See **[docs/LOGGING.md](docs/LOGGING.md)**.

## Memory and larger-than-memory runs

```bash
python -m qrp run --study s.json --indata scdm/ --out results/ \
  --db /scratch/run.duckdb --memory-limit 4GB --temp-dir /scratch/spill
```

Measured on the 2m-patient study (316 MB input, 10 stages):

| Configuration | Time | Peak RSS |
|---|---|---|
| `:memory:`, no limit | 117 s | 3807 MB |
| `--db file --memory-limit 1GB` | 151 s | 1183 MB |
| `--db file --memory-limit 512MB` | 157 s | **687 MB** |

A 5.5× memory reduction for a 33% time cost. The memory floor is **flat
across scale** — 100k, 500k and 2m patients all complete at 160 MB,
because the floor is set by per-operator working sets rather than data
volume. Disk, not RAM, is the binding constraint at scale: at the floor
the pipeline spills roughly 2.6× the input size.

Find the floor on your own data before sizing a machine:

```bash
python tools/find_memory_floor.py --study s.json --indata scdm/ --curve
```

It binary-searches the limit and **names the stage that fails**.

## Input files and data

See **[INPUTS.md](INPUTS.md)** for the exact JSON and SCDM shapes
accepted, and run `python -m qrp inspect` to check a real file before a
run. Two things worth knowing up front:

* Real `create_json.sas` output keys tables by **SAS dataset name**, with
  `QRP_PARAMETERS` holding the logical→actual mapping. `qrp/inputfile.py`
  resolves that indirection; a loader that assumes literal `"cohortfile"`
  keys produces a silent zero-cohort study.
* **DuckDB identifiers are case-insensitive**, so `PatID`/`patid`/`PATID`
  all resolve. Only genuine name differences need aliasing.

## Packaging

```bash
./package.sh qrp-duckdb.zip
```

`.gitignore` is the single source of truth for exclusions — `git archive`
when the tree is a repository, `package.py` reading the same file when it
is not. Hand-maintained exclusion lists drift: an earlier archive shipped
a `.ruff_cache` because the list named the caches that existed when it
was written.

## Layout

```
src/qrp/
  config.py       typed config, validated at load
  inputfile.py    real QRP input file reader (qrp_parameters indirection)
  scdm.py         SCDM schema expectations + probe
  events.py       typed run events + console/JSONL sinks
  runner.py       background execution, progress, cancellation
  runlog.py       per-run .log and .jsonl, warning capture
  errors.py       exceptions -> actionable advice (CLI and UI share it)
  sqlsplit.py     statement splitter for per-statement logging
  show.py         read result tables without pandas
  tui.py          Textual terminal UI
  engine.py       DuckDB connection + stage boundary + timing
  pipeline.py     config -> tables, then 7 stages
  parity.py       dump in the existing harness's directory layout
  cli.py          argparse entry point
  sql/
    00_macros.sql      shared vocabulary
    10_normalize.sql   the schema contract
    20_enrollment.sql  episoderec2 + enrollment spans
    30_exposure.sql    extraction + closed-form stockpiling
    40_index.sql       findgap washout
    42_dose.sql        cumulative dose + CFDD (RANGE-framed windows)
    45_pov1.sql        demographics, age strata, enrollment
    50_episodes.sql    claim episodes + master list + censoring
    60_followup.sql    follow-up washout + events (ASOF)
    70_outputs.sql     attrition + denominators (GROUPING SETS)
    80_covariates.sql  covariate detection (long), prevalence
tools/
  gen_synthetic.py, bench.py, compare_runs.py, find_memory_floor.py
tests/
  test_rewrites.py     proves the two closed-form rewrites
  test_determinism.py  tie-breaking on constructed ties
  test_pipeline.py     thread-invariance, stage gating, invariants
  test_ui.py           event stream, cancellation, headless TUI
```

Roughly 1,700 lines total (900 Python, 500 SQL, 300 tests), against
~40,000 in the PySpark package —
though that ratio is unfair in both directions, since the port covers
more stages and this covers them more narrowly.

## Plugging into the existing parity harness

The harness compares dumped files, not DataFrames, so it is engine-
agnostic and works against this unchanged:

```bash
python -m qrp run --study study/demo_type2.json --indata <cdm> \
       --parity-dump <dplocal> --pt pt001 --iter 00
# then, unmodified:
tools/parity/run_parity_report.sh
```

`parity.py` writes to `<dplocal>/_dbg_<stage>/<pt>/<cohortgrp>/iter_<NN>/`
with deterministic ordering. This is what makes the bake-off cheap to
evaluate rather than an act of faith — and the harness is the single most
valuable thing in the current repository.

## Determinism, and a test that was lying

SAS resolves `BY`-group ties by physical row order. SQL guarantees
nothing unless the `ORDER BY` is a **total** order. A partial tie-break
does not fail loudly — it produces a different answer on a different
thread count or a different parquet row order, surfacing as an
unreproducible parity failure weeks later.

Every window in the pipeline now carries a total order, and
`test_pipeline.py` runs the same input under 1 and 4 threads and requires
byte-identical output across five tables.

**That test was initially worthless, and it is worth saying why.** When a
deliberately non-total `ORDER BY` was substituted into `40_index.sql`,
the invariance test still passed. The reason: the random generator
produced data with *zero ties*, so the tie-breaking clauses were never
executed. A test that cannot fail reads as evidence and isn't.

Two fixes, both in the repository:

- `tools/gen_synthetic.py` now injects the tie shapes that occur in real
  claims — duplicate demographic rows with the same birth date, and
  same-day same-supply dispensings with differing amounts.
- `tests/test_determinism.py` constructs ties directly and asserts both
  that the total order is stable *and* that it selects the documented row
  — a stable but wrong choice would pass an invariance check. It also
  asserts the fixture genuinely contains ties, so the file fails loudly
  if it ever stops testing anything.

One finding fell out of this: stockpiling's `ORDER BY adate` is already a
total order, because the same-day collapse in the preceding CTE
guarantees one row per `(cohort, patid, adate)`. The guarantee comes from
upstream rather than from the clause, so the SQL says so in a comment.

## Honest caveats

- **Semantics need verification against SAS.** The algorithms were read
  off the PySpark port and reasoned about, not validated against SAS
  output. The parity harness is how they become trustworthy; treat every
  stage as unverified until it passes.
- **Not every Type 2 option is wired.** `at_risk_start` and
  `episode_gap_type='P'` are represented in config and SQL but not
  covered by tests. Combo covariates, lab result extras, the
  `min_rx_days > 1` path and surveillance-mode carry-forward are absent.
- **Covariate detection is the scaling risk.** At 12 covariates it is
  45% of runtime. Realistic studies use far more, and the range join
  grows with both episode count and covariate count. If the real
  benchmark disappoints anywhere, expect it here first.
- **Synthetic benchmark data.** Real claims are skewed in ways this
  generator is not — patients with hundreds of dispensings stress window
  functions differently. Benchmark on real data before believing any
  number here.
