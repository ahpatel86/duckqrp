# Inclusion / exclusion criteria

`INCLUSIONCODES` defines cohort criteria beyond the exposure definition
— "only patients with a prior diagnosis of X", "exclude anyone with Y in
the year before index".

## Semantics implemented

Read from `ms_createmicohorts.sas`:

| Field | Meaning |
|---|---|
| `cond` | Numbers the condition. **Every** condition must pass (AND). |
| `condlevel` | Groups codes within a condition. Codes at the same level are **alternatives** (OR). |
| `indexcriteria` | `INC` = must have, `EXC` = must not have. |
| `condfrom` / `condto` | Day offsets from the index date. |
| `codedays` | Minimum number of **distinct days** carrying the code. |
| `codecat` | `DX` or `RX`. For `RX` the supply interval is used, not just the fill date. |

So evaluation is: satisfied-per-`(cond, condlevel)`, ORed within a
condition, ANDed across conditions. That is a grouped aggregate with a
`HAVING`, not a chain of filters.

The stage runs **after** `pov1` (index dates must exist) and **before**
the master list, so an excluded patient never reaches it. Placement
matters: filtering later would leave excluded patients in the
intermediate attrition counts.

Failures are kept in `inclusion_excluded` with the first failing `cond`
and the criteria that failed, so attrition can report why.

## Verified against real data

On a real SCDM extract (174k patients, 15M claims), with
`cond 1: INC hypertension (I10, 4019)` and
`cond 2: EXC diabetes (E119, 25000)`, both over the prior 365 days:

| | index dates | final cohort |
|---|---|---|
| no criteria | 93,873 | 50,045 |
| with INC/EXC | 14,320 | 2,694 |

Checked independently rather than by inspection: **0** surviving
episodes lack the required hypertension code, and **0** carry an
excluded diabetes code. Failure reasons decompose as 70,437 INC-only,
5,206 EXC-only, 3,910 both.

## NOT implemented

### `IEV` / `EEV` — now implemented

Event-anchored rules, evaluated relative to the **event** date rather
than the index date. SAS routes them to `_InclExclHOI`
(`ms_createmicohorts.sas:1388`) and evaluates them against unique
`(patid, eventdt)` pairs in `ms_createpov56.sas:186-196`, keeping only
passing events.

**The distinction from INC/EXC is the whole point.** A failing INC/EXC
rule removes the **episode**; a failing IEV/EEV removes only that
**event** — the episode survives, counted as event-free or scored on a
later qualifying event.

Measured on the 100k fixture, episode count unchanged in every case:

| | episodes | with event | events dropped |
|---|---|---|---|
| no rules | 64,663 | 1,868 | 0 |
| IEV requires a code near the event | 64,663 | 1 | 2,095 |
| EEV excludes a code near the event | 64,663 | 1,867 | 1 |

While IEV/EEV were unimplemented they were lumped into the
index-anchored stage, which was a fair approximation at the time. Once
the event-anchored stage existed they were applied **twice** and against
the wrong anchor — an IEV rule removed 64,643 of 64,663 episodes instead
of filtering a handful of events. `52_inclusion.sql` now handles INC/EXC
only, pinned by a regression test that greps for the old predicate.

### `minrxdays` — implemented

`ms_createpov3.sas:26` — "total days in window >= minrxdays". A
threshold on days of **supply**, not on the number of dispensings, and
meaningful only for `codecat='RX'`. SAS resets it to 1 elsewhere rather
than failing, which is reproduced along with the warning.

### Subconditions — THREE levels, not two

I previously recorded `subcondlevel` as "not a results gap" on the
grounds that it appears in only one SAS file. **That was wrong**, and
the correction matters.

`subcondlevel` is a character column; SAS derives a numeric `subcond`
from it (`ms_processinputfiles.sas:715-740`), and **`subcond` appears
109 times across 9 files**, including the inclusion evaluator
`ms_createpov3.sas`. Grepping the input column name missed the derived
variable entirely.

