# Parity against a real SAS run (wp307)

A SAS input-file set and its msoc outputs, from a genuine run. First
time this package has been checked against SAS OUTPUT rather than
against a reading of the macros.

## Row-level comparison against SAS's mstr

The 0.1% `r01_mstr` matches the test extract exactly — all 3,665 of its
patients are present, and its 31,464 rows are the first upload's
episode count. So episodes can be compared INDIVIDUALLY, joined on
(Group, PatID, IndexDt), instead of by totals.

That immediately found a defect totals could never have shown.

### Outcome codes were routed on the wrong column

SAS routes outcomes on FUPCRITERIA:

```sas
/*POV5*/ if fupcriteria in('DEF') then output _FUPEvent;
/*POV6*/ if fupcriteria in('IOC') then output _FUPWash;
```

This package routed on `indexcriteria`, sending everything that was not
`DEF` to the EVENT role. A real cohort of this study holds:

| indexcriteria | fupcriteria | codes |
|---|---|--:|
| DEF | NOT | 54 |
| **FUT** | NOT | **4,957** |
| NOT | DEF | 1 |

So the outcome set became **4,958 codes instead of 1**, and
`all_events` came out at 11,196 against SAS's **3**. Those phantom
outcomes then dropped 58 valid episodes through the blackout-event
rule.

`FUT` is SAS's washout-for-truncation set (`_GroupWashForTrunk`, ms_cidanum.sas:1766),
neither an exposure nor an outcome. Role counts now match SAS exactly:
54 exposure, 1 outcome, 0 IOC.

### FUT codes TRUNCATE episodes — the cause of the long ends

`ms_createptsmasterlist.sas:152`:

```sas
if fut and trunkdt and trunkdt <= EpisodeEndDt
    then EpisodeEndDt = trunkdt;
```

