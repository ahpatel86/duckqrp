# Parity with the SAS implementation

An audit of the DuckDB SQL against the **SAS macros**, not the PySpark
port. The port was the source this was written from, so anything it got
wrong was inherited silently; this compares to the original.

Status: **both confirmed divergences fixed**, several stages verified
equivalent by reading.
Nothing here has been checked against SAS *output* — that is what the
parity harness is for. This is a reading of the source.

---

## Divergences found — and fixed

### 1. Stockpiling groups by drug, not just by patient  ✔ FIXED

`ms_stockpiling.sas:399` runs `by PatId &GROUPING.` and resets on
`first.&SINGDIM.`, where the caller passes:

```
GROUPING = StockGroup indexcriteria dateonly [fupcriteria] [InclExclVars...]
```

So SAS stockpiles **within a stockgroup**. Two different drugs in the
same cohort are pushed forward independently.

This implementation partitions by `(cohortgrp, patid)` only, so all of a
patient's exposure claims in a cohort are stockpiled together.

**When it matters:** only if a cohort's DEF codes span more than one
`stockgroup`. With a single study drug the results are identical. With
two, this version pushes dates further forward than SAS and produces
longer, fewer supply intervals.

**Fixed.** `stockgroup` is carried from `cohortcodes` into `cfg_codes`,
through `exposure_claims`, and into the `PARTITION BY` of both windows in
`30_exposure.sql`. The closed form is per-partition so the algebra is
untouched. Codes with no stockgroup share `_default`, which reproduces
the single-drug case exactly — asserted by
`test_stockgroup_absent_reproduces_single_drug_behaviour`.

`test_stockpiling_is_partitioned_by_stockgroup` splits a cohort's codes
across two stockgroups and asserts the latest expiry cannot move later
than the merged case, which is the direction the bug pushed it.

### 2. Exposure episodes do not break on enrollment change  ✔ FIXED

`ms_createclaimepi.sas:75`:

```sas
if "&gaptype." = "F" and (gap > &Gap. or LEnrStartDt ne Enr_Start) then
    episode = episode + 1;
```

SAS starts a new episode when the gap is exceeded **or when the claim
falls in a different enrollment span**. `50_episodes.sql` breaks only on
the gap.

**When it matters:** a patient who disenrolls and re-enrols gets one
continuous episode here and two in SAS. Cohorts with `enrolgap` set
generously, or populations with churn, will differ.

**Fixed.** A `located` CTE in `50_episodes.sql` attaches the covering
enrollment span to each claim, and the break condition now ORs in
`claim_enr_start IS DISTINCT FROM prev_enr_start`. `IS DISTINCT FROM`
rather than `<>` so a NULL span on either side counts as a change
instead of swallowing the comparison; the join is a LEFT JOIN because a
claim outside any span must still take part in chaining — enrollment is
enforced by the master list, not here.

Verified against the data rather than by row count:
`test_episodes_break_on_enrollment_change` asserts that **no episode
spans more than one enrollment span**. On the 100k fixture, episodes go
263,574 → 265,019.

---

## Verified equivalent

### Washout (`ms_findgap.sas`)

The two-pass SAS logic reduces exactly to this implementation's single
running maximum. Checked clause by clause:

* `proc sort nodupkey by patid adate descending ExpireDt` — the same
  dedup rule (longest supply per patient-day).
* Pass 1: `gap = DaysUntreated > max(&gap.,0)` with
  `DaysUntreated = Adate - prev_expiredt`, i.e. `prev_expiredt < adate - gap`.
* Pass 2 excludes where
  `periods_overlap(index.Adate-gap .. index.Adate-1, Inc.ADate .. Inc.ExpireDt)`.
  Expanding: `Inc.ADate <= index.Adate - 1` (so only **prior** claims can
  block) **and** `Inc.ExpireDt >= index.Adate - gap`. That holds for some
  prior claim exactly when it holds for the one with the greatest
  `ExpireDt` — hence `max(prior expiredt) < adate - gap`, which also
  subsumes pass 1.
* `gap = 0` → all deduped claims kept (SAS skips both passes). Matches.
* `gap = .` → only the first claim per patient. Matches
  `wash_per IS NULL → prior_max_expiredt IS NULL`.

### Stockpiling recurrence (`ms_stockpiling.sas:399-441`)

`overlap = lexpiredt - CLMDATE + 1`; when positive,
`CLMDATE = lexpiredt + 1` and `ExpireDt = CLMDATE + CLMSUP - 1`. Since
`overlap > 0` iff `lexpiredt >= CLMDATE`, that is exactly
`s(i) = max(a(i), e(i-1) + 1)` — the recurrence the closed form solves.
`lexpiredt` is the *adjusted* previous end, which the closed form also
assumes. Equivalent **within a partition** (see divergence 1).

### Age (`ms_createpov1.sas:92`)

SAS uses `yrdif(birth_date, adate, 'AGE')`. The `age_years` macro matches
on every boundary case tested, including leap-day births:

| born | on | this | SAS |
|---|---|---|---|
| 1960-06-15 | 2020-06-14 | 59 | 59 |
| 1960-06-15 | 2020-06-15 | 60 | 60 |
| 2000-02-29 | 2021-02-28 | 20 | 20 |
| 2000-02-29 | 2021-03-01 | 21 | 21 |

### Enrollment spans (`ms_episoderec2.sas:87-93`)

`ENRSTART - lag(ENREND) - 1 > ENROLGAP` matches
`date_diff('day', prev_end, enr_start) - 1 > enrol_gap`. The
coverage-change conditions match per coverage mode: `MD` breaks on either
flag, `M` on MedCov, `D` on DrugCov.

### Episode gap type P (`ms_createclaimepi.sas:79`)

`gap > (&Gap. * LRxSup)/100` matches
`episode_gap * prev_rxsup / 100.0`.

---

---

## Round 2: censoring and demographics (`ms_createptsmasterlist`)

No new divergences. Four things checked, all equivalent — but two only
after looking closely enough to be surprised.

### Censoring precedence is order-independent