The real hierarchy (`ms_createpov3.sas:22-38`):

| level | combined with | source |
|---|---|---|
| codes within a subcondition | **OR** | one row per code |
| subconditions within a condition | **AND** | `SUBCONDLEVEL` |
| conditions | **AND** | `CONDLEVEL` |

`subcondinclusion` inverts a subcondition: "If the subcondition is met
but it is a subexclusion, then means that condition not satisfied."

Two consequences for what was here before:

* **`cond` was being read as an input column.** It does not exist in
  the file — only `condlevel` does. On a real input every rule would
  have landed in condition 1, collapsing every condition into one and
  ORing what SAS ANDs. Both are now derived by renumbering, exactly as
  SAS does.
* **The inner level was ORed.** With `subcond` unmodelled, subconditions
  were treated as alternatives. Now `bool_and` across subconditions,
  with the sub-exclusion inversion applied first.

The `subcondlevel` validations (single `minrxdays`, `codedays` and CFDD
value per subcondition) are also reproduced as load-time warnings; SAS
warns rather than failing, and so does this.

### A parsing trap worth recording

`str(r.get("subcondinclusion") or "1")` silently converts an integer
`0` to `"1"`, because `0` is falsy — turning every sub-exclusion into a
sub-inclusion. Absence now has to be tested explicitly. The same
`or "default"` idiom is safe everywhere else in the loader, because
those fields default to strings where `""` and `None` both genuinely
mean absent; only the boolean case is hazardous.

### A bug `minrxdays` exposed

Several `INCLUSIONCODES` rows share one `(cond, condlevel)` — one per
code. Joining `cfg_inclusion` directly therefore counted **every claim
once per row**.

`codedays` uses `count(DISTINCT adate)`, which absorbs duplication
entirely, so the bug was invisible for as long as that was the only
threshold. `minrxdays` uses `sum()` and exposed it immediately: three
codes inflated supply threefold, and a 120-day threshold was passing at
40 real days. Survivors went from 587 to 13 once fixed.

Both the index-anchored and event-anchored stages now join a
`SELECT DISTINCT` on the condition key, asserted structurally — an
arithmetic test would only fail on data that happens to have multi-code
conditions.

### The stage now runs AFTER the master list

Inclusion evaluation moved from before episode construction to after,
filtering `ptsmasterlist` instead of `pov1`. This matches SAS —
`ms_createpov3` is called with `_PtsMasterList` — and it is what makes
`EPISODEENDDT` anchoring possible, since the episode end does not exist
any earlier.

For INDEXDT-anchored rules the two placements are equivalent: the
predicate depends only on `(patid, indexdt)`, which both tables carry.
Verified by every existing inclusion test continuing to pass and by a
real-data run reproducing its previous `cohort_final` exactly.

### A bug only real data exposed

`cfg_inclusion_codes` was keyed on `(cohortgrp, cond, subcond)` with
**no `criteria`**. SAS numbers `cond` within `(group, CONDUSE)`, so an
INC rule and an EXC rule can both be cond 1 — and with no criteria in
the key they picked up each other's codes.

Every fixture test used a single criteria at a time and passed. A real
study putting INC (hypertension) and EXC (diabetes) at condlevel 1
produced a cohort in which **100% of survivors violated the criteria**:
46,378 episodes, 46,378 violations.

`criteria` is now part of the key throughout — the code table, the hits
join, and the per-condition rollup. A regression test puts INC and EXC
at the same condlevel and asserts zero violations.

This is the clearest argument in this document for running against real
inputs: the defect was invisible to a test suite that exercised each
criteria in isolation.

### Condition anchors — parsed, partly applied

`condfromanchor` / `condtoanchor` anchor **each end of the window
independently** (`ms_createpov3.sas:139-175`), giving six combinations:

