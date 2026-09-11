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
