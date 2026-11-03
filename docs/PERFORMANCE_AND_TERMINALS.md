# Terminals, and lazy vs eager

## 1. Will the TUI work in any terminal?

Textual is a **library that draws inside an existing terminal** — it is
not its own terminal emulator, and it does not bundle one. So the answer
depends entirely on which terminal you launch it from, and the Windows
answer is genuinely mixed.

| Terminal | Verdict |
|---|---|
| Windows Terminal (PowerShell / cmd / WSL) | Full support. This is the one to standardise on. |
| VS Code integrated terminal | Full support |
| WSL (any distro) | Full support |
| macOS Terminal.app | Works; 256 colours only, so gradients band |
| iTerm2, Alacritty, Kitty, WezTerm | Full support |
| Linux console / SSH into a VM | Full support |
| **Windows `cmd.exe` in legacy conhost** | **Poor.** Limited ANSI, no truecolor, unreliable mouse. Rendering degrades but is usable. |
| **Git Bash / MinTTY** | **Broken.** See below. |

### Git Bash specifically

MinTTY is not a Windows console — it is a pty emulator, and Python's
console input on Windows goes through `msvcrt`, which MinTTY does not
provide. Interactive Textual apps do not receive keystrokes correctly.
This is a known, long-standing MinTTY/Python interaction, not something
Textual can fix.

Two workarounds, both fine:

```bash
winpty python -m qrp ui        # Git Bash ships winpty
```

or run the same command from Windows Terminal instead.

### The escape hatch: serve it to a browser

This is the part worth knowing, because it removes the terminal question
entirely. `textual-serve` runs the identical app and streams it to a
browser over HTTP — **no code changes**:

```bash
pip install textual-serve
python -m qrp serve --port 8000     # then open http://127.0.0.1:8000
```

Verified working: the server starts, returns a page with a browser-side
terminal, and drives the real app.

So the deployment story has two tiers. Where a decent terminal exists,
run the TUI directly — no port, no browser, works over SSH. Where it
does not (Git Bash, legacy cmd, or an analyst who would rather not use a
terminal at all), serve the same app to a browser. You do not maintain
two frontends.

### Recommendation

Standardise on **Windows Terminal**, which is preinstalled on Windows 11
and a free Store install on Windows 10. Document `qrp serve` as the
fallback. Do not spend effort supporting legacy conhost.

---

## 2. Lazy or eager?

Both, deliberately — and asking the question turned up a **7.4×** win
that was being left on the table.

### What DuckDB offers (measured, not assumed)

| Construct | Behaviour | Measured |
|---|---|---|
| `CREATE TABLE AS SELECT` | **Eager** — runs, materialises | 3.10 s on a 2m-patient scan |
| `CREATE VIEW` | **Lazy** — stores the plan | 0.003 s |
| Relational API (`con.read_parquet(...).filter(...)`) | **Lazy** — builds an unexecuted plan, optimised as a whole at fetch | build 10 ms, execute 2.66 s |
| `COPY (SELECT …) TO 'f.parquet'` | **Streaming** — writes row groups as produced | 8.2 s under a 400 MB limit vs 11.8 s materialise-then-copy |

### The policy this codebase follows

**Materialise on fan-out, stay lazy on single consumption.**

A TABLE is an optimiser barrier. That is exactly what you want where
several stages read the same result — it is the SAS materialisation
boundary that Spark lacked and that the PySpark port tried to recreate
with 50 hand-placed `localCheckpoint` calls. It is exactly what you do
*not* want for a staging step read once, because the copy is pure cost
*and* the barrier blocks filter pushdown into the parquet scan.

Current split: 25 eager tables, 7 lazy views.

### The 7.4× that was hiding in it

`covar_source` — a `UNION ALL` of diagnosis and dispensing, consumed
exactly once by covariate detection — was a TABLE. That materialised
~5.3M rows at 2m patients as a staging copy, and the barrier stopped the
covariate code filter from reaching the parquet scan.

Converting it and three other single-consumer intermediates
(`claim_dose`, `dose_lookback`, `prior_event`) to views:

| | eager | lazy |
|---|---|---|
| covariates stage | 65.1 s | **8.8 s** |
| total runtime | 153.7 s | **94.7 s** |
| database file | 1103 MB | **423 MB** |

Output verified **byte-identical** across six tables by SHA-256.

The general lesson is the one the Spark port learned the hard way from
the other direction: materialisation is not free, and neither is its
absence. The difference is that here the decision is a countable
property — how many downstream consumers does this have? — rather than a
judgement call re-litigated at 50 call sites.

### Streaming to disk

Three mechanisms, in increasing order of how much they help:

1. **`COPY (SELECT …) TO parquet`** streams the result out without
   materialising it. Exposed as `Engine.stream_parquet()`. Use it for
   outputs that are never queried in-process.
2. **A persistent database file** (`--db run.duckdb`) puts intermediates
   under DuckDB's buffer manager instead of in RAM. They spill to disk
   under pressure and are re-read on demand.
3. **`--memory-limit` plus `--temp-dir`** caps RAM and gives DuckDB
   somewhere to spill hash joins, aggregations and sorts.

Measured on the full 2m-patient study (316 MB input, 10 stages):

