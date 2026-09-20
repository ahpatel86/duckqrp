# Performance


---

## Benchmark after the review fixes

Real SCDM, single core, replicated to 4x by offsetting patient ids so
the copies are distinct people. Best of three runs.

| study | dataset | patients | MB | best | MB/s | episodes |
|---|---|--:|--:|--:|--:|--:|
| simple | 1x | 174,064 | 175 | 3.05 s | 57.5 | 63,943 |
| simple | 4x | 696,256 | 319 | 11.74 s | 27.2 | 255,772 |
| production | 1x | 174,064 | 175 | 6.38 s | 27.5 | 1,085 |
| production | 4x | 696,256 | 319 | 49.50 s | 6.5 | 4,340 |

### Scaling is NOT uniformly linear — correction to an earlier claim

| study | 1x | 4x | ratio |
|---|--:|--:|--:|
| simple | 3.05 s | 11.74 s | **3.85x** |
| production | 6.38 s | 49.50 s | **7.76x** |

Earlier documentation claimed linear scaling at ~19 MB/s. That was
measured on a **simple** study — two cohorts, a handful of codes — and
it still holds: 3.85x for 4x the data.

It does **not** hold for a production-shaped study. The one benchmarked
here has 49 covariates over 4,385 codes and produces 1,085 episodes from
174k patients, and it scales at 7.76x.

### Where it goes

Per-stage, 1x to 4x:

| stage | 1x | 4x | ratio |
|---|--:|--:|--:|
| **covariates** | 2.14 s | **33.13 s** | **15.5x** |
| cida denominators | 2.00 s | 7.89 s | 3.9x |
| inclusion criteria | 1.02 s | 3.77 s | 3.7x |
| exposure + stockpiling | 0.51 s | 1.76 s | 3.5x |

Every stage but one scales linearly. The covariate detection join is
`ptsmasterlist x cfg_covariates x cfg_covariate_codes x covar_source`,
and **both** the episode side and the claim side grow with the extract:
28.6M x 1,085 at 1x becomes 114M x 4,340 at 4x — a 16x product for 4x
the data, which is the 15.5x observed almost exactly.

### FIXED — the claim scan is now driven by the cohort

`cohort_claims` is built once after the master list, restricted to
cohort members, and read by the covariate, inclusion, risk-score and
event-anchored stages.

| study | dataset | before | after |
|---|---|--:|--:|
| production | 1x | 6.38 s | **4.77 s** |
| production | 4x | 49.50 s | **13.27 s** |
| simple | 1x | 3.05 s | 3.08 s |
| simple | 4x | 11.74 s | 12.32 s |

**4x is 3.7x faster**, and the production study now scales at
13.27/4.77 = **2.8x for 4x the data** — no longer superlinear. Output is
byte-identical on both studies.

Two conditions gate the materialisation, and **both** are necessary:

* **Something reads it heavily.** A study with no covariates, no
  inclusion rules and no risk scores touches the claims once, so a copy
  is pure cost. Gating on the cohort share alone regressed exactly such
  a study from 3.05 s to 6.02 s at 1x and 11.74 s to 26.91 s at 4x —
  it was paying for a copy nothing read.
* **The cohort is a minority of the extract** (<50%). Above that the
  copy is nearly the whole table and saves nothing.

Otherwise `cohort_claims` is a VIEW over `covar_source`, which costs
nothing to define. It is always defined either way, because
`60_followup.sql` reads it unconditionally.

### An earlier attempt that did not work

Restricting `covar_source` to cohort members with a semi-join before the
join looked obvious — the cohort is a small fraction of the extract. It
gave 33.1 s -> 27.1 s at 4x but **regressed 1x from 2.14 s to 3.31 s**,
so it was reverted rather than shipped. DuckDB's optimiser is evidently
already doing most of that work, and the extra subquery cost more than
it saved at the scale that matters most.

The lesson carried into the fix above: **filtering inline and
materialising a filtered table are not the same optimisation.** The
subquery blocks pushdown; an explicit table pays one scan and is then
reused by four stages.

### Caveat on these numbers

The 4x dataset is the 1x extract replicated with offset patient ids, so
per-patient claim density is identical and only the patient count grows.
A real 4x extract would likely have a different distribution, and the
covariate join's behaviour depends on exactly that.


---

## Dataset used for these benchmarks

Real SCDM extract:

| table | rows | compressed |
|---|--:|--:|
| procedure | 13,619,271 | 63.9 MB |
| diagnosis | 10,484,395 | 57.0 MB |
| dispensing | 4,478,080 | 22.2 MB |
| encounter | 4,392,630 | 16.6 MB |
| lab_result | 1,434,206 | 12.7 MB |
| enrollment | 233,340 | 1.3 MB |
| demographic | 174,064 | 1.0 MB |
| **1x total** | **35.2M rows** | **175 MB** |
| **4x total** | **~140M rows** | **319 MB** |