SAS truncates sequentially:

```sas
if EpisodeEndDt > &censordate. then EpisodeEndDt = &censordate.;   /* L127 */
if EpisodeEndDt > enr_end      then EpisodeEndDt = enr_end;        /* L156 */
if enrend_death in ('B','C') and EpisodeEndDt > DeathDt
                               then EpisodeEndDt = deathdt;        /* L146 */
if &maxepisdur. > 0 and EpisodeEndDt - IndexDt + 1 > &maxepisdur.
                               then EpisodeEndDt = IndexDt + &maxepisdur. - 1;
```

Every clause is "truncate to the smaller value", so the chain is a
minimum and the order cannot matter. A single `least(...)` is
equivalent. The `maxepisdur` clause is worth noting: it assigns
`IndexDt + maxepisdur - 1`, a value fixed by the index date rather than
derived from the running `EpisodeEndDt`, so it stays a plain minimum.

### Death censoring: a gate that turns out to be redundant

SAS applies death only when `enrend_death in ('B','C')`, and
`ms_create_enrollment_spans.sas:206-210` sets that flag only when
`censor_dth = 1` **and** `DeathDt <= Enr_End`.

That second condition looked like a divergence — this implementation has
no such test. It is redundant: `enr_end` is already in the same
`least()`, so a death after enrollment ended can never be the minimum.
`least(x, enr_end, deathdt)` with `deathdt > enr_end` is
`least(x, enr_end)`.

### Demographic filtering: raw values, then recode

`ms_create_enrollment_spans.sas:148` filters at the
enrollment/demographic join:

```sas
where Enr.patid = Dem.patid
  and upcase(Dem.sex) in (&DemogSex.)
  and upcase(Dem.Race) in (&DemogRace.)
  and upcase(Dem.Hispanic) in (&DemogHispanic.)
```

and only *afterwards* recodes `if sex in ('A','U') then sex = 'O'`.

So the filter sees raw values and the output carries recoded ones. This
implementation keeps both — `sex_raw` for the `cfg_demog` join, `sex`
for output — which matches. Filtering on the recoded value would have
made a study specifying `sex = "U"` match nobody.

(The PySpark port sets `LOOP_CONFIG["sex"]` and never reads it, which
briefly suggested these were stratification-only. `DemogSex` shows they
are a genuine filter.)

---

## Known gaps, all gated features

Present in SAS, absent here, each behind a condition:

| SAS | Gate | Status |
|---|---|---|
| `IndexLookEndDt` truncation | mock surveillance (`PERIODIDSTART=1`, `PERIODIDEND>1`) | not implemented |
| `censordate_maxdose` | `maxcumdose` **and** `cumdoseper` set | ✔ **FIXED** — see below |
| dose limits on INC/EXC rows | `mincumdose`/`minafdd`/`maxafdd` set | ✔ **IMPLEMENTED** — see docs/INCLUSION.md |
| FUT truncation (`trunkdt`) | Type 2/5 with episode extension | not implemented — truncates an extended episode at the first qualifying claim in the extension |

---

## Round 3: dose  ✔ FIXED

### `maxcumdose` censors as well as excludes

SAS does **two different things** with `maxcumdose`, and only the first
was implemented:

1. `ms_pov1dose.sas:126-137` — **exclude** the index date when prior
   cumulative dose falls outside `[mincumdose, maxcumdose]`
   (`attrition_reason = 1`). That is `dose_excluded`.
2. `ms_createpov4.sas:178-201` — separately **censor** the episode.
   Accumulate `cumdose` within the episode from
   `(episodestart - cumdoseper)` onward, ordered by `(adate, expiredt)`,
   and set `censordate_maxdose` to the expiry of the first claim whose
   running total exceeds `maxcumdose`.
   `ms_createptsmasterlist.sas:137` then truncates `EpisodeEndDt` to it.

The difference is not cosmetic: a dropped patient and a shortened
follow-up give quite different denominators.

**Fixed** in `55_dose_censor.sql`, gated on SAS's own condition
(`maxcumdose` *and* `cumdoseper` both set). On the 100k fixture with
`maxcumdose=800, cumdoseper=90`, **17,703 episodes are shortened rather
than dropped**. Tests assert that no censored episode ends after its
censor date, and — the important one — that censored episodes are still
*present*, shortened rather than removed.

This also required `claim_dose` to carry `expiredt`, since SAS orders
the accumulation by `(adate, expiredt)` and the exclusion path never
needed it.

### Dose comparisons round to the nearest integer

SAS compares `round(cumdose,1)` and `round(cfdd,1)`. The second argument
to SAS's `ROUND` is the rounding **unit**, not a digit count, so that is
round-to-nearest-integer. Raw comparison disagrees at the boundary: a
cumulative dose of 99.6 against `mincumdose = 100` is **included** by SAS
and was excluded here. All four dose comparisons now round.

---

## Round 4: follow-up washout (`ms_createpov56`)  ✔ FIXED

Two divergences, both of which made the cohort **larger** than SAS's —
the direction that produces publishable-looking wrong numbers.

### Missing `FupWashPer` is the strictest setting, not the loosest

`ms_createpov56.sas:78`:

```sas
*If FupWashPer=. then patients need to never have had an Event (hence 99999);
... %MS_PeriodsOverlap(period1=Epi.IndexDt-min(&FUPWASHPER.,99999) Epi.IndexDt-1, ...)
```

A missing value means **never had an event**. This parsed it as `0`,
which means *no washout at all* — the exact opposite.

**Fixed.** `fup_wash_per` is now `int | None`, with `None` meaning
"never". Represented as `None` rather than SAS's 99999 because the
`fup_wash_per > enr_days` validation applies to the value the study
*supplied*; a sentinel fails that check spuriously. (My own validator
caught this, which is how the representation got chosen.)

Measured on the 100k fixture, strictness now orders correctly:

| setting | final cohort |
|---|---|
| `0` (no washout) | 71,350 |
| `183` | 64,663 |
| missing (never) | **40,901** |

### Events in the blackout window exclude the episode