| Configuration | Time | Peak RSS |
|---|---|---|
| `:memory:`, no limit | 117 s | 3807 MB |
| `--db file`, no limit | 153 s | 3385 MB |
| `--db file --memory-limit 1GB` | 151 s | 1183 MB |
| `--db file --memory-limit 512MB` | 157 s | **687 MB** |

**A 5.5× memory reduction for a 33% time cost.**

### Scaling to a 200-300GB table

The floor is **flat across scale** — 100k, 500k and 2m patients all
complete at 160MB. It is set by per-operator working sets (row-group
buffers, minimum hash table sizes), not by data volume. That is the
property that makes a table far larger than RAM viable.

Getting there took a fix, though, and the fix generalises.

**POV1 was a 10-way join in one statement**, two of them range joins
(enrollment containment, age-stratum band). DuckDB builds those hash
tables concurrently, so peak memory was the SUM of ten build sides
rather than the largest. Below ~420MB at 2m patients it raised
`OutOfMemoryException`, always in that same stage.

Split into three passes — equi-join to demographics and apply
demographic eligibility first, then the enrollment range join on the
reduced set, then age strata — the peak becomes the LARGEST hash table,
and each intermediate is narrower because filtering happens earlier:

| | before | after |
|---|---|---|
| 2m floor | ~420 MB | **under 160 MB** |
| 2m at 2GB (4 alternating samples) | 85.8 s mean | 83.1 s mean |

So the memory floor fell 2.6x at no runtime cost, and output was
verified byte-identical across six tables.

A caution on that runtime figure: the FIRST split measurement came back
at 109.9s against 85.0s unsplit, which looked like a 29% regression and
nearly led to the wrong conclusion. Four alternating pairs showed it was
an outlier — a cold page cache after the code change. On a single-CPU
box, one sample of a 90-second run is not a measurement.

**Disk becomes the binding constraint, not RAM.** At the floor the
pipeline spills roughly 2.6x the input size:

| Limit | Spilled | Time (2m) |
|---|---|---|
| 160 MB | 780 MB | 115 s |
| 256 MB | 536 MB | 107 s |
| 512 MB | 366 MB | 98 s |
| 1 GB | 157 MB | 97 s |

For a 300GB table that implies on the order of 800GB of scratch, on a
disk fast enough not to dominate wall-clock. More RAM buys less spill on
a clean curve, and the time cost across that whole range is about 18%.

Also set `max_temp_directory_size` explicitly — it defaults to 90% of
free disk, which at these volumes is a real ceiling you want to know
about rather than discover.

### Find the floor on YOUR data

POV1 was found only because it was the stage that happened to fail
first. Other stages may have the same shape and surface only at a scale
I could not test (this was measured on a 4GB box with 4.9GB free disk;
300GB is a 1000x extrapolation). So the method matters more than the fix:

```bash
python tools/find_memory_floor.py --study s.json --indata scdm/ --curve
```

It binary-searches the limit, **names the stage that fails** at each
step, and prints the RAM/spill curve. Run it against your largest real
table before sizing a VM. Sample output:

```
input: 79 MB
    1024MB  ok       25.5s  spill      0MB
     304MB  ok       20.7s  spill     16MB
     124MB  ok       21.6s  spill    108MB

minimum memory: ~124 MB  (1.57x the input)
peak spill at that limit: 108 MB (1.4x the input)
```

Two structural guards keep the floor from creeping back:
`test_runs_under_a_tight_memory_limit` runs the pipeline at 200MB, and
`test_no_stage_joins_more_than_six_relations` fails if any single
statement joins more than six relations.

### Correction: there IS a floor

I earlier wrote that this "never fails". That is wrong, and testing the
UI at a tight limit is what caught it. DuckDB spills operators to disk,
but it does **not** spill without limit — some operators need a minimum
working set, and below that the run raises `OutOfMemoryException` rather
than degrading further.

Measured on the 2m-patient study:

| Limit | Result |
|---|---|
| 300 MB | **fails** — OOM after spilling 1.4 GB |
| 400 MB | **fails** — OOM |
| 512 MB | succeeds, 96 s, 260 MB spilled |
| 768 MB | succeeds, 94 s, 213 MB spilled |
| 1 GB | succeeds, 93 s, 133 MB spilled |

So the floor for this dataset sits between 400 and 512 MB, roughly 1.6×
the input size. In-memory and on-disk storage both fail at 400 MB, so
`--db` is not a way around a limit that is simply too low.

The honest framing: DuckDB's degradation is far gentler than the Spark
port's driver OOMs, and the failure is a clean exception with the run
stopped rather than a hung driver. But it is still a failure, and the
UI must say so in terms the user can act on — which is why
`RunHandle._explain()` turns `OutOfMemoryException` into "raise the
Memory limit and set a Spill dir", naming the controls rather than the
internals.

```bash
python -m qrp run --study s.json --indata scdm/ --out results/ \
  --db /scratch/run.duckdb --memory-limit 4GB --temp-dir /scratch/spill
```

### What is not used yet

The **relational API** (`con.read_parquet(...).filter(...).aggregate(...)`)
builds a lazy plan in Python and is a genuine option for composing stages
programmatically instead of via SQL text. I kept SQL files because they
are reviewable by someone who knows SAS and SQL but not Python, and
because they diff cleanly against the SAS source during parity work. That
is a deliberate trade, not an oversight — but if stage composition ever
needs to be dynamic, the relational API is the way to do it without
string-building.