174,064 patients at ~202 claims each. The 4x set is the same extract
replicated with offset patient ids, so patient count grows and
per-patient density does not.

**The MB/s figures elsewhere in this document divide by COMPRESSED
parquet size and are therefore optimistic as a throughput measure.**
Rows per second is more honest: the production study at 4x processes
roughly 10.6M rows/s.

## Whole-package scaling sweep

After the covariate fix, every stage was profiled at both sizes with
`tools/scale_profile.py`, which flags anything growing faster than the
data:

| stage | 1x | 4x | ratio |
|---|--:|--:|--:|
| cida denominators | 1.73-1.85 s | 7.47-7.82 s | 4.06-4.45x |
| exposure + stockpiling | 0.52 s | 1.22 s | 2.35x |
| enrollment_spans | 0.14 s | 0.67 s | 4.83x |
| normalize | 0.13 s | 0.47 s | 3.68x |
| covariates | 0.02 s | 0.04 s | — |
| everything else | <0.1 s | <0.15 s | — |

**No stage is meaningfully superlinear.** The covariate stage, which was
15.5x before the fix, is now 0.04 s at 4x and no longer measurable.

Two honesty notes on this table:

* An earlier reading put `cida denominators` at **5.67x**, which would
  have been a genuine problem. It did not reproduce: three clean runs
  gave 4.06x, 4.22x and 4.45x. Run-to-run variance on this box is about
  10%, and the first measurement was taken while other work was
  competing for the machine. **A single timing is not a measurement.**
* `cida denominators` is nonetheless **~60% of total runtime**, so a
  constant-factor improvement there is worth more than a scaling fix
  anywhere else. Its statement breakdown is roughly even across
  `_denom_windows`, `_denom_demog`, `_denom_strat` and the final
  aggregation, with no single dominant step — which is why it has not
  been restructured.

`tools/scale_profile.py` is the durable output here. Every test passes
at fixture scale, where a 15x-vs-4x difference is under a second; only a
scaling comparison makes this class of defect visible.


---

## Would more cores and more RAM help?

### What it grabs by default — now 8 GB, capped both ways

The package sets an **explicit** limit rather than inheriting DuckDB's,
because DuckDB's default is 80% of physical RAM:

| host | package default | DuckDB's default would be |
|---|--:|--:|
| 2 GB | 1 GB | 1.6 GB |
| 4 GB | 2 GB | 3.1 GiB |
| 8 GB | 4 GB | 6.4 GB |
| 16 GB | **8 GB** | 13 GB |
| 64 GB | **8 GB** | 51 GB |
| 128 GB | **8 GB** | ~102 GB |
| 512 GB | **8 GB** | ~410 GB |

Capped **both** ways, and both caps matter:

* The **8 GB ceiling** stops a shared server being drained by a job that
  does not need it. Above a 1 GB limit, more memory buys ~2%.
* The **fraction on small hosts** stops a machine being handed a limit
  it cannot honour — DuckDB accepts the setting and then fails partway
  through, which is worse than spilling.

Override anywhere:

```bash
qrp run --memory-limit 16GB ...          # CLI
```

The TUI's Memory field is prefilled with the same default and accepts
any value. `Engine(memory_limit="16GB")` for library use.

Below the limit the pipeline **spills to disk rather than failing**, so
a lower value costs time, not correctness — 512 MB completes at a 12%
penalty on the production study.

**Historical note:** this used to leave the limit unset.

| server | default grab |
|---|--:|
| 4 GB (this box) | 3.1 GiB |
| 16 GB | ~13 GB |
| 64 GB | ~51 GB |
| 128 GB | ~102 GB |

On a shared DP server that is a lot to take silently, and by the
measurement below it is **wasted** — above 1 GB it buys about 2%.

`--memory-limit 1GB` is a reasonable default for a study of this size.
Set it explicitly rather than relying on the default, and raise it only
if a larger extract proves it necessary.

The run signature now records the **effective** limit and thread count
rather than what was requested, so `memory_limit=None` shows as
`3.1 GiB` and not as `None`. A run record that does not say what the job
took is not a record of what the job took.

### RAM: no — measured

The benchmark box has 1 core and 4 GB. Varying the memory limit on the
production study against the real extract:

| memory_limit | best of 3 | vs 1 GB |
|---|--:|--:|
| 256 MB | **fails** — out of memory | — |
| 512 MB | 5.55 s | 1.12x |
| 1 GB | 4.94 s | 1.00x |
| 2 GB | 4.86 s | 0.98x |
| 3 GB | 4.83 s | 0.98x |
| default (3.1 GiB) | 4.82 s | 0.98x |