`ms_createpov56.sas:95` builds `_EventsInBlackout`, and line 127 drops
those episodes outright:

```sas
if a and not b and not c and not d;
```

This implementation used `blackout_per` only to shift the at-risk start,
so an event in the blackout was *ignored* but the episode **survived**.
SAS removes it.

**Fixed.** `60_followup.sql` now excludes any episode with an event in
`[indexdt, indexdt + blackout_per - 1]`, asserted directly.

### A near-miss worth recording

SAS data-step `min()`/`max()` are row-wise **and ignore missing values**,
unlike SQL's `least()`, which normally propagates NULL. That would have
been a systematic bug across all the censoring logic. DuckDB's `least()`
happens to ignore NULL too, so the two agree — but this was luck rather
than design, and the sentinel `COALESCE(..., DATE '9999-12-31')` guards
already in place make it moot.

---

## Round 5: defaults sweep and person-time

Prompted by a pattern: **all six divergences found so far made the
cohort LARGER than SAS's.** Defaults resolving to "no restriction",
criteria applied as adjustments rather than exclusions. So this round
checked every default systematically rather than waiting to trip over
one.

### Defaults verified

`ms_processinputfiles.sas:611-615` is where SAS sets them explicitly:

| SAS | value | this implementation |
|---|---|---|
| `codedays` | 1 | 1 ✔ |
| `dateonly` | `'N'` | `False` ✔ |
| `minrxdays` | 1 | not implemented (documented gap) |
| `mincfdd` / `maxcfdd` | 0 *(on INCLUSIONCODES rows)* | n/a — cohort-level missing correctly means "no restriction", since `ms_pov1dose` gates on `&maxcfdd. ne .` |

No new divergences. The cohort-level dose parameters and the
per-inclusion-code ones are separate things with different defaults,
which is worth keeping straight.

### Redundant blackout shift removed

With the blackout exclusion now in place (round 4), the event search was
*also* shifting its start by `blackout_per`. SAS does not —
`ms_createpov56.sas:148` uses `refstart=indexdt` with the comment
"Not possible to have event in blackoutper", because those episodes are
already gone.

Provably equivalent either way, but two mechanisms enforcing one rule is
how the copies later drift apart. Removed; verified that zero surviving
episodes carry an event in the blackout at both `blackout=0` and
`blackout=30`.

### Person-time: a difference in kind, not a bug

SAS's `ms_cidadenom` computes `MemberDays = DenomEnrEndDt -
DenomEnrStartDt + 1` — **enrolled** member-days across the population,
the background denominator for incidence rates.

This implementation's `denominators` table sums **exposed** person-days
from `cohort_final` episodes. Both are legitimate denominators and both
appear in QRP outputs, but they answer different questions. If your
study reports incidence against the enrolled population rather than
against exposed time, `ms_cidadenom` is a stage still to be written —
not a correction to this one.

---

## Implemented since: care setting / principal diagnosis

`ms_caresettingprincipal.sas` expands `cohortcodes.caresettingprincipal`
into (EncType, PDX) pairs, which then restrict which claims count as
events:

```sas
(codes.EncType = "**" or codes.EncType = po.EncType)
and (codes.Pdx = "*"  or codes.Pdx     = po.Pdx)
```

The column was parsed and **silently dropped** — the same failure mode
as `inclusioncodes`: a study could specify it, get no warning, and
receive a broader cohort than SAS.

Encoding, per the macro: 3-character tokens of two EncType characters
plus one PDX character, concatenated or space-separated. SAS first
translates `*`→`A` and `.`→`_`, so `AAA` (from `***`) or an empty value
means "any care setting". `AA` is a wildcard EncType, `A` a wildcard
PDX, `_` a missing PDX.

Verified on the 100k fixture — events narrow as the restriction tightens
and every surviving event is backed by at least one qualifying claim:

| restriction | event claims |
|---|---|
| `***` (none) | 217,570 |
| `IP*` | 43,720 |
| `IPP` | 12,868 |

A note on the test: `event_claims` is `DISTINCT` on
`(cohort, patid, adate)`, so a patient with an IP and an AV claim on the
same day legitimately keeps the row. Asserting "no joined claim violates
the rule" wrongly flags the sibling claim — the correct assertion is
`EXISTS` (at least one qualifying claim backs each event), not
`NOT EXISTS`. My first version of the check got this wrong and reported
16 false violations.

---

## Implemented since: CIDA output table (`ms_cidatables.sas`)

The study deliverable — `msoc/<runid>_t2_cida` — one row per
`(level, cohort, stratum)` with SAS's metric names, because this table
leaves the site and is read against SAS documentation:
`npts`, `episodes`, `adjustedcodecount`, `rawcodecount`, `daysupp`,
`amtsupp`, `all_events`, `eps_wevents`, `followuptime`, `timetocensor`.

**USERSTRATA is now parsed** (it was on the unimplemented list).
Following `ms_processinputfiles.sas:1843`: lowercase `levelvars`,
convert `*` to a space so `agegroup*sex` and `agegroup sex` are the
same, and append `agegroupnum` wherever `agegroup` appears. Only rows
with `tableid = 't2cida'` are used, matching SAS's `where` clause.

On SAS's two-stage aggregation (`ms_cidatables.sas:115-140`): pass 1
classes by `group PatId <levelvars>` taking `max(Patient)`, pass 2 sums
that across patients. `max` then `sum` is how SAS counts DISTINCT
patients without a distinct operator; `count(DISTINCT patid)` is the
same thing. Every other metric is a plain sum, which is associative, so
the single-pass form is equivalent.

Verified on real SCDM with four levels — every level is the same
population sliced differently, so totals must agree:

| level | strata | episodes | followuptime |
|---|---|---|---|
| 1 | (overall) | 50,045 | 1,290,396 |
| 2 | agegroup | 50,045 | 1,290,396 |
| 3 | agegroup × sex | 50,045 | 1,290,396 |
| 4 | sex × race | 50,045 | 1,290,396 |

matching `cohort_final` exactly. That reconciliation is the test worth
having: it catches a stratification that drops or duplicates rows, which
is the failure a squared table is prone to.

The denominator merge is now included — see below.

---

## Implemented since: enrolled member-days (`ms_cidadenom.sas`)

The **background** denominator: how much eligible enrolled time existed,
exposed or not. Distinct from the exposed person-time already in
`denominators`, and it is what SAS merges onto the CIDA numerators.

The window (`ms_cidadenom.sas:135-137, 708`):

```
DenomEnrStartDt  = Enr_Start + ENRDAYS
AdjustedEnrEndDt = min(Enr_End, censordate)
                 - max(0, MinEpisDur-1, MinDaySupp-1, BlackoutPer,
                          ReqDaysAftInd,
                          ReqDaysAftEpi + max(MinEpisDur-1, MinDaySupp-1,
                                              BlackoutPer))