| value | meaning |
|---|---|
| blank / `INDEXDT` | the index date |
| `EPISODEENDDT` | the episode end — a **forward-looking** window |
| `INDEXDT_EXP` | `indexdt_exp` — when exposure began inside a pregnancy exposure window (Type 4) |

`INDEXDT` and `EPISODEENDDT` are both applied, now that the stage runs
after the master list. Verified that the two produce different windows
on the same codes — if the anchor were ignored they would agree.

`INDEXDT_EXP` anchors on the date exposure began within a pregnancy exposure
window (Type 4), and **warns at load** rather than silently falling
back to the index date — a silent fallback makes the window wrong
rather than missing.

These columns were not parsed at all before, so any non-index anchor was
silently evaluated as an index-date lookback.

### Dose thresholds — implemented

`mincumdose`, `minafdd` and `maxafdd` are per-SUBCONDITION thresholds,
aggregated across the rows of a subcondition the way SAS does
(`ms_createpov3.sas:333-353`): `max(mincumdose)`, `min(minafdd)`,
`max(maxafdd)` — strictest lower bound, widest upper bound.

**aFDD is an average over the window**, not a per-claim value:

```
afdd = round(sum(cfdd) / sum(numdispensing), 1)     (line 452)
```

so it belongs in the aggregate beside the day counts, not in a filter on
individual claims.

**cumdose is PRO-RATED** by the supply falling inside the window
(`ms_createpov3.sas:369-375`):

```sas
if incdate    < adate+condfrom then ToDeductBf = (adate+condfrom)-incdate;
if incexpiredt > adate+condto   then ToDeductAf = incexpiredt-(adate+condto);
if cumdose > 0 then cumdose = cumdose * (rxsup - Bf - Af) / rxsup;
```

Counting the whole claim when it merely overlaps over-credits a
dispensing that mostly falls outside. Measured: without pro-rating, 205
episodes passed a threshold they should have failed.

Three things this turned up, all recorded because each is the kind of
mistake that repeats:

* **Dose must come from the MATCHED claim.** The first version joined
  `claim_dose`, which is built over `exposure_claims` — the DEF codes
  only — so any inclusion code that was not also an exposure code got
  `cumdose = 0` and a threshold excluded the whole cohort.
* **A code with no `codestrength` entry has no dose**, so any threshold
  excludes it. This looked like a bug during development; the cause was
  a test using codes absent from the strength lookup. Pinned by
  `test_dose_needs_a_strength_lookup`.
* **Join order matters more than it looks.** Inserting
  `LEFT JOIN cfg_code_strength` between `covar_source`'s `ON` clause and
  its `periods_overlap` predicate rebound the overlap to the strength
  join, silently disabling the window filter — 701 criteria violations,
  caught by a test written for an unrelated bug two changes earlier.

### `codepop` — not a Type 2 concern

`codepop` marks which population a code applies to: `M` (mother), `I`
(infant), `MI` (both). It is pregnancy-cohort machinery.

Traced rather than assumed, after `subcondlevel` taught me not to judge
a column by where its name appears. It has 69 references across 8 files
and only 3 are inside an explicit `type = 4` guard, which looks alarming
— but every actual use is reachable only from the mother-infant path:

* `ms_createpov3.sas:273` gates the `M`/`I` split on
  `cohortType in ("mil","milhoi")`. The Type 2 callers pass
  `cohortType=&type.`, i.e. `2`.
* `ms_createmicohorts.sas:1034-1046` reads `codepop` only inside blocks
  writing to `_preg&groupind.`.

So there is nothing to implement for Type 2. Recorded here so the next
reader does not re-investigate it, and so the claim can be checked
rather than taken on trust.

### Still not implemented

These are parsed and **warned about at load**, because ignoring them
makes the cohort broader than SAS's — the dangerous direction:


If your studies use any of these, say which and they can be added — the
shape fits what is already here.