**Above 1 GB, more RAM buys about 2%.** The simple study is flatter
still — 3.76 s at 1 GB against 3.84 s at 3 GB, which is inside run-to-run
noise.

So the working set fits comfortably in 1 GB at this scale. Giving the
process 16 GB would not make it meaningfully faster. Below 512 MB it
starts spilling and then fails, so 1 GB is the sweet spot rather than a
number to raise.

### Cores: cannot be measured here

`nproc` reports **1**, and DuckDB's default `threads` is therefore 1.
There is no second core on this machine to test with, so any figure for
multi-core speed-up would be extrapolation, not measurement. It is not
quoted.

What can be said from the stage profile, without claiming a number:

* DuckDB parallelises parquet scans, hash joins, aggregation and sorts
  across threads. Nearly all of this pipeline's time is in exactly those
  operators — `cida denominators` (a large join then a GROUPING SETS
  aggregation) is ~60% of runtime, and `normalize` and
  `enrollment_spans` are scans.
* The window functions in stockpiling and washout parallelise by
  partition, and the partition key is `patid`, so parallelism is limited
  by patient count rather than by anything structural. At 174k patients
  that is not a constraint.
* Nothing in the pipeline is serialised behind a Python loop over data.
  Config is registered once and every stage is a single SQL statement,
  so there is no per-cohort or per-patient round trip to block scaling.

That is a reason to expect the workload to parallelise reasonably, not
evidence that it does. **Benchmark it on the target hardware before
planning around any speed-up.** The 1-core numbers in this document are
a floor and should be treated as one.


---

## Final benchmark (all output corrections applied)

Real SCDM: 174,064 patients, 35.2M rows, 175 MB parquet. Best of two,
single core, 1 GB effective memory limit.

| study | dataset | patients | rows | best | rows/s |
|---|---|--:|--:|--:|--:|
| production | 1x | 174,064 | 35.2M | **4.60 s** | 7.6M |
| production | 4x | 696,256 | 140.6M | **15.75 s** | 8.9M |
| simple | 1x | 174,064 | 35.2M | 3.51 s | 10.0M |
| simple | 4x | 696,256 | 140.6M | 13.32 s | 10.6M |

Scaling: production 3.42x and simple 3.79x for 4x the data. Both
sub-linear, because fixed per-run costs amortise.

The production study is a real input file — 14 cohorts, 1,124 cohort
codes, 4,385 covariate rows across 49 covariates, 250 inclusion rows,
and now the full output set including `baseline`, `numcounts` and
`followuptime_cida`.

### One stage sits on the threshold

`cida denominators` measured **4.75x, 4.89x and 5.43x** across three
runs for 4x the data, against a flag threshold of 5.0. The 5.43 was
taken while the 4x dataset was still being written, so the machine was
contended; the settled figures are 4.75-4.89.

Earlier measurements of the same stage gave 4.06-4.45. The stages added
since (`baseline`, `numcounts`, `followuptime`) plausibly account for
the shift.

**Reported as borderline rather than clean.** It is roughly 65% of total
runtime, so a constant-factor improvement there is worth more than a
scaling fix anywhere else, and the flag firing inconsistently across
runs is itself the finding — a single measurement would have called it
either linear or superlinear depending on when it was taken.


---

## After the architectural refactor

Structural changes only — the stage table, one declaration per output,
and the `outputs.py` split. Verified no behavioural change: every output
count identical, and the full suite green (241 passed, 2 skipped, 0
failed across all 243 tests).

| study | dataset | best | rows/s | scaling |
|---|---|--:|--:|--:|
| production | 1x | 5.36 s | 6.6M | |
| production | 4x | 17.31 s | 8.1M | **3.23x** |
| simple | 1x | 3.64 s | 9.6M | |
| simple | 4x | 13.38 s | 10.5M | **3.68x** |

Both sub-linear, and unchanged from before the refactor (3.42x / 3.79x).
**No stage grows faster than the data** — `cida denominators` measured
4.80x, within the 4.06-4.89 range recorded earlier.

The absolute times sit at the upper end of the ~10% run-to-run variance
this document warns about (5.36 s against a best of 4.15 s recorded
earlier). The machine had been running tests continuously for hours, so
these are a contended reading. The scaling RATIOS are the figures a
refactor could plausibly have moved, and they did not move.

### Running the full suite

It takes roughly 13 minutes, longer than a single command can be held
open here, and background processes do not survive between commands. It
was run in six deterministic chunks:

```bash
pytest tests/ -q --collect-only | grep "::" > all_tests.txt
# split every Nth line into chunk0..chunk5, then per chunk:
mapfile -t IDS < chunk0.txt && pytest "${IDS[@]}" -q
```

`mapfile` rather than word splitting, because parametrised test IDs
contain commas and brackets. A check afterwards confirmed the six chunks
partition the collected set exactly: 243 collected, 243 dispatched, 243
unique.