MemberDays       = DenomEnrEndDt - DenomEnrStartDt + 1,  kept when > 0
```

Symmetric: eligible time starts once the required prior enrollment is
satisfied, and ends early enough that an index date there could still
meet every forward-looking requirement.

**The merge is a FULL join, not a LEFT one.** A stratum can have
eligible members but no exposed episodes; a left join would drop those
rows and understate the denominator. Numerator metrics are zero-filled.

Verified on real SCDM. Levels reconcile (161,871,568 member-days at every
level), and the drop from enrolled to eligible is fully accounted for:

| | patients |
|---|---|
| with enrollment spans | 144,190 |
| eligible in denominator | 77,303 |
| dropped — window too short | 66,887 |
| dropped — data quality | **0** |

### The two structural guards disagreed

Written as one statement this was a nine-way join, which tripped
`test_no_stage_joins_more_than_six_relations` — the guard added after
POV1 in that shape set the memory floor. Splitting it into three passes
then tripped `test_no_single_consumer_temp_tables`, because each
intermediate has exactly one consumer.

The two rules genuinely pull in opposite directions here, and **memory
wins**: peak memory in a wide join is the sum of the concurrent hash
tables, not the largest. The denominator temps are now exempt from the
single-consumer rule for the same stated reason as the POV1 pair, and
the exemption says so rather than being silent.

---

## Implemented since: risk scores (`ms_computeriskscores.sas`)

A risk score is a weighted sum over **conditions**, not over claims.
`ms_computeriskscores.sas:369` takes `max(weight)` per
`(group, PatId, IndexDt, condidnum)` *before* summing, so meeting a
condition ten times scores it once — and two codes mapping to one
condition with different weights contribute the larger, not the sum.
Then line 396 adds the intercept, and line 407 gives the intercept to
patients matching nothing rather than NULL.

Three condition sources, unioned: `DX`/`PX`/`RX` claims within the
`riskfrom`/`riskto` anchor window, and `DM` rows matching a sex or age
group rather than a claim. `RISKSCORECODES` carries its own
`caresettingprincipal`, reusing the parser from that stage.

Verified on real SCDM against an independent recomputation of
"max weight per condition, summed, plus intercept" — **zero episodes
differ**. Outputs are `dplocal/<runid>_risk_scores` (patient-level) and
`msoc/<runid>_risk_score_summary` (distribution).

### Two bugs this turned up

**A DuckDB cursor does not inherit connection settings.** The telemetry
cursor was printing its own progress bar to stdout — which would corrupt
the TUI and had been leaking into captured output. Silenced explicitly,
with a test asserting both progress settings are off on the monitor
cursor.

**The single-consumer guard fired again, and this time it was right.**
Unlike the denominator stage, none of the risk-score intermediates has
more than two joins, so there was no memory argument for materialising
them — they became CTEs. Worth recording that the same guard was
correctly overridden in one stage and correctly obeyed in the next: the
question is always whether the join is wide, not whether a temp table
feels tidier.

---

## Implemented since: geographic variables (`ms_geographicvars.sas`)

Adds `zip3`, `state`, `hhs_reg`, `cb_reg`, `sdi` and `sdi_cat` to each
episode from a ZIP lookup, plus `zip_uncertain`.

Two rules worth stating because both are easy to get subtly wrong:

* **The Unknown rules cascade.** A missing zip *or* an unmatched
  statecode makes all four geography variables Unknown, not just the one
  that failed to map. And `cb_reg` is additionally Unknown when the
  lookup says `"other"` — a value, not a NULL.
* **`zip_uncertain` defaults to `'Y'`.** A missing `zip_date` means
  uncertain, and so does an index date *before* the zip was recorded:
  the address on file postdates the event. Only a `zip_date` on or
  before the index date gives `'N'`.

Required carrying `zip_date` through POV1 into the master list, which
was not previously kept.

### `missing()` on a character variable

SAS's `missing()` is true for NULL **and for blank**. `IS NULL` alone is
not equivalent, and the difference is invisible until a source column
uses `''` rather than NULL — which real SCDM `postalcode` does. Found by
noticing that the "unmatched zip" in a real run was an empty string, not
a NULL.

Here the two happen to coincide, because an empty zip also fails the
lookup join and so leaves `statecode` NULL either way. But the
coincidence is not something to rely on, so there is now an
`is_missing()` macro used throughout this stage, and a test pinning
NULL, `''` and `'  '` as missing.

---

## Implemented since: code distribution (`ms_codedistribution.sas`)

Enumerates which codes — and which **combinations** of codes — defined
the index events. The table a reviewer uses to sanity-check an exposure
definition before trusting the cohort.

SAS builds it by sorting on `(patid, indexdate, distindexID)`, then
concatenating the IDs per index date into an underscore-separated
`distindexlist`, then counting episodes per distinct list
(`ms_codedistribution.sas:250-278`).

**The sort is the point.** It makes the list canonical, so two episodes
defined by the same code set in a different claim order land in the same
bucket. Reproduced with `ORDER BY` inside `string_agg`; without it the
same set would produce different strings and scatter across buckets.
There is a test asserting every list is in ascending ID order, plus a
second assertion that multi-code combinations actually occur — otherwise
the first test passes vacuously.

Two MSOC outputs: `distindex` (counts per combination) and
`distindexmap` (code to ID). Verified on real SCDM: 295 distinct
combinations over 40 codes, 255 of them multi-code, and episode counts
summing to 69,208 — exactly the master list.

---

## Implemented since: utilization (`ms_computeutilization.sas`)

Two covariate families, counted in a window anchored on the index date:

* **Medical** — encounter counts per care setting. SAS's default list is
  `AV OA IP IS ED` (line 63), output as one column per setting plus a
  total.
* **Drug** — `count(rx)`, `count(distinct generic)` and
  `count(distinct classname)` (lines 415-418).

**The distinct counts are the point.** Dispensings measure intensity;
distinct generics and classes measure breadth of treatment. They answer
different questions, and if they were all plain counts the ordering
`numrx >= numgeneric >= numclass` would collapse. The test asserts that
ordering *and* that strict inequality actually occurs somewhere —
otherwise it passes vacuously on data with no repeat fills.

Verified on real SCDM against an independent recomputation of `numrx`:
zero rows differ. Every episode gets a row (69,208 = master list), with
zeros where the window is empty.

### The join guard fired a third time

Written as one statement of CTEs this was seven joins. CTEs in a single
statement are still **one query plan**, so the concurrent hash tables
all count toward peak memory — the guard was right again. Split into
`_util_med` and `_util_drug` and exempted from the single-consumer rule
for the same stated reason as POV1 and the denominator stage.

Three stages have now hit this: POV1, `cidadenom`, `utilization`. The
pattern is consistent enough to be worth stating as a rule — a stage
that joins the master list to more than two claim sources wants
splitting, not a wider statement.

---

## Implemented since: lab extraction (`ms_extractlabs.sas`)

Two configurable mechanisms, both of which fail silently if got wrong.

**LABDATETYPE** — which date a lab record is dated by. A three-character
priority string (`ms_extractlabs.sas:176-183`): `'LRO'` means lab date,
else result date, else order date. It is a COALESCE whose *order* comes
from config, so `'ORL'` is a different answer, not a stylistic variant.
A test asserts the two orders produce different output — the fixture
leaves `result_dt` NULL 20% of the time so a priority starting with `R`
must fall through.

**LABRESULT** — a result filter written as a comparison string:
`'>=7'`, `'<=140'`, `'~=0'`, or a range `'3.5:5.5'`. Two traps:

* `'<='` must be tested before `'<'`, or `'<=7'` parses as `'<'` with a
  bound of `'=7'`.
* the range separator is `':'`, **not** `'-'`. SAS explains why in a
  comment: a hyphen is ambiguous with a negative lower bound. `'-2:2'`
  parses correctly.

Verified with three codes at once (`>=100`, `50:150`, unfiltered): the
bounded codes stay inside their bounds and the unfiltered one admits
values outside them — the second assertion matters, or the first proves
nothing.

### Three extraction paths, dispatched by codetype

`substr(codetype,1,2)` selects which column a lab code matches
(`ms_extractlabs.sas:201, 301`):

| prefix | path | matches |
|---|---|---|
| `01` | LAB01 | the site's lab code |
| `02` | LAB02 | the LOINC |
| other | LABXX | the procedure code |

and `substr(codetype,3,1)` is the **result type** the criterion applies
to — a numeric criterion must not constrain a character result.

SAS runs all three paths and then removes true duplicates, "as a record
could have been extracted three times using three different criteria"
(line 17). Matching on the code SET instead of running three passes
means a claim matched by two paths appears once, and no dedup pass is
needed.

Verified on the fixture: each path finds rows, and a code valid on one
path finds nothing on another (a `lab_code` does not match a LOINC).

### Now verified against real lab data

A real SCDM lab extract (1.4M rows, 15k patients) corrected four things
the synthetic fixture had hidden. Each is the same class of error as
`ndc` vs `rx` — a schema assumption that a self-made fixture cannot
falsify.

**There is no `lab_code` column.** LAB01 does not match a code at all:
the lookup maps a code onto a SEVEN-ATTRIBUTE combination — test name,
sub category, specimen source, result unit, result type, fasting
indicator, patient location — and the lab record is matched on that
combination (`ms_extractlabs.sas:251-258`). The previous implementation
matched a `lab_code` column that does not exist.

**The result columns are `ms_result_n` and `ms_result_c`**, not
`result_num`.

**`order_dt` and `result_dt` are SAS numeric dates stored as DOUBLE, and
in this extract they are entirely NULL** — only `lab_dt` has values. The
LABDATETYPE fall-through is therefore load-bearing rather than
defensive: a study specifying `ROL` gets nothing without it.

**`result_type` is 77% `'U'` (unknown)**, against 23% `'N'` and 0.07%
`'C'`. A numeric criterion does not constrain a non-numeric result, so
the "criterion does not apply" branch is the **common case**, not an
edge case. A test asserting that bounded codes stay inside their bounds
reported 11,242 false failures until it was scoped to `result_type='N'`.

Verified on the real extract with three independent checks, all zero:
LAB01 results not backed by a full seven-attribute match; LAB02 results
not backed by a LOINC match; results outside the covariate window.
28,430 lab results extracted across two cohorts in 9.9 s.

One sanity note worth recording: extracted creatinine has a **max of
12,300 mg/dL**, which is physiologically impossible. That is in the
source data (raw median 0.87, max 13,600), not introduced by the
pipeline — checked before assuming it was a bug.

Building that fixture produced its own lesson: the first version
generated patids `1..174064` while real SCDM patids run to 175 million,
so only **151 of 174,064** patients overlapped and the stage looked
almost inert. Synthetic keys have to be drawn from the real ones.

---

## Implemented since: Most Frequent Use (`ms_mfu.sas`)

"Which codes appear most often in this cohort" — used to review what a
population is actually being treated for, rather than what the study
assumed.

**`COUNTMETHOD` is the whole point.** Ranking by `codecount` (claims) or
`patcount` (distinct patients) answers different questions: a code
appearing 50 times in one patient outranks a code appearing once in 40
patients by claim count, and loses badly by patient count.

Real SCDM makes the difference concrete — the same cohort, two methods:

| by claims | | by patients | |
|---|---|---|---|
| `I10` | 21,126 claims / 3,640 pts | `V700` | 8,539 claims / 4,989 pts |
| `4019` | 15,398 / 4,189 | `V7231` | 8,288 / 4,543 |

Twenty of the top-ten ranks disagree between the two. A test asserts
they disagree — if the two produced the same order, `countmethod` would
not be being read at all.

SAS ranks with a retained counter over a sorted set, keeping
`rank <= topxx`. That is `row_number()` with `QUALIFY`, but the ordering
needs an explicit tie-break or two codes with equal counts could swap
between runs and the top-N cut would not be reproducible — pinned by a
thread-count invariance test.

---

## Implemented since: IOC washout codes

`fupcriteria='IOC'` marks a **third code role**, alongside the index
(`DEF`) and outcome (`EVENT`) sets. An IOC code never defines an index
or an outcome — it only disqualifies an episode whose follow-up washout
window contains one.

SAS routes them to `_FUPWash` (`ms_createmicohorts.sas:1685`) and
evaluates them in `_WashEventsInFupWash` (`ms_createpov56.sas`)
alongside the outcome codes, with the same window and the same
`dateonly` handling.

Implemented as a `washout_claims` view unioning the event codes with the
IOC codes, which the existing ASOF join then reads. Verified on the
100k fixture:

| IOC codes | final cohort |
|---|---|
| none | 64,663 |
| 2 | 64,488 |
| 20 | 62,314 |

with an independent check that **no** surviving episode has an IOC claim
in its washout window, and a test asserting IOC codes never leak into
the exposure or event sets — the separation is the point of the role.

---

## Cross-feature testing

Three of the defects in this document were found by running features
**together**, not by the focused tests that cover each one:

* `cond` read as an input column — surfaced when a real study used
  `condlevel`
* `subcondinclusion` parsed with `or "1"`, so integer `0` became a
  sub-inclusion
* `cfg_inclusion_codes` missing `criteria` from its key, so INC and EXC
  rules at the same condlevel picked up each other's codes

The last is the sharpest: every focused test used one criteria at a
time and passed, while a real study using both produced a cohort where
**100% of survivors violated the criteria**.

`test_every_feature_enabled_at_once` now runs all twelve optional
stages in a single study — IOC codes, INC and EXC sharing a condlevel,
an IEV rule, covariates, user strata, risk scores, utilization,
geography, MFU, dose restrictions and dose censoring — and asserts both
the invariants and that each stage produced rows. The non-vacuity check
matters: the first version used a rare code for the INC rule, leaving
149 episodes, and several downstream stages ran against nothing while
every assertion still passed.

Exercising features one at a time verifies each in isolation and
nothing about their combination.

## Out of scope for Type 2 — verified, not assumed

* **`codepop`** — mother/infant population marker. Every use is gated on
  `cohortType in ("mil","milhoi")` or writes to a `_preg` dataset. See
  docs/INCLUSION.md.
* **`INDEXDT_EXP` anchors** — anchor on the exposed index date, which
  is Type 4 (pregnancy) machinery. Warned about at load.
* **Types 1, 3, 4, 5, 6** — not ported.

---

## Covariate anchoring (`ms_cidacov`) — FIXED

`covfromanchor`/`covtoanchor` anchor each end of a covariate window
independently (`ms_cidacov.sas:47-54`) — the identical mechanism the
inclusion rules use, listing all six anchor combinations.

The covariate stage hardcoded `m.indexdt` for both ends and ignored the
columns entirely, so an `EPISODEENDDT`-anchored covariate — a
**forward-looking** window — was silently evaluated as a lookback.

Found by auditing `ms_cidacov` after fixing the identical defect in the
inclusion stage. Worth stating as a pattern: **when a mechanism appears
in one macro it is worth grepping for it in the others before assuming
it is local.** The same anchor columns turned out to exist on inclusion
rows, covariate definitions and risk-score codes.

Now applied, with `INDEXDT_EXP` warning rather than falling back
silently. Tests assert that a blank anchor and an explicit `INDEXDT`
give identical results, and that `EPISODEENDDT` gives a different
window.

---

## Anchors — a systematic sweep

After the covariate stage turned out to carry the same anchor defect as
the inclusion stage, I grepped every `*anchor` column in the SAS rather
than waiting to trip over a third. There are **22 distinct anchor
columns**; mapping each to the macros that read it separates the ones
Type 2 reaches from the ones it does not:

| anchor pair | macro | status |
|---|---|---|
| `condfromanchor` / `condtoanchor` | `ms_createpov3` | ✔ implemented |
| `covfromanchor` / `covtoanchor` | `ms_cidacov` | ✔ implemented |
| `riskfromanchor` / `risktoanchor` | `ms_computeriskscores` | ✔ implemented |
| `hdpswinfromanchor` / `hdpswintoanchor` | `ms_computeps` | propensity scores — not ported |
| `obsfromanchor` / `obstoanchor` | `ms_evalsecondaryepi` | secondary episodes — not ported |
| `outcomefromanchor` / `outcometoanchor` | `ms_createmicohorts` | mother-infant path |
| `durationanchor` | `ms_process_pregnancyoutcomes` | Type 4 |
| `cc_covfromanchor` / `cc_covtoanchor` | `ms_cidacov` | combo covariates — not ported |
| `keepanchor` | `ms_computeutilization` | **vestigial** — built from `medutilanchorfrom`/`medutilanchorto`, which are referenced once and never set anywhere in the package |

**Three stages carried the identical defect** — inclusion rules,
covariate windows and risk-score windows all hardcoded the index date
and ignored their anchor columns, silently turning an
`EPISODEENDDT`-anchored (forward-looking) window into a lookback. All
three are now implemented, with `INDEXDT_EXP` warning rather than
falling back silently.

`test_every_anchor_column_is_handled_or_warned` asserts that every
anchored config object exposes the pair and defaults a blank to
`INDEXDT`, so a fourth cannot be added silently.

The lesson generalises beyond anchors: **when a mechanism appears in one
macro, grep for it across the package before assuming it is local.** I
found the second instance by accident and the third only by deliberately
looking.

---

## A real input file — four corrections

Running a production `qrp_inputfiles_type2_*.json` (14 cohorts, 1,124
cohort codes, 4,385 covariate rows, 250 inclusion rows) found four
things the hand-written fixtures could not.

**COVARIATECODES is one row per CODE**, with `covarnum` repeated — 4,385
rows for 49 covariates, up to 1,432 codes for one of them. The parser
expected one row per covariate with a `codes` list, a shape the fixtures
invented. On a real file every row became its own covariate and
validation rejected the duplicate covarnums. Same class of error as
`ndc` vs `rx` and the lab schema.

**Real files carry no `covarname`.** `stockgroup` supplies the label in
practice.

**`PX` is a mainstream codecat**, not an edge case — 150 of 1,124 cohort
codes, 80 covariate codes, 30 inclusion codes. There was no
`cdm_procedure` view and `covar_source` had no PX arm, so procedures
were invisible. Both added.

**`CC` combo covariates are boolean EXPRESSIONS**, not code lists:

```
covar 12: 3 or 4 or 5 or 6
covar 14: 2 and (3 or 4 or 5 or 6)
covar 49: not (1 or 48)
```

Seventeen of the 49 covariates in this study use them, so rejecting `CC`
meant the study could not run at all. Now parsed at load into a template
of `{cN}` placeholders, each becoming an `EXISTS` against
`covariates_long`. **Only integers ever reach SQL** — the study's own
text is never concatenated in, and a malformed expression fails at load
with a message. Verified independently: zero rows of covar 12 unbacked
by 3/4/5/6, zero rows of covar 16 failing `2 and 3`.

The study runs end to end in **6.1 s** against the real SCDM extract:
259 episodes, 2,469 covariate rows, 238 CIDA rows across 4 strata
levels.

## The parity harness — comparison side

`qrp run --parity-dump <dir>` writes the DuckDB side in the layout the
existing harness expects. **`tools/parity_compare.py` is the other
half**, and it is the piece that was missing:

```bash
qrp run --study s.json --indata data/ --out results/ --parity-dump duck_dbg/
python tools/parity_compare.py sas_dbg/ duck_dbg/
```

Exit 0 means no differences; exit 1 lists them.

### What it classifies, and why

| class | why it is separate |
|---|---|
| `MISSING/EXTRA TABLE` | one side produced a table the other did not |
| `MISSING/EXTRA COLUMN` | **the commonest defect found in this package** — invisible to any row-count check |
| `ROW COUNT` | different numbers of rows |
| `KEY MISMATCH` | same count, different rows — a count alone is a weak check |
| `VALUE` | same key, different value; counted per column so one systematic error does not drown the report |

Rows are aligned on declared key columns before values are compared. A
positional diff on a 60,000-row master list says only "these differ",
which is not actionable.

Numeric and missing-value formatting is tolerated: SAS writes `.` for
missing and formats floats differently. A harness that flags every such
difference produces thousands of false positives and gets ignored, which
is worse than not running it.

### What the dump covers

Thirteen tables across nine stages — the intermediates, where a
divergence is easiest to localise, **and every deliverable**:

| stage | tables |
|---|---|
| STOCKPILING / POV1 / PTSMASTERLIST / POV56 | `stockpiled`, `index_candidates`, `pov1`, `ptsmasterlist`, `cohort_final` |
| ATTRITION / CENSOR | `attrition`, `censoring` |
| CIDA | `t2_cida`, `numcounts`, `denomcounts` |
| CODEDIST | `distindex`, `distindexmap` |
| FOLLOWUPTIME | `followuptime` |

The dump originally covered only the five intermediates. The sweep then
found **every deliverable wrong** — wrong shape, wrong column names,
missing columns, and in one case the wrong table entirely. Comparing
only intermediates would have caught none of it.

`IGNORE_COLUMNS` excludes this package's own column names on tables that
carry both (`cohort_final` has `group` and `cohortgrp`, `FEventDt` and
`eventdt`). A test asserts every ignored column actually exists — a
stale entry makes the dump fail at runtime with
`Column "step" in EXCLUDE list not found`, which is how that test came
to exist.

### Why this is the highest-value remaining work

Every source of ground truth applied to this package has found defects:

| ground truth | defects found |
|---|---|
| reading the SAS macros | 8 |
| a reviewer pointing at a dismissed column | 1 |
| real lab data | 4 |
| a real input file | 4 |
| a code review | 13 |
| sweeping the outputs | 8 — *every output table* |

Reading has repeatedly failed, including on claims already believed
verified — the `INDEXDT_EXP` description in this document was fiction,
written confidently and propagated across three files before being
checked.

**SAS's actual output is the one source of truth never applied.** One
comparison against it would test every stage at once, including the ones
read confidently and got wrong.

## Still unaudited

- Covariate anchoring in `ms_cidacov`.
- `ms_cidadenom` enrolled member-days (see above).
---

## Implemented since: EVENTCOUNT

`ms_createpov56.sas:130-139` deduplicates `_FUPEvent` before counting:

| value | key | meaning |
|---|---|---|
| 0 | none | every qualifying claim counts |
| 1 | `(PatId, Adate, codecat, codetype, code)` | one per code per day |
| 2 | `(PatId, Adate)` | one per day, whatever the code |

This was **hardcoded to 2** — `SELECT DISTINCT (cohortgrp, patid,
adate)` — and the omission was invisible because the only consumer took
`min(adate)`, which is invariant under all three keys. Adding
`numevents` (needed for IEV/EEV) made the setting start changing the
answer.

Verified on the 100k fixture:

| eventcount | event claims | dup `(patid,adate)` | dup `(patid,adate,code)` |
|---|---|---|---|
| 0 | 217,652 | 82 | 4 |
| 1 | 217,648 | 78 | **0** |
| 2 | 217,570 | **0** | 0 |

`1` removes code-level duplicates while keeping same-day
different-code events; `2` removes those too. A third test asserts the
event **flag** is identical across all three — only the count moves,
since `min(adate)` really is invariant. That invariant is what let the
bug hide, so it is worth pinning explicitly.


---

## Correction: what INDEXDT_EXP actually is

This document described `INDEXDT_EXP` as "the exposed index date, which
only exists in comparator designs" in several places. **That was wrong**
and the correction is worth recording because the error was repeated
across three files without ever being checked.

`indexdt_exp` is **pregnancy machinery** (`ms_createmicohorts.sas:764`):

```sas
*assign indexdt_exp. If exposure date begins prior to exposure window
 then set to start of exposure window;