`trunkdt` is the earliest FUT claim overlapping
`[EpisodeStartDt, EpisodeEndDt]` (`ms_createpov4.sas:155-167`, commented
"Truncate (potentially extended using Episode Extension) episodes with
FUT").

So FUT is a **fourth role**, not a residue. The sequence of errors:

1. FUT codes were treated as OUTCOMES, which dropped valid episodes
   through the blackout-event rule.
2. Corrected to "not an outcome" — but then skipped entirely, so
   nothing truncated and every affected episode ran long.
3. Now routed to a TRUNK role and applied.

Role counts for one cohort now match SAS exactly: **54 exposure, 1
outcome, 0 IOC, 4,957 truncation**.

| | before | after |
|---|--:|--:|
| identical episode end dates | 90.2% | **95.5%** |
| episodes | 31,774 | 31,408 (SAS 31,464) |
| `all_events` | 23 | **3 — exactly SAS** |

The outcome count landing exactly on SAS's 3 is the strongest single
confirmation of the role split: the outcome set is now one code, and it
fires the same number of times SAS does.

Worth noting what the intermediate state looked like: fixing the
outcome routing alone made the episode COUNT worse (31,774 against
31,464) while being strictly more correct. The count only recovered
once the truncation it had been accidentally standing in for was
implemented properly.

### What remains on episode ends

The residual changed CHARACTER, which is informative:

| | before truncation | after |
|---|--:|--:|
| differing ends | 3,077 | 1,410 |
| longer here | 3,077 (**all**) | 556 |
| shorter here | 0 | 854 |
| median difference | +116 days | -3 days |

The one-directional signature is gone, so the missing-truncation bug is
genuinely fixed rather than merely offset. What is left is
bidirectional and much smaller.

The 854 now too SHORT are the new problem: truncation firing where SAS
does not. None is degenerate (no episode ends on its index date), 410
are within a week and 148 over a month out, so it is not a single
off-by-one.

Two hypotheses were checked against the macros and BOTH ruled out
before writing any code:

* **`IndexLookEndDt`.** SAS truncates to it at
  `ms_createptsmasterlist.sas:132`, before applying `trunkdt`. It looked
  like the missing piece because this package does not carry a column
  of that name — but `IndexLookEndDt` is `look.LookEnddt`
  (`ms_createpov1.sas:167`) and `LookEnddt = enddate`
  (`ms_createpov1.sas:399`). It is simply the query period end, which
  this package already applies. Not the cause.
* **The exposure extension.** SAS's comment says truncation applies to
  episodes "potentially extended using Episode Extension", so a
  mismatch in the extended end would change which FUT claims fall
  inside. But `expextper` IS applied here —
  `max(expiredt) + exp_ext_per` (`50_episodes.sql:100`). Not the cause.

A third was implemented, MEASURED, and reverted:

* **The unextended POV4 window.** SAS computes `trunkdt` over `_POV4`,
  whose end is `max(ExpireDt)` with no exposure extension
  (`ms_createpov4.sas:106`). Narrowing the window to
  `raw_episodeenddt - exp_ext_per` therefore looked correct. It is
  not: exact end dates fell 95.5% -> **94.6%**. It fixed 106 too-short
  episodes and created 404 too-long ones.

  That result is itself evidence: SAS must apply the extension before
  the truncation is computed, so the extended end is right even though
  the POV4 aggregation statement does not show it.

### A worked example of the 854

```
war_splenec_prev  patid 29670225  index 2016-05-31
  uncensored end   2017-01-12
  my end           2016-06-06     <- truncated at the FUT claim
  SAS end          2016-06-16     <- ten days LATER
  FUT claims in the episode:  2016-06-06  RX  00378632405  (NDC)
```

There is exactly ONE FUT claim in the episode and SAS did not truncate
on it, even though it falls well inside. All 854 short episodes had
their end moved, and none sits at enrolment end or death, so truncation
is what moved them.

So the question is narrower than "when does SAS truncate": it is **why
this RX FUT claim does not truncate**. Candidates, in order of
plausibility:

1. SAS truncates at the FUT exposure's END, not its dispensing date.
   The 10-day gap would then be that dispensing's supply.
2. `_GroupWashForTrunk` receives only certain code categories, and an
   NDC FUT code is not among them.
3. Care-setting or `pdx` conditions on the FUT code that this package
   ignores.

#### The case, fully specified

Everything about this episode is now known:

```
cohort   war_splenec_prev      patid 29670225
exposure claims (post-stockpiling)
    2016-05-31  rxsup 90  expires 2016-08-28
    2016-09-15  rxsup 90  expires 2016-12-13     (gap 18 days < episodegap 30, so they chain)
FUT claim in the episode
    2016-06-06  RX  00378632405  rxsup 90
enrolment                     2010-01-01 .. 2016-09-30
my episode end   2017-01-12 uncensored -> 2016-06-06 after truncation
SAS episode end  2016-06-16
```

**2016-06-16 is not explained by any rule identified so far.** It is
not the claim date (06-06), not its supply end (09-04), not enrolment
end (09-30), not the exposure expiry (08-28), not the chained end
(12-13) and not the extended end (2017-01-12).

The routing is confirmed unconditional — `if indexcriteria in('FUT')
then output _GroupWashForTrunk` runs for every FUT claim in the Type 2
branch (`ms_cidanum.sas:1766`), fed from all six domain datasets
(`_ITDrugs, _ITMeds, _ITLabs, _itdates, _itenc, _itDth`), and `fut` in
`ms_createptsmasterlist.sas:152` is just the merge flag for "a trunkdt
exists". So SAS is not skipping the claim for a routing reason.

#### SAS's own mstr row for this episode

Reading the 92-column row rather than guessing:

```
IndexDt        2016-05-31      EpisodeEndDt   2016-06-16
fup_spec       1               followuptime   16
TotRxSup       17              RawDisp        1      AdjustedDisp 1
cens_elig      1               FEventDt       (none)
```

Two things follow.

**`fup_spec = 1`** — the episode was censored for the SPECIFIED reason,
which is the flag SAS sets for trunkdt censoring. So SAS DID truncate
this episode; it simply truncated it to 2016-06-16 rather than to the
FUT claim date of 2016-06-06.

**`TotRxSup = 17`** where the dispensing carries `rxsup = 90`. SAS
recomputed the supply to the truncated window (2016-05-31 to
2016-06-16 is 17 days inclusive). So the supply is an OUTPUT of the
truncation, not its cause.

Nothing at all is dated 2016-06-16 in this extract — no dispensing,
diagnosis, procedure or encounter — so the date is computed, not read
off a claim. The nearest encounters are 06-08 and 06-22.

#### SOLVED: FUT claims are stockpiled

The FUT code row carries `stockgroup = valsartanhydrochlorothiazide`.
**FUT claims stockpile exactly as exposure claims do** — a dispensing
arriving while the previous one in its stockgroup is still supplying
starts when that supply runs out, not on its own fill date. That
shifted date is the `trunkdt` SAS uses.

The worked case, finally closed:

```
prior stockgroup claim  2016-03-18  rxsup 90  -> supplies to 2016-06-15
FUT claim               2016-06-06  stockpiles to  2016-06-16
SAS trunkdt / episode end              2016-06-16   <- matches
```

The ten-day gap was the leftover supply of the preceding claim, which
is why it matched no parameter: it is data, not configuration. It also
explains the varied shortfalls across the other episodes — 1, 2, 3, 5,
10, 17 days — each being that patient's remaining supply.

| | before | after |
|---|--:|--:|
| identical episode end dates | 95.5% | **98.0%** |
| ends too short | 854 | 256 |
| ends too long | 556 | 357 |
| episodes | 31,408 | 31,488 (SAS 31,464) |
| patients | 19,712 | 19,752 (SAS 19,738) |

#### CORRECTION: stockpiling IS unconditional without a parameter file

An earlier note here claimed SAS stockpiles only nominated stockgroups
and that the study's stockpiling parameter file was needed. **That was
wrong.** The stockpiling file is OPTIONAL, and reading
`ms_stockpiling.sas:394-431` shows what happens without it:

```sas
%if %str("&PERCENTDAYS.") eq %str(".") %then %do; %let PERCENTDAYS=; %end;
...
overlap = lexpiredt - CLMDATE + 1;
if overlap > 0 then do;
    %if %str("&PERCENTDAYS.") eq %str("") %then %do;
        CLMDATE  = lexpiredt + 1;          /* push, unconditionally */
        ExpireDt = CLMDATE + (CLMSUP - 1);
    %end;
    %else %do;  /* only with a file: push when PercentDays <= threshold */
```

With no file the threshold is empty and **every overlap pushes** — the
behaviour this package already implements. The conditional path at
`ms_cidanum.sas:1590` is the WITH-file branch, guarded by
`%if %eval(&NOBS>0)`; the unconditional call at 1545 is the one that
runs here.

So the drift is not the defect it appeared to be: SAS drifts the same
way.

#### The real difference: only DISPENSINGS are stockpiled

`%MS_STOCKPILING(INFILE=_ITDrugs, ...)` — drug claims only. Diagnosis
and procedure claims never pass through it.

This package was stockpiling truncation claims from all three domains,
giving DX and PX claims a notional one-day supply and chaining
same-day codes into long artificial runs.

| | before | after |
|---|--:|--:|
| identical episode ends | 98.0% | **98.2%** |
| ends too long | 357 | 322 |
| ends too short | 256 | 254 |

Also confirmed from the same call: SAS groups by
`StockGroup indexcriteria dateonly fupcriteria`, so exposure and
truncation claims stockpile in SEPARATE chains. This package holds them
in separate tables, which is equivalent.

#### SOLVED: truncation codes lost their stockgroup

Tracing one patient's chain by hand showed every FUT claim sitting in
stockgroup `_default`, while the input file gives them all
`warfarinsodium` — and other codes different values entirely.

Two places dropped it:

* `config.py` captured `stockgroup` only `if key == "DEF"`.
* `pipeline.py` passed it through only `if role == "DEF"`.

So all 187,892 truncation codes landed in one bucket and chained
together regardless of drug. That is what pushed dates 23 days out on
one patient and four YEARS on another: unrelated medicines stacking
into a single supply run.

| | before | after |
|---|--:|--:|
| identical episode ends | 98.2% | **99.2%** |
| ends too short | 254 | **88** |
| ends too long | 322 | **158** |
| episodes | 31,488 | 31,448 (SAS 31,464) |
| patients | 19,752 | 19,728 (SAS 19,738) |

Both counts now UNDERSHOOT slightly, where before they overshot.

The lesson repeats one from earlier in this document: the answer was in
a column of the input file, not in the macro source. Three rounds were
spent reading `ms_stockpiling.sas` — correctly establishing that the
push rule, the same-day operator and the grouping all matched — while
the actual defect was that the grouping KEY was being thrown away
before the SQL ever saw it.

#### Truncation claims were not windowed

`trunc_claims` read the whole dispensing table, with no date filter,
while exposure claims are restricted to
`[start_date - widest_lookback, censor_date]`. SAS builds `_ITDrugs`
from the EXTRACTED claims, so its stockpile chain starts at the window
edge; this package's started at the beginning of the data and carried
extra accumulated drift into the study period.

| | before | after |
|---|--:|--:|
| identical episode ends | 99.2% | **99.6%** |
| ends too short | 88 | **40** |
| ends too long | 158 | **82** |
| episodes | 31,448 | 31,444 (SAS 31,464) |
| patients | 19,728 | 19,722 (SAS 19,738) |

#### A note on the two stockpiling inputs

They are easy to confuse and mean different things:

* **`stockgroup`** is a COLUMN on the cohortcodes file. This study
  populates it with 206 distinct values, and it is what decides which
  claims chain together. It is always present.
* **`STOCKPILE_NONCOVAR`** is a separate OPTIONAL file carrying
  `SameDay`, `SupRange`, `AmtRange` and `PercentDays` — the thresholds
  that decide WHETHER an overlap pushes. This study does not supply
  one, so SAS pushes unconditionally.

Every stockgroup value used here came from the cohortcodes column, not
from the absent parameter file.

#### What remains

576 of 31,426 ends still differ (322 long, 254 short). A worked case:

```
antixa_rupture_inc  patid 151125959  index 2016-06-10
  single exposure claim  2016-06-10  rxsup 30  expires 2016-07-09
  + expextper 30      ->  my end 2016-08-08
  SAS end                 2016-08-04      fup_spec = 1
```

`fup_spec = 1` means SAS truncated at 2016-08-04 — but **no claim of
any kind exists on that date**, and the patient's enrolment runs to
2019 with no death record. So 08-04 is a stockpiled truncation date.

This package's chain for that patient:

```
orig 2015-06-01 -> 2015-09-02      orig 2015-11-08 -> 2016-08-27
orig 2015-08-10 -> 2016-02-29      orig 2016-04-07 -> 2017-02-23
```

SAS's equivalent claim lands on 2016-08-04, 23 days earlier than this
package's 2016-08-27. The chains agree at the start and diverge as they
accumulate.

Checked and matching, so not the cause:

* **Same-day aggregation.** SAS defaults `SAMEDAY_SUPP` to `sum`
  (`ms_stockpiling.sas:262-271`) when no stockpiling file is present,
  which is what this package does.
* **Grouping.** SAS groups by
  `StockGroup indexcriteria dateonly fupcriteria`; all FUT claims here
  share the last three, so it reduces to stockgroup — as here.
* **The push rule** — `CLMDATE = lexpiredt + 1` unconditionally without
  a parameter file.

The divergence is therefore in HOW the chain accumulates rather than in
its inputs or its grouping. The next step is to reproduce SAS's chain
for this one patient by hand from `ms_stockpiling.sas:398-431` and find
the first claim where the two disagree — the algorithm is short enough
to trace exactly, and one patient is enough.

After the truncation fix, 613 of 31,426 episode ends still differ — 357
long, 256 short. Examining one shows the cause:

```
FUT claim   orig 2016-02-26  ->  stockpiled to 2020-07-14
            orig 2016-04-22  ->  stockpiled to 2020-10-12
            orig 2016-07-21  ->  stockpiled to 2021-01-10
```

**Four years of drift.** Each 90-day supply pushes the next claim
forward, and with thousands of FUT codes sharing a stockgroup the chain
never resets.

SAS does not stockpile unconditionally. `ms_cidanum.sas:1571-1590`
draws per-stockgroup parameters from a `STOCKPILE_NONCOVAR` table —

```sas
call symputx("SAMEDAY",     strip(SameDay));
call symputx("SUPRANGE",    strip(SupRange));
call symputx("AMTRANGE",    strip(AmtRange));
call symputx("PERCENTDAYS", put(PercentDays, best.));
```

— and then stockpiles only the stockgroups in that list:

```sas
%MS_STOCKPILING(INFILE=_ITDrugs(where=(stockgroup in (&stock_list))), ...)
```

So stockpiling is CONDITIONAL, governed by supply and amount ranges and
a percent-of-days threshold, and restricted to the stockgroups the
study nominates. This package applies it to every stockgroup with no
conditions, which is right often enough to have fixed 598 episodes and
wrong often enough to leave 613.

The same parameters govern EXPOSURE stockpiling, so this is not only a
truncation issue — the exposure chain in the example above also shows
claims spaced exactly 90 days apart, which is the signature of the same
unconditional chaining.

**To go further, the study's stockpiling parameter file is needed.** It
is not among the twelve tables uploaded, and `QRP_PARAMETERS` does not
name it, so the values SAS used for `SameDay`, `SupRange`, `AmtRange`
and `PercentDays` are not derivable from what is here.

#### What the SAS source alone could not show

`ms_finalizeptsmasterlist.sas:339`:

```sas
if trunkdt and EpisodeEndDt_Censor = trunkdt then fup_spec = 1;
```

So `fup_spec = 1` means the censoring date EQUALS trunkdt. SAS's end is
2016-06-16, so **SAS's trunkdt is 2016-06-16**.

And `ms_createpov4.sas:155-167` computes trunkdt as

```sql
min(Trunk.Adate)
from _POV4 Epi, _GroupWashForTrunk trunk
where Epi.Patid = trunk.Patid
  and PeriodsOverlap(Epi.EpisodeStartDt..Epi.EpisodeEndDt, trunk.ADate)
```

which is exactly what this package implements. But **no claim of any
kind exists on 2016-06-16** in the extract SAS ran on:

| table | dates near 06-16 |
|---|---|
| dispensing | 05-31, 06-05, 06-06, 06-06, 06-29 — all `rxsup` 90 |
| diagnosis | 06-22 |
| procedure | 06-22 |
| encounter | 06-08, 06-22 |

`min(Adate)` over that patient's claims cannot return 06-16. Nor is it
`maxepisdur` — this cohort sets none, and its parameters are identical
to cohorts that agree exactly (`episodegap 30`, `expextper 30`,
`blackoutper 1`).

So the formula in the source and the data in the extract are BOTH
accounted for, and they do not produce SAS's answer. Something between
them — most likely how `_GroupWashForTrunk` claim dates are derived
before that query sees them (stockpiling on the FUT drug would move
them) — is not visible in the macro source alone.

That is the point at which reading stops being productive. A rerun with
`QRP_DEBUG=Y`, which writes the per-step exclusion lists, would show
`_GroupWashForTrunk` directly and settle it in one pass.

Worth noting `TotRxSup` is also a column this package does not
recompute after censoring — a separate, smaller defect this row
exposed.

**Candidate 1 was tested and ruled out.** The dispensing row is
`2016-06-06, rxsup 90`, so the supply ends 2016-09-04 — not the
2016-06-16 SAS used. The truncation date is neither the claim date nor
its supply end.

That leaves candidates 2 and 3, and a fourth worth adding: SAS builds
`_GroupWashForTrunk` inside a specific POV branch
(`ms_cidanum.sas:1766`), so the set may not receive every FUT claim in
the first place. Checking which claims actually reach it — rather than
which codes are labelled FUT — is the next step.

So the 854 remain unexplained, but the question is now specific. What is known: they are not degenerate,
not a fixed offset (410 within a week, 148 beyond a month), and the
truncation window and its bounds both match SAS. The next place to look
is how SAS builds the POV4 episode that `trunkdt` is computed over,
since a different episode BOUNDARY there would change which FUT claims
are in scope without either endpoint rule being wrong.

### Episode ENDS: the earlier measurement

Joining on (Group, PatID, IndexDt), 31,440 episodes match:

| | |
|---|--:|
| identical `EpisodeEndDt` | 28,363 (**90.2%**) |
| differing | 3,077 (9.8%) |
| median length, SAS vs here | 119d vs 119d |

The differences are **entirely one-directional**:

| | |
|---|--:|
| longer here | **3,077** |
| shorter here | **0** |
| median difference | +116 days |
| largest | +2,913 days |

Not one episode is shorter. That is a rule difference in episode
construction, not noise: something chains dispensings into one episode
that SAS keeps separate. `episodegap` is 30 with `episodegaptype = 'F'`
and `expextper` is 30 for this study, and those are the parameters to
examine.

One concrete case, same patient and same index dates:

| index | SAS end | end here |
|---|---|---|
| 2016-07-15 | 2016-08-14 (30d) | 2017-04-29 (288d) |
| 2017-05-08 | 2017-05-16 (8d) | 2017-09-04 (119d) |

This matters beyond the episode count: `episodelength`,
`followuptime`, `timetocensor` and the censoring flags are all derived
from the episode end, so a tenth of the rows carry wrong values for
them even where the episode itself is correctly identified.

### Where that leaves the episode comparison

| | before the fix | after |
|---|--:|--:|
| episodes only in SAS | 82 | **24** |
| episodes only here | 26 | 334 |
| total episodes | 31,408 | 31,774 (SAS 31,464) |

The fix is right — the role counts prove it — and it revealed a SECOND
defect it had been masking. The bogus blackout-event rule was removing
about 366 episodes: roughly 56 of them wrongly, and about 310 that SAS
also removes, but for a different reason this package does not
implement.

Those 310 look entirely ordinary: median episode length 120 days,
identical to the cohort as a whole, none short, none with an event. So
the missing rule is not about length or outcomes.

`all_events` is now 23 against SAS's 3, so outcome DETECTION is still
over-firing roughly eightfold on a single code — care settings or
`codetype` on that code are the obvious next checks.

## Which extract is which

The uploaded `parquet.7z` behaves as the **0.1%** sample, not the 1%.
Running the same study on it:

| | patients | episodes | denominator members |
|---|--:|--:|--:|
| **this extract** | 19,712 | **31,408** | **2,503,074** |
| SAS, 1st upload | 19,738 | 31,464 | 2,502,986 |
| SAS, 2nd upload | 196,714 | 303,246 | 25,015,666 |

It reproduces the FIRST upload to 0.18% on episodes and **0.0035% on
denominator members** — agreement that close is only possible on the
same patients. The second upload is 9.7x larger on every measure.

Three independent checks agree:

* **Patient ids.** Of the 36,731 patients in the new `mstr`, 47 appear
  in this extract. Sampling specific ids — 15188, 20040, 30101, 37116,
  105807 — none exists in its demographic, enrolment or dispensing
  tables.
* **Scale.** This extract holds 174,064 patients. A 1% sample of the
  same universe would hold roughly ten times that, which matches the
  second upload's 9.7x.
* **Id distribution.** Both span the same id space with near-identical
  quartiles, which is what two different draws from one population look
  like — similar shape, disjoint membership.

So the 1-2 episode differences cannot be explained by missing patients:
this extract and the first upload are the same population, and they
already agree to 0.1-0.2%.

**To compare against the new `mstr`, the 1% parquet extract is needed** —
the one that run used. With it, the residual episodes could be named
row by row instead of inferred from totals.

## Second upload: different data, but a new contract check

A second set of msoc tables arrived with `r01_mstr` in parquet. It is
NOT the same run as the first:

| | first upload | second upload |
|---|--:|--:|
| patients | 19,738 | 196,714 |
| episodes | 31,464 | 303,246 |
| denominator members | 2,502,986 | 25,015,666 |
| initial episodes (one cohort) | 10,270 | 94,369 |

About 9-10x larger throughout. Checked directly: of the 36,731
patients in the new `mstr`, **47 appear in the test SCDM** — 0.1%. The
patient-id RANGES overlap, which is why it looks similar at a glance,
but the populations do not.

So no numeric comparison is possible against this upload. The FIRST
upload did match the test extract's scale, which is why those numbers
agreed to 0.1-0.2%; that parity work stands.

### The mstr grain is confirmed

The second upload is internally consistent: for all 40 cohorts its
`mstr` row count equals `t2_cida.episodes` and its distinct `PatID`
count equals `t2_cida.npts`.

So **SAS's `<runid>_mstr` is the FINAL cohort — one row per surviving
episode**, not a pre-filter list. That confirms the
`cohort_final -> <runid>_mstr` mapping this package settled during the
output sweep, which had been reasoned from the macros rather than seen.

### What the mstr also settled: the per-episode column contract

This is the first sight of SAS's `<runid>_mstr` row shape, and it is
much wider than this package produces.

| | columns |
|---|--:|
| SAS `r01_mstr` | **92** |
| `cohort_final` here | 39 |

The 53 missing columns are not miscellaneous — they are whole
categories this package keeps in SEPARATE tables:

| group | count | examples |
|---|--:|---|
| covariate flags | 32 | `COVAR1` .. `covar32` |
| utilization counts | 10 | `NumAV`, `NumOA`, `NumIP`, `NumVisits`, `ExactNumVisit` |
| censor / follow-up flags | 13 | `cens_elig`, `cens_dth`, `fup_episend`, `fupdays_value_cat`, `Censorcat_sort` |
| other | 15 | `year`, `month`, `quarter`, `CCI`, `IndexLookEndDt`, `PeriodID`, `distindexexp`, `distindexhoi`, `death_source` |

**SAS's master list is one wide row per episode carrying everything** —
covariates, utilization, the comorbidity index and the censoring flags
all live on it. This package computes each of those but writes them to
`covariates`, `utilization` and `risk_scores` instead.

A data partner opening `<runid>_mstr` expecting the SAS shape would
find the columns absent, even though the values exist elsewhere in the
output. That is the same class of defect as `mstr` once pointing at the
wrong table — a contract mismatch rather than a wrong number.

Also confirmed from this file: the master list uses
**`fupdays_value_cat`**, while `followuptime_cida` uses
`censdays_value_cat`. Both names are real; they belong to different
tables.

Not yet implemented.

## The headline: every run so far used the wrong query period

`startdate` and `enddate` were read ONLY from the QRP_PARAMETERS
scalars, with a **hardcoded fallback to 2010-2015** when absent.

Both real studies state their period in the **MONITORING file**
instead, and neither sets the scalars. So both were silently run over
2010-2015 — a period that does not overlap either study.

## Consolidated state

Full suite: **270 passed, 2 skipped, 0 failed** across 272 tests.
mypy clean apart from 10 third-party stubs; ruff clean. A clean install
from the packaged zip passes `qrp doctor` 11/11.

### Defects found and fixed through this comparison

| # | defect | effect |
|---|---|---|
| 1 | query period defaulted to a hardcoded 2010-2015 | every study ran over the wrong years |
| 2 | denominator ignored exposed-plus-washout time | all 40 cohorts reported an identical denominator |
| 3 | denominator window bounded by `censor_date` | member-days 45% high, or members 1.4% high |
| 4 | `condinclusion = 0` read as INCLUDE | exclusions applied as requirements: 42,708 episodes became 58 |
| 5 | no query-period filter on index dates | SAS's largest single exclusion, never applied |
| 6 | denominator not shaved by exclusion conditions | member-days gap |
| 7 | `codetype` not used for matching | latent: 280 NDC codes matched procedure claims |
| 8 | outcomes routed on `indexcriteria` | 4,958 outcome codes instead of 1; 11,196 events against SAS's 3 |
| 9 | FUT codes had no role at all | every affected episode too long; no truncation |

Every one was invisible to a test suite that passed throughout.

### Also corrected

* All 22 macro citations audited: several named lines that say
  something else, and one pointed past the end of the file.
* Config registration moved to a bulk path: 91s to 3.6s on a
  40-cohort study, results byte-identical.

## Final parity position

| metric | this package | SAS | gap |
|---|--:|--:|--:|
| **outcomes (`all_events`)** | **3** | **3** | **exact** |
| denominator members | 2,503,074 | 2,502,986 | +0.0035% |
| denominator member-days | 2,269,346,378 | 2,268,962,961 | +0.017% |
| cohort patients | 19,712 | 19,738 | -0.13% |
| cohort episodes | 31,408 | 31,464 | -0.18% |
| episode END dates | 95.5% identical | | |

Per cohort: 20 of 40 exact on patients, 18 of 40 exact on episodes.

### The residue is systematic, not noise

Splitting the 40 cohorts by type shows a consistent signature:

| cohort type | washout | episodes | patients |
|---|--:|--:|--:|
| incident (`_inc`) | 183 | **+1** | +1 |
| prevalent (`_prev`) | 0 | **-2** | -1 |

Every one of the 20 incident cohorts is one episode HIGH; every one of
the 20 prevalent cohorts is two episodes LOW. That rules out data noise
and points at two separate boundary rules, each worth about 0.1%:

* **Incident, +1.** One episode survives here that the washout removes
  in SAS — a washout boundary, since these are the cohorts with
  `t2washper = 183`.
* **Prevalent, -2.** Two episodes are missing where there is NO
  washout. For a prevalent cohort SAS counts people whose exposure
  began BEFORE the query period and continues into it; the most likely
  explanation is that such an episode is anchored differently, and this
  package requires the defining claim itself to fall inside the period.

Checked and not the cause: index dates at the period boundaries. One
episode sits exactly on `start_date` and none on `end_date`, and index
dates stop at 2024-12-30 against a period ending 2025-04-30 — which
reflects where the data itself ends, not a rule.

### Step-by-step funnel comparison

SAS's attrition gives checkpoints. Building the same cumulative funnel
from this package, for the two cohorts that differ in `t2washper` only:

| checkpoint | prev (mine / SAS) | inc (mine / SAS) |
|---|---|---|
| index date within the query period | 1,821 / 1,820 | 1,152 / 1,156 |
| + pre-index enrolment | **1,410 / 1,410** | **779 / 779** |
| final | 1,373 / 1,375 | 766 / 765 |

**Three of the four steps are verified correct:**

* pre-index enrolment lands EXACTLY on SAS's count for both cohorts
* the exclusion criteria remove exactly 5 episodes for the prevalent
  cohort and 3 for the incident one — SAS removes 5 and 3
* `min_epis_dur` and `min_days_supp` remove nothing in either, as in SAS

The remaining difference is in the final censoring/blackout step, and
the ATTRIBUTION differs from SAS even where the totals nearly agree:

| | mine | SAS |
|---|--:|--:|
| removed by the blackout rule | 2 | 30 |
| removed by everything else after enrolment | 35 | 5 |
| **total removed** | **37** | **35** |

SAS books 30 episodes to "must be longer than blackout period"; this
package removes the same kind of episode through `indexdt <=
episodeenddt` — a censored episode ending before its own index date.
Same intent, different label, and two episodes' difference in the
result.

Identifying those two needs SAS's per-episode output (`DPLocal
<runid>_mstr`), which was not part of the upload. Totals cannot
localise a two-row difference.

### Suggested order for the remaining work

1. **Prevalent -2** — the largest of the three residuals and the one
   with a concrete hypothesis (pre-period exposure anchoring).
2. **Incident +1** — a washout boundary; comparing one cohort's
   episode list against SAS's `mstr` would identify the row directly.
3. **Denominator +1** — needs SAS's member list, or the dplocal
   `_DenomCounts` dataset, which was not part of the upload.

All three are around 0.1% and none would change a study's conclusions,
but each is a real rule difference rather than rounding.

Seven defects found and fixed through this comparison, every one of
them invisible to a test suite that passed throughout.

| study | real period | was used | episodes: was -> now |
|---|---|---|--:|
| wp307 | 2016-04-01 .. 2025-04-30 | 2010-2015 | 6 -> (see below) |
| wp322 | 2015-10-01 .. 2024-12-31 | 2010-2015 | 1,085 -> **2,599** |

**Every figure quoted for the production study in this repository's
docs was computed over the wrong years**, including the benchmarks —
they measured a cohort less than half the real size.

Nothing caught it because the fallback is silent and the fixture
studies DO set the scalars. It took real SAS output, where SAS found
31,464 episodes and this package found 6, to make it visible.

Fixed: the monitoring file is read (`indenddate`, else `fupenddate`
under FUPDRIVEN, per ms_processinputfiles.sas:996-1010), and a study
with no period anywhere now RAISES instead of defaulting.

## What could and could not be checked

The test SCDM turned out to be the extract already available, so a
NUMERIC comparison was possible after all.

### Denominators: a real bug, partly fixed

**They should be exact, and they were not.** The cause: SAS removes
exposed-plus-washout time from the denominator — a member is INELIGIBLE
from the day after an exposure claim until its supply expires plus the
washout, and those periods are shaved out of the enrolled window
(`ms_cidadenom.sas:461-478`). This package did not do it at all.

The symptom was unmistakable once compared per cohort: **`dennumpts`
was 62,730 for all 40 cohorts**, where SAS gives 62,462 for the
incident cohorts (washout 183) and 62,729 for the prevalent ones
(washout 0, where SAS skips the block entirely).

| | dennumpts total |
|---|--:|
| SAS | 2,502,986 |
| before | 2,509,200 |
| after the shave | **2,505,280** |

#### The optimisation had hidden it

`_denom_cfg_id`, which decides which cohorts share a denominator pass,
omitted `wash_per` AND the exposure codes — so all 40 cohorts collapsed
into ONE config and necessarily reported the same number.

The key-completeness test passed throughout, because it checks the key
against **what this package's SQL reads** — and the SQL was itself
missing the washout. **Validating an optimisation against the
implementation it optimises is circular.** Only external output could
show it. The study now resolves to 20 configs.

#### Second fix: the window was bounded by the wrong date

The denominator window ended at `least(enr_end, censor_date)`. It
should end at the **query period end**. Isolated by testing the
variants against real SAS output on one cohort:

| window rule | members | member-days |
|---|--:|--:|
| SAS literal (`Enr_Start+ENRDAYS` .. `Enr_End`) | 63,607 | — |
| clipped to the query period, no pullback | 2,538,084 | 2,272,191,504 |
| bounded by `censor_date` + pullback (before) | 2,505,280 | — |
| **query period + pullback** | **2,503,074** | **2,269,457,544** |
| SAS | 2,502,986 | 2,268,962,961 |

Neither half works alone: clipping without the pullback puts MEMBERS
1.4% high, and the pullback against `censor_date` puts MEMBER-DAYS 45%
high. Together they agree to **0.0035% on members and 0.02% on
member-days**.

Enrollment itself is exactly right: the window rule starts from 73,940
members for this cohort, which is SAS's attrition step 2 to the member.

#### The last denominator gap is downstream of the numerator

`ms_cidadenom.sas:145` shaves the denominator by the inclusion and
exclusion CONDITIONS (`excl_incl = Y`), using the same `CondInclusion`
field. So eligibility itself depends on the exclusion rules, and the
remaining member-level difference cannot be closed before those rules
parse correctly.

That reverses the natural order of work: the denominator cannot be made
exact independently of the numerator, because SAS derives part of it
from the same criteria.

#### Third fix: shave by the exclusion conditions

SAS shaves the denominator by the exclusion CONDITIONS as well as by
exposure (`ms_cidadenom.sas:145`, `excl_incl`), so eligibility depends
on the exclusion criteria and not only on enrolment.

The mapping is the inverse of the rule: an index at T is excluded when
a matching code falls in `[T + condfrom, T + condto]`, so a code at D
disqualifies indices in `[D - condto, D - condfrom]`.

| | member-days over SAS |
|---|--:|
| before | 494,583 |
| after | **383,417** |

It closed 22% of the remaining member-day gap and removed no members,
which is consistent: the exclusion codes in this study are rare — about
95 patients across the whole extract — so they rarely disqualify a
member's entire eligible period.

This could only be implemented after the `condinclusion` parser fix;
before it, the rules were being read as inclusions.

#### SOLVED: truncation codes lost their stockgroup

Tracing one patient's chain by hand showed every FUT claim sitting in
stockgroup `_default`, while the input file gives them all
`warfarinsodium` — and other codes different values entirely.

Two places dropped it:

* `config.py` captured `stockgroup` only `if key == "DEF"`.
* `pipeline.py` passed it through only `if role == "DEF"`.

So all 187,892 truncation codes landed in one bucket and chained
together regardless of drug. That is what pushed dates 23 days out on
one patient and four YEARS on another: unrelated medicines stacking
into a single supply run.

| | before | after |
|---|--:|--:|
| identical episode ends | 98.2% | **99.2%** |
| ends too short | 254 | **88** |
| ends too long | 322 | **158** |
| episodes | 31,488 | 31,448 (SAS 31,464) |
| patients | 19,752 | 19,728 (SAS 19,738) |

Both counts now UNDERSHOOT slightly, where before they overshot.

The lesson repeats one from earlier in this document: the answer was in
a column of the input file, not in the macro source. Three rounds were
spent reading `ms_stockpiling.sas` — correctly establishing that the
push rule, the same-day operator and the grouping all matched — while
the actual defect was that the grouping KEY was being thrown away
before the SQL ever saw it.

#### Truncation claims were not windowed

`trunc_claims` read the whole dispensing table, with no date filter,
while exposure claims are restricted to
`[start_date - widest_lookback, censor_date]`. SAS builds `_ITDrugs`
from the EXTRACTED claims, so its stockpile chain starts at the window
edge; this package's started at the beginning of the data and carried
extra accumulated drift into the study period.

| | before | after |
|---|--:|--:|
| identical episode ends | 99.2% | **99.6%** |
| ends too short | 88 | **40** |
| ends too long | 158 | **82** |
| episodes | 31,448 | 31,444 (SAS 31,464) |
| patients | 19,728 | 19,722 (SAS 19,738) |

#### A note on the two stockpiling inputs

They are easy to confuse and mean different things:

* **`stockgroup`** is a COLUMN on the cohortcodes file. This study
  populates it with 206 distinct values, and it is what decides which
  claims chain together. It is always present.
* **`STOCKPILE_NONCOVAR`** is a separate OPTIONAL file carrying
  `SameDay`, `SupRange`, `AmtRange` and `PercentDays` — the thresholds
  that decide WHETHER an overlap pushes. This study does not supply
  one, so SAS pushes unconditionally.

Every stockgroup value used here came from the cohortcodes column, not
from the absent parameter file.

#### What remains

576 of 31,426 ends still differ (322 long, 254 short). A worked case:

```
antixa_rupture_inc  patid 151125959  index 2016-06-10
  single exposure claim  2016-06-10  rxsup 30  expires 2016-07-09
  + expextper 30      ->  my end 2016-08-08
  SAS end                 2016-08-04      fup_spec = 1
```

`fup_spec = 1` means SAS truncated at 2016-08-04 — but **no claim of
any kind exists on that date**, and the patient's enrolment runs to
2019 with no death record. So 08-04 is a stockpiled truncation date.

This package's chain for that patient:

```
orig 2015-06-01 -> 2015-09-02      orig 2015-11-08 -> 2016-08-27
orig 2015-08-10 -> 2016-02-29      orig 2016-04-07 -> 2017-02-23
```

SAS's equivalent claim lands on 2016-08-04, 23 days earlier than this
package's 2016-08-27. The chains agree at the start and diverge as they
accumulate.

Checked and matching, so not the cause:

* **Same-day aggregation.** SAS defaults `SAMEDAY_SUPP` to `sum`
  (`ms_stockpiling.sas:262-271`) when no stockpiling file is present,
  which is what this package does.
* **Grouping.** SAS groups by
  `StockGroup indexcriteria dateonly fupcriteria`; all FUT claims here
  share the last three, so it reduces to stockgroup — as here.
* **The push rule** — `CLMDATE = lexpiredt + 1` unconditionally without
  a parameter file.

The divergence is therefore in HOW the chain accumulates rather than in
its inputs or its grouping. The next step is to reproduce SAS's chain
for this one patient by hand from `ms_stockpiling.sas:398-431` and find
the first claim where the two disagree — the algorithm is short enough
to trace exactly, and one patient is enough.: one member per cohort

| cohorts | difference |
|---|--:|
| 30 of 40 | **+1 member** |
| 10 | +2, +3, +7, +15 |

The +1 is not marginal on window length — adding 1 or 2 days to the
pullback does not move the count at all, so it is one member SAS
excludes for a reason not yet identified. At 0.0016% it is the last
thing to chase, not the first.

#### Earlier state, for the record

| cohorts | difference |
|---|--:|
| all 22 prevalent (washout 0) | **+1 member, every one** |
| incident | +72, +28, +14, +91, +2, +798, +4, +41, +86 |

The universal +1 is in the BASE window, not the shave: those cohorts
skip shaving entirely. SAS keeps a window only `if DenomEnrEndDt >=
DenomEnrStartDt` after adding `ENRDAYS`. Not yet traced to a member.

**The shave itself needs more verification.** On a two-cohort fixture,
adding a washout INCREASED `dennumpts` (154,300 -> 154,730): splitting
a window lets one member fall into two age bands, so a sum across
strata rows rises. That may be correct — SAS may do the same — but it
is unverified, so no test asserts a direction. It measurably improved
agreement on the real study, which is the only evidence for it so far.

### Previously reported as "within 0.25%"

| | dennumpts |
|---|--:|
| SAS | 2,502,986 |
| this package | 2,509,200 |

Enrollment spans, demographic filtering and age banding are therefore
substantially correct — that machinery is exercised end to end by the
denominator.

### Numerators: root cause found

SAS 31,464 episodes; this package 58. Traced the whole funnel:

```
pov1                          225,468
episodes                       78,240
after the episode filters      42,708   <- the episode stage is fine
ptsmasterlist                      58   <- 52_inclusion.sql
```

The episode stage is NOT at fault: running its query verbatim gives
42,708. The collapse is entirely in the inclusion/exclusion stage,
and the cause is in the PARSER.

This study's exclusion file uses a shape the parser does not handle:

| column | this study | assumed |
|---|---|---|
| `condinclusion` | `0` = EXCLUSION | a `criteria` column saying INC/EXC |
| `condlevel` | text: `Splenectomy_rupture_puncture` | a number |
| `subcondlevel` | text: `Exclusion` | a number |

Every one of the 57 rules for a cohort parses as:

```
criteria  'INC'      <- but condinclusion = 0 means EXCLUDE
cond      1          <- every distinct text label collapsed to 1
```

**So 57 exclusion rules are applied as inclusion REQUIREMENTS**: a
patient must have splenectomy codes to qualify. Almost none do, which
is exactly the 42,708 -> 58 collapse.

**One defect, now fixed.** `condinclusion = 0` must map to EXCLUDE.
There is no `indexcriteria` column in this file at all, and the parser
fell back to `INC`.

A second suspected defect turned out NOT to be one: `condlevel` and
`subcondlevel` are text labels here, and the parser already maps
labels to sequential numbers correctly. Every rule reported `cond=1`
because this cohort genuinely has ONE condition with one subcondition
— checked against the file, 57 rules all sharing
`Splenectomy_rupture_puncture` / `Exclusion`. Reporting it as a bug
was wrong.

### Effect of the fix

| | episodes | patients |
|---|--:|--:|
| before | 58 | 58 |
| **after** | **41,982** | **26,624** |
| SAS | 31,464 | 19,738 |

From 99.8% UNDER to about 33% over — a different regime entirely, and
the first time the numerator has been the right order of magnitude.

wp322 moved too (2,599 -> 4,703 episodes): it carries the same
`condinclusion` shape, so its exclusions were also being applied as
requirements.

## Numerators: essentially at parity

Reading SAS's own episode-level attrition steps — which enumerate every
exclusion individually — showed the largest one by far:

```
 7  Episode  10,270           Initial Episode Count
12  Episode   1,820  -8,450   Episode-defining index claims must be
                              during the query period
16  Episode   1,410    -410   pre-index enrollment
18  Episode   1,405      -5   exclusion criteria
23  Episode   1,375     -30   blackout
```

**Nothing in this package applied step 12.** Claims are extracted from
`start_date` minus the widest lookback, so that covariate and washout
windows can see history; without a filter, those lookback claims could
themselves become index dates.

One line in `50_episodes.sql`:

```sql
AND indexdt BETWEEN DATE '{start_date}' AND DATE '{end_date}'
```

| | members | episodes |
|---|--:|--:|
| before | 897 | 1,552 |
| **after** | **805** | **1,373** |
| SAS | 806 | 1,375 |

Across all 40 cohorts:

| | this package | SAS | difference |
|---|--:|--:|--:|
| patients | 19,712 | 19,738 | **-0.13%** |
| episodes | 31,408 | 31,464 | **-0.18%** |

From 33% OVER to under 0.2% under. wp322 moves from 4,703 episodes to
2,720, which is the same defect: index dates were being drawn from its
lookback window too.

### How it was found

By reading SAS's attrition table instead of guessing. Each of the three
previous hypotheses — member-level exclusions, `codetype`,
`caresettingprincipal` — was plausible and wrong. The attrition table
names every exclusion SAS applies and how many rows each removes; the
one this package was missing entirely stood out immediately.

### CORRECTION: the overcount is NOT in exposure

An earlier round of this document claimed the divergence "starts at
extraction", from this comparison:

| | |
|---|--:|
| my `exposure_claims` patients | 1,171 |
| SAS "members with cohort-identifying codes" | 1,113 |

**That comparison was invalid.** SAS's figure is attrition step 6 — it
counts members who have already passed steps 1-5 (non-missing
birth/sex, enrollment coverage, age range, chart availability,
demographics). My figure was raw extraction across all 174,064
patients, before any filter.

Comparing like with like:

| restriction | patients | vs SAS 1,113 |
|---|--:|--:|
| raw extraction | 1,171 | +58 |
| + enrolled | 1,145 | +32 |
| + claim within the query period | **1,119** | **+6** |

Extraction is essentially correct — 0.5% apart, and the remaining 6 are
plausibly the age and demographic filters SAS applies before counting.

Two things follow. **Exposure code selection is right**: this cohort
has 54 DEF codes in the parser and 54 in SAS. And the real divergence
is DOWNSTREAM — final members 897 against 806, episodes 1,552 against
1,375 — in episode construction or censoring, not in finding the
claims.

Ruled out before writing any code this time:

* `caresettingprincipal` — EMPTY for all 5,012 rows in this file.
* Role assignment — `indexcriteria` is `FUT` for 4,957 rows and `DEF`
  for 54. SAS sends FUT codes to a washout dataset
  (`ms_createmicohorts.sas:1766`), and the parser already keeps them
  out of DEF. The DEF counts match exactly.

### Superseded: the earlier exposure hypothesis

Comparing one cohort step by step against SAS's own attrition:

| step | this package | SAS |
|---|--:|--:|
| members with cohort-identifying codes | **1,171** | 1,113 |
| final members | 897 | 806 |
| final episodes | 1,552 | 1,375 |

**The divergence starts before any exclusion runs.** 58 extra members
are identified at extraction, and the gap widens downstream rather than
originating there. Chasing the exclusion criteria was chasing the wrong
stage.

#### Cause: `codetype` is not used for matching

The cohort codes for this one cohort span FIVE code systems:

| codecat | codetype | codes |
|---|---|--:|
| DX | 10 | 2,044 |
| PX | 10 | 3 |
| PX | HC | 21 |
| PX | ND | 70 |
| RX | ND | 2,874 |

`30_exposure.sql` uses `codetype` only in the deduplication key and the
`eventcount=1` key — **never as a matching condition**. Codes are
matched on `(code, codecat)` alone, so the same code string in a
different code system matches here and not in SAS. ICD-10-PCS, HCPCS
and NDC procedure codes share a namespace in exactly this way.

This is the same defect class as the exposure `codecat` bug from the
first review, one level deeper: config rows carry
`(code, codecat, code_supply)` and need `codetype` as well, with the
SQL filtering on it.

#### Implemented — and it did NOT explain the overcount

The config tuple went from three elements to four:

```python
exposure_codes: tuple[tuple[str, str, int | None], ...]
    # (code, codecat, code_supply)
  ->
exposure_codes: tuple[tuple[str, str, str, int | None], ...]
    # (code, codecat, codetype, code_supply)
```

with the same change to `event_codes` and `ioc_codes`, a `codetype`
column on `cfg_cohort_codes`, and the three domain joins in
`30_exposure.sql` gaining

```sql
AND (k.codetype = '' OR k.codetype IS NULL
     OR upper(x.codetype) = k.codetype)
```

An empty codetype means the input file did not say, and matches
anything, so files without the column behave exactly as before.
`cdm_dispensing` also had to start exposing `codetype` — the diagnosis
and procedure views already did, dispensing did not — resolved from the
extract like the code column rather than assumed, since SCDM extracts
vary.

**The fix is right, and the hypothesis was wrong.** It is a real latent
bug: this study has 280 `PX/ND` codes that were matching procedure
claims of codetype `C4`, `RE` and `HC` indiscriminately. But the
numbers did not move — 1,171 members before and after — because those
particular codes do not occur in the procedure table at all.

wp322 is unchanged at 4,703 episodes, and wp307 unchanged at 1,171
members. So the overcount is still unexplained, and the next candidate
is no longer obvious: `caresettingprincipal` (a cohortcodes column that
is parsed but may not be applied to DEF codes) is the one remaining
lead.

### Exclusion criteria: correct, and not the problem

Verified rather than assumed:

* Exclusion semantics match SAS: a condition excludes when ALL its
  subconditions are met (`ms_createpov3.sas:592-598`). This study has
  one condition with one subcondition, so any matching code excludes.
* The codes themselves are simply RARE in this data — across the whole
  extract only 224 diagnosis claims (57 patients) and 64 procedure
  claims (38 patients) match the 57 exclusion codes. They cannot
  account for a 6,900-patient difference in either direction.
* `cohort_claims` covers the cohort properly (4,823 patients against
  4,821 in the master list), so the exclusion is not searching a
  truncated claim set.

### What else has been ruled out

Patients are 35% over (26,624 vs 19,738) and episodes 33% over
(41,982 vs 31,464) — the same ratio, so this is extra PATIENTS, not
extra episodes per patient. About 6,900 patients are being kept that
SAS excludes.

Checked and NOT the cause:

* **`conduse`** — empty for every rule in this file.
* **Mixed codecat within one condition.** `cfg_inclusion_codes` stores
  codes with no `codecat`, so all 57 codes join to BOTH rule rows (the
  DX row and the PX row) and are searched in both domains. That is
  latently wrong — a code string occurring in two domains would match
  spuriously — but it is self-correcting here, because a DX code does
  not appear in the procedure table. It also over-matches rather than
  under-matches, so it cannot explain keeping too many.
* **`codetype`** (ICD-9 vs ICD-10) is not modelled. Same direction:
  ignoring it can only match MORE claims, so it cannot explain an
  overcount either.

Still to check: the exclusion WINDOW arithmetic (`condfrom = -183`,
`condto = -1` relative to index), and whether SAS applies the exclusion
at MEMBER level rather than per episode — SAS's attrition calls these
"Member" exclusions, and excluding a member removes all their episodes,
where excluding per episode removes only the overlapping ones.

That last one fits the evidence: it would remove strictly more, and the
attrition table labels the step `claim_level = Member`.

### Known but unfixed

`cfg_inclusion_codes` should carry `codecat` per CODE, the way
`exposure_codes` was fixed to carry `(code, codecat, code_supply)`
triples after the first review. It is not causing a visible error on
this study, but it is the same defect that once emptied two cohorts
entirely.

### Numerators: previously reported

SAS 31,464 episodes; this package 58. Localised through the funnel:

```
pov1            225,468
episodes         78,240
ptsmasterlist         58     <- the collapse is here
```

So exposure extraction and episode construction produce a plausible
78,240 episodes, and the master-list stage discards 99.9% of them.
That stage applies the enrollment requirement, `min_epis_dur` and the
blackout period. **The cause is not yet identified** and is the next
thing to chase.

Two candidates worth checking first: `expextper` (30 days here, and
this package stores it but the exposure extension may not be applied),
and `COHORTDEF` — a field with 35 references in the macros that this
package does not read at all.

## Column sets: four exact, one wrong

| table | result |
|---|---|
| `attrition` | **exact** — 6 columns, same order |
| `distindex` | **exact** — 4 columns |
| `distindexmap` | **exact** — 9 columns |
| `t2_cida` | **exact** — all 27 columns |
| `followuptime_cida` | **3 differences, all real** |

The four exact matches include every correction made during the output
sweep: the extra columns stripped from `attrition` and `distindex`, the
five columns added to `distindexmap`, and `year`/`month`/`quarter` plus
the five geography columns on `t2_cida`. Those were derived by reading
the macros and are now confirmed against real output.

## What the real output found that reading did not

### 1. `fupdays_value_cat` does not exist

The column is **`censdays_value_cat`**, plus a continuous
**`censdays_value`** and a sort key **`censorcat_sort`**.

`ms_createcensortable.sas:42` says it outright: "Continuous variable
censdays_value and categorical variable censdays_value_cat are included
in the aggregated MSOC table **regardless of which metric is being
output**." The name was inferred from the macro's `catvar=` parameter,
which is the input, not the emitted column.

### 2. `DROP_CENS_OUTPUT` is not handled

```sas
dplocalflaglist = cens_elig %if &DROP_CENS_OUTPUT. eq n
                  %then %do; cens_dth cens_qryend %end; cens_dpend
```

With `DROP_CENS_OUTPUT=Y` — which this run sets — `cens_dth` and
`cens_qryend` are **dropped**. This package emits them unconditionally.

The unread-parameter warning did not catch it: that list was built by
grepping for `%if "&param" = "Y"`, and this one tests `eq n`.

### 3. Censor strata are dynamic, not a fixed set

`censoring` and `followuptime_cida` carry exactly the columns named in
their levelvars. This study's levels use `censdays_value` and
`censdays_value_cat`, so the output has those and nothing else — no
`sex`, `agegroup`, `race`, `hispanic` or `year`.

This package emits the fixed demographic set, NULL where unused. That
is right for `t2_cida`, whose retain list is fixed — and it matched
exactly — but wrong here.

### 4. `claim_level` says "Member", not "Claim"

SAS uses `Member` and `Episode`. This package writes `Claim` and
`Episode`. "Claim" was chosen while implementing the attrition unit
fix; the value was never checked against real output.

### 5. Attrition is far more granular in SAS

**28 steps per cohort against 4.** SAS names each exclusion
individually — "Members must satisfy the age range condition within the
query period", "Members must meet chart availability criterion", and so
on — where this package reports one line per stage.

The columns are right and the numbers reconcile within themselves, but
a reader comparing attrition step by step will not find the same rows.

## The study loads and runs

`tools/sas_inputfiles_to_json.py` converts the sas7bdat input set,
following the QRP_PARAMETERS indirection.

| | |
|---|--:|
| tables converted | 12 |
| cohorts | 40 |
| cohort codes | 200,480 |
| inclusion rules | 2,280 |
| risk-score codes | 103,503 |
| parse time | 24.9 s |
| full run (against a different SCDM) | 123 s |

`t2_cida` came out at 40 rows, matching SAS — that one is structural
(40 cohorts x 1 level) and holds regardless of the data.

**Parsing takes 24.9 s**, most of it the 103,503 risk-score codes. That
is slow enough to notice and worth profiling; it was never visible on
the 1,124-code study used until now.

## To finish the parity check

The missing piece is the **test SCDM this run used**. With it,
`tools/ms_parity_export.sas` and `tools/parity_compare.py` would give a
value-by-value comparison rather than a structural one.


---

## Citation audit needed: Type 2 vs Type 4 macros

The `fupcriteria` routing above was originally cited to
`ms_createmicohorts.sas`. **That macro builds comparator/control
cohorts — Type 4 logic — and is not authoritative for Type 2.** The
Type 2 path is `ms_cidanum.sas`, which carries the same routing at
lines 1684 (`_FUPEvent`) and 1766 (`_GroupWashForTrunk`).

The two are NOT duplicates: 3,352 lines against 1,923, with different
content at the same line numbers. `ms_createmicohorts.sas:764` is
`indexdt_exp` — pregnancy-window logic that cannot be reached from
Type 2 at all.

The routing FIX is unaffected: it was confirmed against Type 2 source
and validated against SAS output (54 exposure / 1 outcome / 0 IOC,
matching exactly). Only the citation was wrong.

### Audit result

All 22 were checked by reading the cited line. The results were worse
than a wrong filename:

| cited line | what it actually says | verdict |
|---|---|---|
| 571 | `bypass creation of _pregcohort` | **pregnancy logic**, not CODESUPPLY |
| 764 | `indexdt_exp = indexdt2` | Type 4 — correct, that note IS about Type 4 |
| 1034 | `data _preg&groupind.` | pregnancy logic |
| 1388 | `indexcriteria in('IEV','EEV')` | right rule, wrong macro |
| 1685 | `by milID IndexDt` | multiple-incidence, not `_FUPWash` |
| **2117** | — | **past the end of a 1,923-line file** |

So several citations named lines that say something else entirely, and
one pointed beyond the end of the file. They looked precise and were
not evidence.

Corrected to the verified Type 2 locations:

| behaviour | now cites |
|---|---|
| outcome routing (`_FUPEvent`) | `ms_cidanum.sas:1684` |
| IOC washout (`_FUPWash`) | `ms_cidanum.sas:1664` |
| FUT truncation set | `ms_cidanum.sas:1766` |
| IEV/EEV to `_InclExclHOI` | `ms_cidanum.sas:1683` |
| inclusion/exclusion semantics | `ms_createpov3.sas` (exclusion rule at 592-598) |

Two citations were left pointing at `ms_createmicohorts.sas`
deliberately: the `INDEXDT_EXP` notes, which describe Type 4 pregnancy
anchoring and for which it is the correct source.

Two more are now marked "exact line unverified" rather than carrying a
number that does not support the claim — CODESUPPLY overriding RxSup,
and the contents of SAS's mstr. Both behaviours were established
elsewhere (CODESUPPLY in a code review, the mstr contents from the
uploaded file itself); only the macro reference was unfounded.

**The underlying behaviours appear sound** — the ones that mattered
were validated against SAS output, not just read. But a precise-looking
line reference is a claim, and several of these did not hold up.