indexdt_exp = indexdt2;
if . < indexdt_exp < exposurefromdt then indexdt_exp = exposurefromdt;
```

The surrounding variables are `pregstartdt`, `exposurefromdt`,
`exposuretodt`, `exposureunit`. So `indexdt_exp` is **the date exposure
actually began inside a pregnancy's exposure window**, clamped to the
start of that window — distinct from `indexdt`, which for a pregnancy
cohort is usually the pregnancy start date.

Anchoring a window on `INDEXDT_EXP` therefore means "measure from when
the drug exposure began, not from the pregnancy start".

Every use is gated on `type = 4`, so it is not reachable from Type 2.
The practical consequence for this package is unchanged — it still
warns rather than silently treating it as an index-date anchor — but the
*reason* given was fiction, and a reader deciding whether the gap
mattered to them would have been misled about which study designs it
affects.


---

## Second review — findings and outcomes

Twelve items. **One was incorrect, five were real correctness bugs**,
and the rest were cleanups. Each was verified before acting, which
mattered: the incorrect one would have made a working function worse.

### Not a bug

**`Engine.shape()` returns bytes as rows.** It does not.
`duckdb_tables().estimated_size` IS the row count — verified on a wide
table with 400 bytes of padding per row, where rows and bytes differ by
~400x, and it tracked rows exactly. The suggested test was added anyway,
because nothing pinned the semantic and the claim could not be checked
from the code alone.

### Real bugs, all of one kind

Every one had the same shape: **the config layer promises a feature the
SQL does not deliver.**

| finding | effect |
|---|---|
| PX risk-score codes | join forced `codecat='DX'`, so PX codes searched the diagnosis table. Score 0 before, 5,510 after. |
| risk-score care settings | `enctype`/`pdx` parsed into `RiskScoreCode` and never reached the SQL. A study restricting to inpatient got a score over every setting. |
| `eventcount` key | missing `codetype`. **26 real pairs** in the extract where ICD-9 and ICD-10 share a code on one patient-day were collapsed into one event. |
| EVENT / IOC domains | read `cdm_diagnosis` only. SAS sets `_FUPEvent` from `_ITDrugs` (RX), `_ITMeds` (DX and PX), `_ITLabs`, `_itenc`, `_itDth` — so a PX or RX **outcome could never fire**. |
| path quoting | `/data/O'Brien/scdm` terminated the SQL string. Verified: DuckDB rejects it outright. |

The EVENT/IOC one is the most serious: an outcome defined by a procedure
or a dispensing produced a cohort reporting no events, with no error.
Measured after the fix — DX 19, PX 192, RX 17 episodes with an event,
where PX and RX were previously 0.

### A fixture that was not exercising the real shape

The EVENT fix surfaced it: `demo_full.json` omitted `codecat` on all 80
cohort-code rows. Real input files **always** carry it — 0 of 1,124 rows
omit it in the study file seen. The fixture has been corrected rather
than the default relaxed; a fixture that cannot distinguish domains
cannot test domain handling.

### A near-miss

The first care-setting test read `IP_` as a wildcard and the expected
ordering came out backwards, which looked like a bug in the fix. `IP_`
is "IP with MISSING pdx"; `IPA` is the wildcard. The code was right and
the test was wrong — the correct ordering is 745 unrestricted >= 150
IP-any >= 55 IP-principal >= 0 IP-missing.


---

## Sweep: config fields that never reach SQL

The five real bugs in the second review were all one shape — **the
config parses a field and the SQL never uses it**. Rather than wait for
a third review to find the rest, every config field was checked against
its consumer.

**130 columns across 18 `cfg_*` tables are all genuinely referenced**
as `<alias>.<column>` in SQL. Four dataclass fields came back
unreferenced; two were false positives (`StratumLevel.table_id` and
`InclusionRule.subcondlevel` are used inside `config.py` itself, which
the scan excluded).

### CODESUPPLY was the one real hit

`CODESUPPLY` replaces the claim's own `RxSup`
(`ms_createmicohorts.sas:571`: `if not missing(codesupply) then
RxSup = CodeSupply`). It was parsed, validated against the CFDD limits,
and **never applied**.

It is per **CODE**, not per cohort. In the real study file 150 of 1,124
rows carry it — all of them PX, because a procedure claim has no
days-supply of its own.

The PX exposure arm hardcoded `rxsup = 1`. That was correct **only
because every one of those 150 values happens to be 1**. A study
specifying 30 would have got 1-day episodes. Verified after the fix:
mean episode length moves 50.9 -> 31.4 -> 89.8 days for CODESUPPLY of
none / 30 / 90.

Correct by coincidence is the failure mode worth naming here: the
production study produced right answers, so no amount of running it
would have surfaced this.

### A second bug inside the first

The CFDD conflict check read `supply_rows[0]` — collapsing a per-code
value to one per cohort. A study where only the **third** code set
CODESUPPLY passed validation silently; one where the first set it
failed. Now checked across every exposure code.

The test pins the last code specifically, since that is the case the
old code missed.

### A fixture assumption caught in passing

The first version of that test asserted the conflict raises for any
code. It does not: only `lisinopril` carries a CFDD limit in the
fixture, so `beta_blocker` setting CODESUPPLY is legitimate. The test
now targets the cohort with the limit and asserts the other is
accepted — the assertion that would otherwise have been wrong in the
permissive direction.
