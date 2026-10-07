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

## Verified state

Full suite **275 passed, 2 skipped, 0 failed** across 277 tests. mypy
clean apart from 10 third-party stubs; ruff clean. A clean install from
the packaged zip passes `qrp doctor` 11/11.

| study | best | episodes |
|---|--:|--:|
| wp322 production | 5.14 s | 2,720 |
| wp307 (40 cohorts) | 22.20 s | 31,444 |

| metric | this package | SAS | gap |
|---|--:|--:|--:|
| episode END dates | **99.61% identical** | | 122 of 31,440 |
| outcomes | 3 | 3 | **exact** |
| patients | 19,722 | 19,738 | -0.08% |
| episodes | 31,444 | 31,464 | -0.06% |
| denominator members | 2,503,074 | 2,502,986 | +0.0035% |
| denominator member-days | 2,268,871,418 | 2,268,962,961 | -0.004% |

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


---

## Second review: four High findings addressed

| finding | status | effect |
|---|---|---|
| shared denominator ids merge different cohort definitions | **fixed** | the key now includes each cohort's EXCLUSION rules and the full exposure code identity (domain, vocabulary, supply), not just the code strings |
| denominator exclusions use numerator-filtered claims | **fixed** | the shave reads the domain tables directly; `cohort_claims` is materialised from the master list and holds only patients who reached an episode |
| missing source RX codetype silently removes exposures | **fixed** | a NULL SOURCE codetype cannot contradict the configured one — rejecting it removed every claim for an extract without the column |
| follow-up time uses the CIDA strata | **fixed** | `cfg_strata` is tagged by `tableid` and each consumer filters to its own; it was `cida_levels() or followuptime_levels()` |

The codetype finding was a defect introduced by this parity work itself
— the vocabulary predicate added two rounds earlier. It changes nothing
on the study compared, whose extract populates codetype everywhere,
which is exactly why only a review caught it.

Effect on parity: denominator member-days move from **383,417 OVER** to
**91,543 UNDER** SAS (0.004%). Episodes and patients are unchanged at
31,444 and 19,722.

### Three Medium findings, also addressed

| finding | fix |
|---|---|
| `t2_cida` coalesced intentionally NULL member-days to 0 | a MISSING denominator row still means zero, but a PRESENT row with NULL days stays NULL — `OUTPUTDENOM='M'` asks for members only, and `denomcounts` already wrote NULL. The two outputs now agree. |
| `eventcount=1` dedup omitted `codecat` | added to the partition key, matching SAS's `(PatId, Adate, codecat, codetype, code)`. Events are drawn from DX, PX and RX now, so a same-day diagnosis and procedure sharing a code collapsed into one. |
| rerun cleanup missed CSV copies | the sweep covers `{lib}/csv/` as well as `{lib}/`. A naming-mode switch left `<run>_censor_cida.csv` beside `<run>_censoring.csv`. |

Verified no regression: outcomes remain exactly 3, episodes 31,444,
denominator members 2,503,074.

### The three carried-over items, now fixed

| item | fix |
|---|---|
| enrolment file lacking the optional `chart` column | the column is resolved from the extract, like the dispensing code and codetype, and falls back to NULL. Referencing it unconditionally made a file carrying every REQUIRED column fail with a binder error — a valid extract could not be read at all. Verified by stripping `chart` from the fixture: same 61,723 episodes. |
| numeric lab criterion of `0` read as absence | `str(spec or "")` made a falsy 0 blank, so "result = 0" became "no criterion" and widened the extraction. Only None and blank mean absent now. |
| run-log collision race | the file is created EXCLUSIVELY (`open(..., "x")`) in a retry loop. Check-then-open let two runs in the same second both see the name free and both open it `"w"`, so one silently overwrote the other — the loss the suffix exists to prevent. |

None came from the parity work; all three were long-standing.

Also flagged and NOT actioned, correctly in the reviewers' judgement:
`92_cidadenom.sql` recomputes age at each shaved segment's start, so one
member can span two age bands. Whether that is right depends on SAS's
age-anchor semantics, which are not determinable from the material
here. It should be verified against a SAS reference before anyone
changes it.


---

## Residual after the review fixes: 122 episode ends

122 of 31,440 ends differ (82 long, 40 short), spread evenly at about
three per cohort across all 40 — no concentration to exploit.

Classified by whether truncation was involved:

| | count | mine truncated | SAS's end IS a truncation date |
|---|--:|--:|--:|
| long | 82 | 58 | **0** |
| short | 40 | 40 | 12 |

For the 82 long cases SAS's end is **never** a truncation date. Nor is
it any of:

| candidate | matches |
|---|--:|
| enrolment end | 0 |
| death date | 0 |
| censor date | 0 |
| uncensored episode end | 0 |
| an exposure expiry | 0 |
| an exposure expiry + `expextper` | 0 |
| a raw (unstockpiled) truncation date | 0 |
| a truncation date minus one day | 4 |

A worked case shows the shape: the truncation chain pushes a claim 60
days in this package and 58 in SAS, so the ends land two days apart.
The chains agree at the start and separate as they accumulate — the
same signature as the stockgroup and windowing defects already fixed,
but smaller and no longer explained by either.

One further hypothesis was implemented and measured:
`ms_cidanum.sas:617` keeps a dispensing when its SUPPLY reaches the
study start (`rxdate + rxsup - 1 >= studystartdate`), not when its fill
date does. Applying that to the truncation claims is
**output-identical** here — no chain in this study is affected by the
claims it excludes. It is kept because it matches the documented
extraction, not because it changed anything.

At 0.39% of episode ends, with patients and episodes both within 0.08%,
this is the point where the remaining explanations are cheaper to get
from a SAS-side `QRP_DEBUG=Y` run — which writes the intermediate
datasets directly — than from further inference.


---

## Debug datasets: the residual is now directly comparable

A `QRP_DEBUG=Y` run supplied the per-cohort intermediates — `_fut`,
`_groupwashfortrunk`, `_pov1`, `_pov4`, `_ptsmasterlist`, `_fupevent`,
`_inclexcl` and the per-level attrition sets, for all 40 cohorts. The
truncation chain can now be compared against SAS's own, instead of
inferred from episode ends.

### `_fut` — the truncation dates

For `antixa_rupture_prev`:

| | |
|---|--:|
| SAS `_fut` rows | 332 |
| this package | 348 |
| keys present in both | 317 |
| **same `trunkdt`** | **308** |

The nine that differ are all LATER here, by 2, 4, 6 and 10 days.

### `_groupwashfortrunk` — the claims those dates come from

| | |
|---|--:|
| SAS rows | 65,532 |
| this package | 70,840 |
| only in SAS | 2,918 |
| only here | **8,304** |

**This package carries about 8% more truncation claims than SAS**, and
each extra claim adds another push to the stockpile chain. That is the
mechanism behind every remaining difference: same cadence, more steps.

One patient makes it concrete — both chains step exactly 90 days, but
with 37 entries here against SAS's 35:

```
SAS  2015-08-06  2015-11-04  2016-02-02  2016-05-02 ...  ends 2023-07-25
here 2015-05-18  2015-08-16  2015-11-14  2016-02-12 ...  ends 2023-11-02
```

### A correction, and a caution

An earlier entry recorded the supply-reach window
(`rxdate + rxsup - 1 >= studystartdate`) as "measured, output-identical".
**It was never applied** — the search string in that edit had a
trailing space and the replacement failed silently, so the measurement
was of unchanged code. Applied properly it is measurably WORSE: exact
ends 99.61% to 99.45%, with short cases rising from 40 to 98. It is not
in the build.

That is the second time an edit here reported a result it had not
actually produced. Any change whose measurement shows NO movement at
all should be treated as suspect until the code is confirmed changed.

### The excess is concentrated, not uniform

Comparing chain LENGTH per patient against `_groupwashfortrunk`:

| | patients | extra rows here |
|---|--:|--:|
| chains identical in length | **4,091** | 0 |
| longer here | 1,991 | +4,862 |
| longer in SAS | 46 | -73 |

and the patient sets themselves differ: 6,373 here against SAS's 6,128,
so **245 patients carry a truncation chain here that SAS has none for
at all**.

Two-thirds of patients match exactly. That rules out a systematic
difference in the stockpiling algorithm — which would perturb every
chain — and points at a per-PATIENT difference in what enters the set.

**The 245 extra patients turn out to be nearly harmless**: only 8 of
them reach `cohort_final` at all, so the truncation dates computed for
the rest are never consulted. Restricting the set to patients with an
exposure claim removed them and changed parity not at all — while
over-restricting the set to 1,137 patients against SAS's 6,128, so that
is not SAS's rule either. Reverted.

The difference that MATTERS is therefore the 1,991 patients whose
chains are 2-3 claims longer, not the patient roster. Those patients
are in both sets; SAS simply has fewer claims for them. Diffing the raw
claims behind one such chain against `_groupwashfortrunk`, date by
date, is the remaining question — and both sides are now on disk.

### Two more hypotheses tested and reverted

| tried | result |
|---|---|
| supply-reach window (`rxdate + rxsup - 1 >= studystartdate`) applied to the truncation set | **worse** — exact ends 99.61% to 99.45%, short cases 40 to 98 |
| `rxamt > 0` scoped to the truncation set only (it had already measured worse applied globally) | **worse** — 99.61% to 99.57%, and it removed only 45 of the 5,308 excess rows |

Neither is in the build. The second is worth noting as a near-miss:
`ms_cidanum.sas:617` really does filter `rxsup > 0 and rxamt > 0`, but
that clause guards a different extraction than the one feeding
`_groupwashfortrunk`.

### SOLVED: the chain must start at the enrolment window

The debug datasets made the diff possible. A patient with ONE entry in
SAS and two here:

```
patid 5372
  SAS _groupwashfortrunk :  2015-08-08
  here                   :  2015-05-11,  2015-08-09
  raw FUT claims         :  ... 2015-05-11 (90d), 2015-08-08 (90d)
```

The 2015-05-11 claim's 90-day supply ends exactly **2015-08-08**. With
it in the chain the 08-08 claim overlaps by one day and is pushed to
08-09; without it there is no overlap and 08-08 stands. SAS excludes
the earlier claim, so its truncation date is a day earlier — and that
one day is the episode-end difference.

**A truncation claim enters the chain only if its SUPPLY still runs at
the start of the required prior-enrolment window**
(`start_date - enr_days`). Taking every claim back to `claims_from`
pulled in claims SAS never sees, and each extra one pushes everything
after it further forward.

| | before | after |
|---|--:|--:|
| identical episode end dates | 99.61% | **99.97%** |
| ends too SHORT | 40 | **0** |
| ends too long | 82 | **8** |
| truncation claim rows | 70,840 | 66,964 (SAS 65,532) |
| episodes | 31,444 | 31,440 (SAS 31,464) |

### And then: non-dispensing exposure is not stockpiled either

The last eight differences were all ONE patient, two episodes across
four cohorts. The claims are filgrastim J-codes — procedure-sourced
exposure, not dispensings:

```
patid 60364873, three pairs of same-day J1442 administrations
  09-19 x2, 09-20 x2, 09-21 x2   (rxsup 1 each, no rxamt)
  chained here ->  09-19..09-20, 09-21..09-22, 09-23..09-24
  + expextper 30                   episode ends 2018-10-24
  SAS                              episode ends 2018-10-21
```

Two defects in one place:

* **Chaining.** SAS stockpiles `_ITDrugs` only, so procedure- and
  diagnosis-sourced exposure keeps its own dates. This had already been
  fixed for the truncation set and not for exposure.
* **Same-day supply.** Two administrations of the same drug on one day
  are ONE day of exposure. Summing them added a day per repeat.

With both corrected the last exposure day is 2018-09-21, and
`09-21 + 30 = 2018-10-21` — SAS's answer exactly.

### SOLVED: the membership gap, by the same rule

24 episodes were in SAS's master list and not here; none were here and
absent from SAS. Traced through the funnel:

| stage | of the 24 |
|---|--:|
| exposure claims exist on that date | 24 |
| reach `index_candidates` / `pov1` | 18 |
| reach `episodes` | 2 |
| reach `ptsmasterlist` | 0 |

A worked case — SAS has ONE episode for the patient, starting
2016-08-15; this package chained that date into a longer episode and so
never produced it as an index:

```
claims on their OWN dates : ... 2016-04-13 (90d, expires 2016-07-11)
                                2016-08-15   -> gap 35 days  > episodegap 30
                                             -> SAS starts a NEW episode
stockpiled here           : ... 2016-05-12..2016-08-09
                                2016-08-15   -> gap 5 days   -> merged
```

The accumulated push had closed a gap SAS leaves open. **The exposure
chain needed the same start rule as the truncation chain**: a
dispensing joins only if its supply still runs at
`start_date - enr_days`.

| | before | after |
|---|--:|--:|
| episodes only in SAS | 24 | **0** |
| episodes only here | 0 | **0** |
| episodes | 31,440 | **31,464 — exactly SAS** |
| patients | 19,722 | **19,738 — exactly SAS** |

## Episode-level parity is exact

| metric | this package | SAS | |
|---|--:|--:|---|
| patients | 19,738 | 19,738 | **exact** |
| episodes | 31,464 | 31,464 | **exact** |
| episode END dates | 31,464 / 31,464 identical | | **exact** |
| outcomes | 3 | 3 | **exact** |
| denominator members | 2,503,074 | 2,502,986 | +0.0035% |
| denominator member-days | 2,268,871,418 | 2,268,962,961 | -0.004% |

Every episode SAS produces, this package produces, with the same index
date and the same end date. The only remaining difference in the whole
comparison is the denominator, at 88 members in 2.5 million.

### Episode ends: the earlier measurement

| | |
|---|--:|
| matched episodes | 31,440 |
| **identical `EpisodeEndDt`** | **31,440 (100.000%)** |
| too long | 0 |
| too short | 0 |

Worth noting how this was found. Three hypotheses reasoned from the
macro source all failed — the supply-reach window against the study
start, the amount filter, the patient roster. The answer came from
diffing ONE small chain against SAS's own dataset and asking what made
those two specific claims different. The debug data turned a search
over rules into a search over rows.

### Next step

Diff the 8,304 claims present here and absent from
`_groupwashfortrunk` against the 2,918 in the other direction. Both
sets are on disk; the question is which FILTER SAS applies to the
truncation claim set that this package does not, and the answer is a
single query away rather than another hypothesis.


---

## The mstr column contract

SAS's `<runid>_mstr` is one wide row per episode carrying everything.
This package computed the same values and wrote them to `covariates`,
`utilization` and `risk_scores`, so 53 of SAS's 92 columns were absent
from the master list even though the numbers existed elsewhere.

| | columns |
|---|--:|
| SAS `r01_mstr` | 92 |
| before | 39 (53 missing) |
| **after** | **101 (8 missing)** |

Added: the 32 covariate flags, the utilization counts under SAS's
names, `year`/`month`/`quarter`, `PeriodID`, `IndexLookEndDt`,
`RawDisp`, `AdjustedDisp`, `TotRxSup`, `TotRxAmt`, `ttc`, and both the
`fup_*` and `cens_*` censoring families — SAS writes the same flags
under two names (ms_finalizeptsmasterlist.sas:394).

Episode count and every parity figure are unchanged: 31,464 episodes,
19,738 patients, ends exact.

### The columns follow the STUDY, not this study

Nothing about the shape is hardcoded. The covariate flags are
generated from `study.covariates`, so a study with 15 covariates gets
15 columns and a study with a non-contiguous set gets exactly those
numbers:

| study covariates | mstr columns |
|---|---|
| 1..12 | covar1 .. covar12 |
| 1, 2 | covar1, covar2 |
| 1, 5, 9 | **covar1, covar5, covar9** |

A hardcoded `covar1..covar32` would have been right for this study and
wrong for every other one.

The utilization counts are gated on the stage having run: a study with
no `utilfile` gets no `NumAV` at all, rather than a column of zeros.
Two tests pin both behaviours, including the non-contiguous case.

### The eight NOT added, and why

`cci`, `censorcat_sort`, `death_enctype`, `death_source`,
`distindexexp`, `distindexhoi`, `exactnumvisit`, `fupdays_value_cat`.

Each needs a source this package does not model — the Charlson index,
the death-record provenance fields, the distribution-index identifiers
and the follow-up-day categorisation. **They are omitted rather than
filled with zeros**, so their absence stays visible to anyone
comparing. Writing a plausible 0 into `CCI` would be worse than leaving
the column out.

### A gap this exposed in the stage table

The stage was declared in `STAGES` and `run()` never called it, so it
silently did nothing — the table says which stages EXIST, but `run()`
still orders them by hand. `test_plan_and_run_cannot_disagree_about_stages`
checks that every stage names a real SQL file and a real gate; it does
NOT check that `run()` actually invokes each one. That is worth
closing: the declaration and the execution can still drift apart in
this one direction.


---

## The denominator: what is and is not known

With episodes exact, the denominator is the only numeric difference
left: **+88 members in 2,502,986** (0.0035%), and member-days **91,543
UNDER** (-0.004%).

The per-cohort shape is unchanged by every episode fix:

| difference | cohorts |
|---|--:|
| +1 | **30** |
| +2 | 4 |
| +3 | 2 |
| +7 | 2 |
| +15 | 2 |

Thirty cohorts off by exactly one member says a single member qualifies
here and not in SAS, repeated across cohorts that share a denominator
configuration.

### Checked with the debug data

`attrition_level2` is SAS's list of the 4,738 members it excluded at
the enrolment step. **Not one of them survives this package's
enrolment filter** — so the enrolment rule is at least as strict as
SAS's, and the extra member is not being let in there.

Members OVER while member-days are UNDER is itself informative: it is
not one rule applied too loosely. It looks like an extra member with a
short eligible window, plus slightly too much time shaved elsewhere.

### The denomcounts dataset settled half of it

`r01_denomcounts` gave per-cohort member and member-day counts, and the
per-cohort split was the clue:

| cohort | member difference |
|---|--:|
| hctz_rupture_inc / hctz_splenec_inc | +15 |
| war_rupture_inc / war_splenec_inc | +7 |
| doac_*_inc | +3 |
| antixa_*_inc | +2 |
| all 20 `_prev` | +1 |

**Every large difference was an `_inc` cohort** — the ones with a
washout. The washout shave was computing its ineligible window from
`exposure_claims`, the RAW claim dates, where SAS uses the STOCKPILED
exposure: `ADate + 1` to `ExpireDt + washper`
(ms_cidadenom.sas:461-478). Raw dates end the window early, so members
re-enter the denominator sooner than SAS allows.

| | before | after |
|---|--:|--:|
| member excess | +88 | **+40** |
| cohorts with a per-cohort excess above 1 | 8 | **0** |
| member-days | -91,543 | -140,321 |

All 40 cohorts are now uniformly +1 member. The member-day figure moved
the wrong way — a stockpiled expiry is later than a raw one, so the
shave grew — and both numbers are now about 0.005% of their totals.
The change is kept because it is what the source specifies and because
it removed the entire structured part of the error; the remaining
member-day gap is uniform rather than concentrated, which is a better
starting point than the mixture it replaced.

### The shave source is `_groupindex`, not every claim

`ms_cidadenom.sas:459` shaves from `_groupindex` — the stockpiled
exposure JOINED TO ENROLMENT — not from the full claim set. A
dispensing filled outside any enrolment span never makes a member
ineligible.

The set sizes confirm it: SAS's `_groupindex` has **10,706** rows for
`antixa_rupture_inc`; this package's stockpiled exposure has 10,927,
and restricting it to claims inside an enrolment span gives **10,707**.

Applied as a semi-join. It is **output-identical on this study** — the
221 excluded claims all fall outside the denominator windows anyway —
and is kept because it is what the macro specifies, with that
measurement recorded so it is not mistaken for a fix.

### Tried and rejected

| tried | result |
|---|---|
| drop the `denom_end` pullback, now that the shave is correct | far worse: members +35,052, member-days +2,592,961. The pullback is essential; it is one day per member, and the residual deficit is 0.056 days per member, two orders smaller. |

That bounds the problem usefully: the remaining -140,321 member-days
cannot be a whole-day boundary rule applied to every member. It is the
shave removing slightly too much from a small number of members.

### The degenerate-period guard

`ms_cidadenom.sas:467` drops periods whose start is past their end
(`if UneligStart<=UneligEnd;`) BEFORE merging. On its own such a period
shaves nothing, but carried into a running-max merge it can still
extend a block, so the order matters.

Now applied, between the raw periods and the merge. **Output-identical
on this study** — no degenerate periods arise here — and confirmed
applied by inspecting the file rather than inferring it from the
unchanged numbers, which is how the same edit was mis-reported twice
before.

### The member-day gap is entirely the EXCLUSION shave

Disabling that branch alone separates the two problems cleanly:

| exclusion shave | members | member-days |
|---|--:|--:|
| OFF | +40 | **+445,775** |
| ON | +40 | **-140,321** |

So this package removes 586,096 days where SAS removes 445,775 —
**31% too much** — and the member count is untouched either way. The
+40 members and the -140,321 days are two INDEPENDENT problems, not one
rule with two symptoms.

Checked and ruled out as the cause:

* **Subcondition logic.** SAS shaves per `(COND, SUBCOND)` with
  condition-level combination, which would over-remove if applied as a
  flat union. But this study has exactly one condition with one
  subcondition per cohort — 57 code groups all at `cond=1, subcond=1`,
  window `(-183, -1)` — so a union over the codes IS the right
  semantics here. Not the cause on this study, though it remains a real
  structural difference for studies that use several subconditions.
* **The claim window.** SAS shaves from `_IT<inclusioncodes>`, the
  extracted claim set; this package read the raw domain tables
  unbounded. Now windowed to match — **output-identical**, because the
  extra claims produce shave periods outside the denominator windows
  anyway. Kept as correctness, recorded as a no-op.

The 31% excess is therefore in the shape of the shaved period rather
than in which claims feed it.

#### Reading the shave loop, which is NOT symmetric

`ms_cidadenom.sas` branches on the SUBcondition flag, and the two sides
do opposite things:

| `SUBINCLUSION` | builds | calls |
|---|---|---|
| 1 | `EligStart` / `EligEnd` | **`%ms_shaveoutside`** — keep only time INSIDE |
| 0 | `UneligStart` / `UneligEnd` | shave the period OUT |

This study's exclusion rules carry `condinclusion = 0` (the CONDITION
is an exclusion) with `subcond_inclusion = 1` (the SUBCONDITION is an
inclusion), so SAS takes the **shaveoutside** path and then combines at
the condition level using `&INCLUSION`. This package takes the periods
straight to a shave-out. Those are not obviously the same operation.

An empirical check argues the net polarity is nonetheless right:
disabling the shave gives +445,775 days and enabling it -140,321, with
SAS between the two. An inverted polarity would not land 31% off; it
would be wildly wrong. So the composition is probably equivalent and
the length differs.

Both branches take the period END from `ExpireDt` rather than `ADate`
when `dateonly = 'N'` (lines 197-201 and 305-308). `ExpireDt >= ADate`,
so that makes SAS's period LONGER and would have it shave MORE — the
opposite of what is observed. **That rules the `ExpireDt` difference
out as the cause**, which is worth recording because it was the
obvious next thing to try and it would have been wrong.

#### SOLVED: inclusion codes matched across domains

`cfg_inclusion_codes` carried no `codecat`. The join matched a claim
against the RULE's domain, and every rule in this study sits at
`cond = 1, subcond = 1` — 57 of them, spanning DX and PX. So a DX code
matched PX claims and a PX code matched DX claims, because a sibling
rule always supplied the other domain.

That is the same defect class as the exposure join's missing
vocabulary check, in a different table.

| | before | after |
|---|--:|--:|
| denominator member-days | **-140,321** | **+8,033** |
| as a share of 2.27 billion | -0.0062% | **+0.0004%** |
| days removed by the shave | 586,096 | 445,775 wanted |

A 17-fold improvement, and the cohort figures are untouched: 19,738
patients, 31,464 episodes, 3 outcomes, all still exactly SAS.

#### What the reading did and did not buy

Three candidates were eliminated by reading the macro before any code
changed — the period-end `ExpireDt` rule (wrong direction), the
subcondition combination (one subcondition here), and the `codedays`
overlap path (`codedays = 1` throughout). Each would have been a
plausible change and each would have been wrong.

The actual cause was found by checking a REGISTERED TABLE's columns
rather than the macro: `cfg_inclusion_codes` was missing a field it
needed. That is the third defect in this comparison found by looking at
what the code feeds itself rather than at what SAS says — after the
`stockgroup` on truncation codes, and the `codetype` on exposure
claims.

#### Where that leaves it

The over-removal is not explained by: which claims feed the shave
(windowed, no change), subcondition combination (one subcondition
here), or the period end rule (wrong direction). What has not been
checked is the condition-level combination — how `&INCLUSION` merges
the subcondition results — and the `codedays > 1` overlap path, which
builds `overlap_start` as a MAX across repeats and would shorten
periods for codes requiring several occurrences.

That is the next place to look, and it wants the macro read carefully
rather than another guess: three changes reasoned from this file today
were tested and reverted, and the one that worked came from diffing
data, not from reading.

### What remains: one member per cohort

### Why this stops here

The debug folder carries the POV and attrition intermediates — `_pov1`,
`_pov4`, `_fut`, `_groupwashfortrunk`, `_ptsmasterlist`,
`attrition_level*` — but **no denominator intermediates**. There is no
`_denom*` dataset, so the row-by-row diff that solved the truncation
chain in two queries cannot be repeated here.

`DPLocal.<runid>_DenomCounts` arrived and settled the structured half
of the gap (see above). What it cannot settle is the rest: it carries
one row per cohort at level 000, so it gives TOTALS, not members. The
remaining +1 per cohort is a single member per denominator
configuration, and naming that member needs a member-level dataset —
`_UneligGroupIndex`, `_DenomEligible`, or whatever `ms_cidadenom`
leaves behind under `QRP_DEBUG`.

Everything inferable from the macro text has now been applied:
stockpiled dates, the `_groupindex` enrolment restriction, and the
degenerate-period guard. The two that were output-identical are marked
as such. What is left is not a rule this package has wrong in a way the
source reveals — it is one member in 62,000 per cohort, and the next
honest step is data rather than more reading.

At 0.0035%, with patients, episodes, episode end dates and outcomes all
exact, this is the last open numeric item and the smallest one.


---

## The remaining +40 members

Full suite **286 passed, 2 skipped, 0 failed** across 288 after the
domain-matching fix, which touches every inclusion path.

| metric | this package | SAS | gap |
|---|--:|--:|--:|
| patients | 19,738 | 19,738 | **exact** |
| episodes | 31,464 | 31,464 | **exact** |
| episode END dates | 31,464 / 31,464 identical | | **exact** |
| outcomes | 3 | 3 | **exact** |
| denominator members | 2,503,026 | 2,502,986 | +0.0016% |
| denominator member-days | 2,268,970,994 | 2,268,962,961 | +0.0004% |

One extra member per cohort, uniformly across all 40. Since 40 cohorts
share 20 denominator configurations, that is roughly twenty members.

### Checked and eliminated

| candidate | finding |
|---|---|
| missing demographics | none: every member has a sex (89,306 F / 84,758 M) and a birth date |
| degenerate windows | none: no segment has `memberdays <= 0`, and none has `denom_start > denom_end` |
| zero-day members | none: the smallest total is 1 day, held by 10 members |

The extra members are ordinary. They are not being admitted by a
boundary that lets an empty window through.

### Further inference: five more candidates eliminated

| candidate | how checked | finding |
|---|---|---|
| enrolment span construction | compared against SAS's own `Enr_Start`/`Enr_End` in `_pov1` | **all 1,171 spans match exactly**, zero mismatches — SAS's enrolment bridging and this package's agree |
| an age restriction | study config | the study sets no age bounds, and its only stratum is level 000, so age cannot move a member count |
| washout involvement | grouped the gap by `enr_days` / `wash_per` | both groups are +1 per cohort and ~+4,000 days: the cause is in the SHARED base window, not the washout |
| window bounds | checked every segment against the query period | none starts before it or ends after it; min 2016-04-01, max 2024-12-30 |
| demographics and degenerate windows | as above | complete, and no empty or negative segment |

The `_pov1` comparison is the strongest of these. Enrolment is the
foundation the denominator is built on, and it is now verified
identical to SAS's at the span level rather than assumed.

### What would settle it

The +8,033 member-day surplus over +40 members is about 200 days each,
which is consistent with the extra members accounting for the whole
day surplus as well — so the two remaining figures are probably ONE defect, not
two. That is a change from the earlier position, when the exclusion
shave made them independent.

Localising it needs a member-level denominator dataset —
`_UneligGroupIndex`, `_DenomEligible`, or whatever `ms_cidadenom`
leaves under `QRP_DEBUG`. `r01_denomcounts` gives totals only, and
totals have now been pushed as far as they go: every structured part of
the gap is closed, and what remains is twenty named individuals that no
aggregate can identify.


---

## Third review (at 0915de2): six remaining findings

The review confirmed ten earlier concerns closed and listed six open.
All six are now addressed.

| severity | finding | fix |
|---|---|---|
| **High, carried over** | EVENT/IOC joins omit vocabulary matching | both joins now apply the same policy as the exposure join. The event source also hardcoded `NULL AS codetype` on its RX branch, discarding the dispensing vocabulary entirely — that is fixed too. |
| **Medium, new regression** | follow-up-time-only studies export an EMPTY censoring table | the fallback checked for "no strata at all" while the main branch filtered to `t2cida`, so a study defining only `t2followuptime` levels satisfied neither. Scoped the check to `t2cida`. **This regression was introduced by my own tableid split**, which shows the value of a review pass after a fix. |
| **Medium** | an extract without optional `race` still crashes | optional-column resolution is now GENERAL, covering `chart`, `race`, `hispanic`, `postalcode` and `postalcode_date`, including the demographic tie-break `ORDER BY`. Verified against an extract with all five removed: identical results, 61,725 episodes. |
| **Low** | non-drug truncation lacks the drug branch's missing-vocabulary policy | branches aligned. |
| **Low** | stale-CSV assertions can pass when stale files remain | rewritten against a clean run, and **verified to fail when cleanup is deliberately broken** — the previous version could not fail at all. |
| (carried) | malformed lab criteria still disable the filter after a warning | a nonempty unparseable criterion now RAISES. A warning does not prevent silent broadening, because warnings are not read before results are. |

Parity is unaffected: 19,738 patients, 31,464 episodes, 3 outcomes,
all still exactly SAS.

### One test left deliberately weak, and named as such

`test_event_and_ioc_joins_enforce_vocabulary` is STRUCTURAL — it
asserts the shipped SQL carries the predicate rather than observing a
claim being excluded. A behavioural version needs a fixture whose event
codes are defined ONLY with a vocabulary; the bundled demo study also
defines them without one, and a permissive entry matches everything, so
a behavioural assertion against it passes whether or not the fix is
present. Two attempts at a behavioural test both passed vacuously
before this was understood, which is exactly the failure the review
caught in the CSV test.


---

## `_UneligGroupIndex`: the denominator shave, compared row by row

The 20 per-cohort unelig datasets made the shave directly comparable.
For `antixa_rupture_inc`:

| | |
|---|--:|
| SAS rows | 10,706 |
| this package | 10,707 |
| patients, SAS and here | **1,143 / 1,143 — identical** |
| rows matching exactly | 10,071 |
| rows differing | ~635 |

The patient SET is identical. What differs is dates, and the START of
every differing row matches — only the END moves.

### Found: SAS clips the supply at the enrolment end

Joining to `_pov1`, which carries `rxsup` and `Enr_End`, 47 rows had a
different supply. **46 of them have `ExpireDt = Enr_End` exactly**: SAS
truncates a dispensing's supply where enrolment stops, before adding
the washout. This package carried the full supply, so 46 ineligible
windows ran past where SAS ends them.

Applied — and **output-identical**, because the denominator window
itself already ends at `enr_end`, so shaving beyond it changes nothing.
Kept as correctness, recorded as a no-op, like the two before it.

### The remaining difference is chain drift, and it is now visible

The periods only in one output are the same patients with shifted
dates:

```
patid  98573092   SAS 2019-10-22   here 2019-11-10   (+19 days)
patid 116846041   SAS 2021-05-28   here 2021-06-03   (+6 days)
patid 116846041   SAS 2021-08-31   here 2021-09-01   (+1 day)
```

Always later here, never earlier — the signature of a stockpile chain
carrying claims SAS does not have, exactly as the truncation chain did
before its start rule was fixed.

The likely cause is ORDERING. This package chains ALL exposure claims
and then restricts to enrolled ones; `_groupindex` is the exposure
JOINED TO ENROLMENT, so SAS plausibly restricts first and chains
second. Filtering after chaining leaves the pushes from out-of-enrolment
claims baked into every later date.

That is testable and is the next change — but it must be made in the
DENOMINATOR path only. The episode path is exactly correct at 31,464
episodes with every end date matching, and it is built from the same
`stockpiled` table, so changing that table risks the parity already
achieved. The denominator needs its own chain, built from enrolled
claims, rather than a filter applied to the shared one.


### Filtering before chaining: implemented, measured, reverted

A denominator-specific chain was built from ENROLLED claims only,
separate from `stockpiled` so the episode path stayed untouched. The
result splits:

| measure | before | after |
|---|--:|--:|
| unelig rows matching SAS EXACTLY | 10,071 | **10,417** |
| unelig rows produced | 10,707 | 11,108 (SAS 10,706) |
| cohorts exact on members | 0 | **4** |
| member excess | +40 | +44 |
| member-day excess | +8,033 | +66,151 |

The DATES got closer and the row COUNT got worse, which locates the
remaining difference precisely. Restricting before chaining fixes the
drift — 346 more rows land on SAS's exact dates — but it also admits
claims the old path excluded.

The reason is which date the enrolment test uses. The old path filters
on the STOCKPILED adate, so a claim pushed past the enrolment end is
dropped; the new one filters on the RAW adate, so it is kept and
chained. SAS's `_groupindex` carries stockpiled dates alongside
`Enr_Start`/`Enr_End`, which does not by itself say which date the join
used.

Reverted, because the totals are worse and the ordering question is not
settled by the evidence available. The next attempt should restrict on
the RAW date before chaining AND drop rows whose stockpiled date leaves
the enrolment span — combining both filters rather than choosing
between them. That is a single testable change with both measurements
already recorded to judge it against.


### Both filters combined — and the inference that follows

The specified experiment was run: restrict on the RAW date before
chaining AND drop rows whose stockpiled date leaves the span.

| measure | baseline | raw filter only | both filters |
|---|--:|--:|--:|
| unelig rows (SAS 10,706) | 10,707 | 11,108 | **10,945** |
| rows matching SAS EXACTLY | 10,071 | 10,417 | **10,417** |
| member excess | **+40** | +44 | +44 |
| member-day excess | **+8,033** | +66,151 | +66,151 |

The second filter removed 163 rows and moved the denominator totals
NOT AT ALL. Those rows lay outside the denominator windows entirely.

**This is the useful finding, and it points away from the shave.**
Making the unelig periods measurably closer to SAS's — 346 more rows
on exactly the right dates — makes the denominator totals WORSE, and
consistently so. If the shave were the cause, accuracy there would
improve the totals.

So the +40 members and the day surplus are NOT primarily a shave
problem. The shave's remaining imperfection is real but it is not what
is moving those numbers; something in the BASE eligible window is, and
the closer-to-SAS shave simply removes a compensating error.

That reframes the next step. Rather than continuing to refine the
unelig periods, the base window construction — `_denom_windows`, its
start and end rules and the pullback — should be compared against a
SAS reference. The shave has now been compared row by row against
SAS's own dataset and is close; the base window has never been
compared against anything except totals.

All three variants are reverted. The baseline stands at +40 members
and +8,033 member-days.


## The base window, read from source at last

`ms_cidadenom.sas:126-139` builds it plainly:

```sas
where Enr_End >= &startdate.;
DenomEnrStartDt = Enr_Start;
DenomEnrStartDt = DenomEnrStartDt + &ENRDAYS.;
DenomEnrEndDt   = Enr_End;
if DenomEnrEndDt >= DenomEnrStartDt;
```

**No clipping to the query period, and no pullback of any kind.** The
clipping happens later and implicitly, where member-days are counted
by intersecting the window with each TIME PERIOD
(ms_cidadenom.sas:577-590):

```sas
if PeriodsOverlap(period1=DenomEnrStartDt DenomEnrEndDt,
                  period2=PER&p .. PER&p1-1) then
    NumMemDaysYM(&p) = sum(...,
        Min(DenomEnrEndDt, PER&p1-1) - max(DenomEnrStartDt, PER&p) + 1);
```

A member is counted when their window overlaps any period; days
outside every period contribute nothing.

### Why this matters, and why it is not a one-line fix

This package clips `denom_start` and `denom_end` to the query period
and then subtracts a PULLBACK — a term derived empirically, by trying
variants until the totals matched, not read from the macro. The source
has no such term.

But removing the pullback measures far worse (+35,052 members,
+2,592,961 member-days), so it is compensating for something real. The
candidate is the PERIOD boundaries: SAS clips to `PER&p1 - 1` of the
last period, which need not be the query period end. This study's
query period runs to 2025-04-30 while its data ends 2024-12-30, so if
SAS's final period stops at the data end, the effective clip differs
from this package's by four months.

That is the thing to check next, and it is checkable: compare against
SAS's period definitions rather than guessing at a correction term.
Replacing a fitted constant with the rule it was standing in for is
the same move that fixed the truncation chain, the stockgroup and the
inclusion-code domain — each of which looked like a tuning problem
until the underlying rule was found.


### The period-boundary hypothesis, eliminated

The monitoring file settles what the periods are:

```
periodid 1   startdate 2016-04-01   fupenddate 2025-04-30   cdpend 'Y'
```

`cdpend = 'Y'` censors at the data partner end, so the single period
runs 2016-04-01 to the data end rather than to 2025-04-30 — which was
the suspected four-month discrepancy.

**It makes no difference here.** The extract's enrolment ends
2024-12-31, and this package's `denom_end` is
`least(enr_end, end_date)`, so enrolment already bounds the window
below both candidate period ends. The clip is the same either way.

So the fitted pullback is NOT standing in for the period boundary. It
remains unexplained, and it remains the only fitted constant in the
package.

What that leaves, for whoever picks this up: the pullback is one day
per member, applied at `denom_end`. Removing it adds 35,052 members and
2.59 million member-days, so it is doing real work — but no rule in
`ms_cidadenom.sas` produces it, the base window there has no such term,
and the period intersection does not either. Either it compensates for
a difference in the ENROLMENT spans that feed the window (though those
were verified identical against `_pov1` at the span level), or for
something in how a member qualifies that has not yet been located.

The honest summary of this line of work: the denominator shave has been
compared row by row against SAS's own dataset and is close; the base
window has been read from source and this package differs from it by a
term that cannot be justified from the source but cannot be removed
without making the numbers much worse. That contradiction is the next
thing to resolve, and resolving it probably requires SAS's
`_denom<group>` dataset — the base window before shaving — which would
show the window boundaries directly.


## `_denom<group>`: the +1 member is now a named patient

The base-window datasets made the last difference addressable.

| | SAS `_denom1` | here |
|---|--:|--:|
| rows | 68,803 | 67,680 |
| patients | 63,409 | 62,463 |
| earliest window start | **2010-07-03** | 2016-04-01 |

SAS does NOT clip the window start to the query period — starts run
back to 2010, exactly as `ms_cidadenom.sas:135-137` says
(`Enr_Start + ENRDAYS`, no clipping). The period intersection then
reduces 63,409 patients to the 62,462 that reach `denomcounts`.

Restricting SAS's set to windows that overlap the query period and
diffing against this package's:

| | |
|---|--:|
| SAS, overlapping the period | 63,337 |
| here | 62,463 |
| **only here** | **1** |
| only in SAS (shaved away entirely) | 875 |

**One patient. That is the +1.**

### The patient, and what distinguishes them

```
patid 39853274
  enrolment  2010-01-01 .. 2015-10-31
             2016-01-01 .. 2016-12-31      <- a two-month GAP
  window here  2016-07-02 .. 2016-12-30    (2016-01-01 + 183, less the pullback)
  SAS          no row at all
```

This package treats the two spans separately, so the second one starts
its own 183-day qualifying clock on 2016-01-01 and qualifies on
2016-07-02. SAS has no row for this patient in `_denom1` at all.

#### Gap-bridging: checked and ruled out

The obvious reading was that SAS BRIDGES the two spans into one, so the
183-day clock starts in 2010 rather than 2016. `ms_episoderec.sas`
exists for exactly that. But the call site passes **`ENROLGAP=0`**
(ms_cidanum.sas:663) — no bridging at all. The hypothesis is wrong, and
the patient's raw enrolment confirms the spans are genuinely separate:

```
2010-01-01 .. 2010-12-31   medcov Y  drugcov Y
2011-01-01 .. 2015-10-31   medcov Y  drugcov Y
2016-01-01 .. 2016-12-31   medcov Y  drugcov Y
```

#### Where the patient actually leaves SAS's pipeline

They are NOT in `attrition_level2` (the enrolment step), and they ARE
in `attrition_level6`. So SAS carries them through enrolment and drops
them at level 6.

That is as far as the available data goes, and it leaves a question
rather than an answer: level 6 excludes 72,827 of the 73,940 that pass
level 2, leaving 1,113 — the EXPOSED patients. The denominator is
62,462, which cannot come from that path. So either the denominator
input is taken before level 6 and something else excludes this patient,
or `_denom1` is built from a set this comparison has not identified.

What IS established, and is the useful result:

* the +1 member is ONE named patient per denominator configuration,
  reproducibly identified by diffing against `_denom<group>`
* SAS does not clip the window start to the query period; this package
  does, and that difference is real but is not what admits the patient
* the patient has three clean enrolment spans, valid demographics, and
  a window this package computes as 2016-07-02 to 2016-12-30
* gap-bridging, demographics, degenerate windows, window bounds, the
  period boundary and the washout are all eliminated

The remaining question is narrow and concrete: what does
`&datain.` — the dataset `ms_cidadenom` is invoked on — contain, and
which step removes patid 39853274 from it.


## SOLVED: enrolment that outlives the member

The `_denom<group>` datasets reduced the +1 per cohort to one named
patient, and the patient explained it:

```
patid 39853274
  death        2015-10-26
  enrolment    2010-01-01 .. 2010-12-31
               2011-01-01 .. 2015-10-31
               2016-01-01 .. 2016-12-31   <- ENTIRELY AFTER DEATH
  window here  2016-07-02 .. 2016-12-30
  SAS          no row at all
```

The extract carries an enrolment span that begins two months after the
member died. SAS truncates the span to the death date before building
the window and deletes it when that leaves the end before the start
(ms_cidanum.sas:2184-2188):

```sas
if enrend_death in ('B','C') and DeathDt <= Enr_End then Enr_End = DeathDt;
if Enr_end < Enr_Start then delete;
```

This package took the span at face value and gave a dead member a
six-month eligible window.

| | before | after |
|---|--:|--:|
| denominator members | +40 | **+0 — exactly SAS** |
| cohorts exact on members | 0 / 40 | **40 / 40** |
| denominator member-days | +8,033 | **+753** (0.00003%) |

### A detail that cost two attempts

The first fix aliased the truncated value as `enr_end` in the same
SELECT that computes `denom_end` — and SQL does not expose a column
alias to its siblings, so `denom_end` kept reading the raw
`e.enr_end` and NOTHING CHANGED. The numbers were identical before and
after, which looked like "the hypothesis is wrong" rather than "the
code did not run". The rule written down earlier in this document —
a change showing NO movement at all is suspect until the code is
confirmed changed — applied exactly, and checking the patient directly
rather than the totals is what caught it.

## Final parity

| metric | this package | SAS | |
|---|--:|--:|---|
| patients | 19,738 | 19,738 | **exact** |
| episodes | 31,464 | 31,464 | **exact** |
| episode END dates | 31,464 / 31,464 | | **exact** |
| outcomes | 3 | 3 | **exact** |
| denominator members | 2,502,986 | 2,502,986 | **exact** |
| denominator member-days | 2,268,963,714 | 2,268,962,961 | +0.00003% |

Every count matches. 753 member-days in 2.27 billion remain.


### The last 753 member-days

Distributed with clear structure:

| difference | cohorts |
|---|--:|
| +7 | 14 (`*_rupture_prev`) |
| +33 | 14 (`*_splenec_prev`) |
| +26 | 3 (`*_splenec_inc`) |
| +6 | 2 (`*_rupture_inc`) |
| +32 | 2 (`*_splenec_inc`) |
| +3 | 2 (`*_rupture_inc`) |

**The split is by EXCLUSION CONDITION, not by exposure.** A rupture
cohort and a splenectomy cohort differ only in their exclusion codes,
and the splenectomy ones carry roughly 26 more excess days each. So the
residual is in the exclusion-condition shave — this package removes
slightly LESS time than SAS for splenectomy exclusions.

That is where to look next, and the structure says it is a property of
the codes rather than of the exposure or enrolment logic, both of which
are now exact. The likely candidates are the `dateonly` handling on
non-drug exclusion codes (SAS takes the period end from `ExpireDt`
rather than `ADate` when `dateonly = 'N'`, which for a procedure code
is the same date but need not be for every code) and the care-setting
conditions on exclusion codes, which this package does not apply to the
denominator shave.

At 753 days in 2,268,962,961 — 0.00003% — with every count exact, this
is the smallest remaining difference in the comparison.


---

## The test extract truncates `rxamt` — and what that changes

The pyqrp investigation found that the SCDM-to-parquet conversion wrote
`dispensing.rxamt` as an INTEGER, turning fractional amounts (a 0.6 mL
prefilled syringe) into 0. The duckqrp test extract has the same
defect:

| | |
|---|--:|
| `rxamt` type | `INTEGER` |
| dispensings | 4,478,080 |
| `rxamt = 0` | 54,995 |
| fractional values | **0** |

Real dispensing data always contains fractional amounts; zero of them in
4.5 million rows is the signature of truncation.

### It does NOT explain the remaining denominator gap

`92_cidadenom.sql` never reads `rxamt`. Both denominator shaves are
driven by stockpiled DATES (from `rxsup`, which is genuinely whole days)
and by exclusion conditions, which in this study are all DX/PX codes.
The last 753 member-days were already traced to the rupture-vs-
splenectomy exclusion shave, which touches no drug claim.

### It DOES reverse an earlier conclusion

An earlier entry recorded applying SAS's dispensing filter
`rxsup > 0 and rxamt > 0` (ms_cidanum.sas:617), measuring it worse
(episodes 31,444 -> 31,404), reverting it, and concluding that the
filter "evidently guards a different dataset".

**That conclusion was wrong.** The filter was dropping dispensings whose
true amount was fractional — kept by SAS, which reads the real values,
but stored as 0 in this extract. The measurement was correct; the
inference drawn from it was not. With a correctly converted extract
the filter is SAS's rule and should be restored, then re-measured.

### It exposed two output columns never compared before

`t2_cida.amtsupp` and `daysupp` against SAS:

| cohort | SAS `amtsupp` | here | | SAS `daysupp` | here | |
|---|--:|--:|--:|--:|--:|--:|
| `peg_rupture_prev` | 411 | 1 | -99.8% | 885 | 1,069 | +21% |
| `fil_rupture_prev` | 541 | 126 | -76.7% | 1,134 | 1,431 | +26% |
| `antixa_rupture_prev` | 545,937 | 153,984 | -71.8% | 227,167 | 299,197 | +32% |
| `hctz_rupture_prev` | 3,225,336 | 953,345 | -70.4% | 1,391,318 | 1,501,888 | +8% |

Two definitional defects in `90_cidatables.sql`, independent of the
input bug:

* **`amtsupp` sums the INDEX dispensing's amount only** (`c.rxamt`),
  while `daysupp` sums the whole episode (`c.episode_rxsup`). Days and
  amount are measured over different things, so every cohort's amount
  is low by roughly the share of non-index dispensings. Truncation adds
  to this for the injectables, which is why peg falls to almost zero.
* **`daysupp` is not clipped to the episode window.** SAS computes
  `TotRxSup` from dispensings within `[IndexDt, EpisodeEndDt]` via its
  utilization macro (`refend=EpisodeEndDt`, ms_createptsmasterlist.sas:
  210-240), which is why a truncated episode earlier carried
  `TotRxSup = 17` against a 90-day dispensing. This package sums
  uncapped supply, so days run high wherever an episode is censored or
  truncated.

Neither is fixed yet: `amtsupp` cannot be verified against SAS until the
extract carries real amounts.


---

## The denominator, resolved to 34 days

### Correction: the pullback was never a fitted constant

Earlier entries described the denominator's `denom_end` pullback as
"derived empirically, not read from source" and "the only fitted
constant in the package". **That was wrong.** It is SAS's own formula,
term for term (ms_cidadenom.sas:709):

```sas
AdjustedEnrEndDt = min(Enr_End, &censordate.) - Max(0, &MinEpisDur.-1,
    &MinDaySupp.-1, &BlackoutPer., &reqdaysaftind.,
    &reqdaysaftepi. + max(&MinEpisDur.-1, &MinDaySupp.-1, &BlackoutPer.));
DenomEnrEndDt = min(DenomEnrEndDt, AdjustedEnrEndDt);
...
if MemberDays > 0;
```

It also explains the 1,000 one-day segments SAS's `_denom` export has
and this package does not: SAS trims them to zero length at this step
and drops them with `MemberDays > 0`, exactly as this package does
earlier. They are not a discrepancy.

### Found: SAS shaves the denominator around OUTCOME events

`ms_cidadenom.sas:486-515` removes member-time around every follow-up
event claim, across the whole eligible population:

```sas
UneligStart = sum(ADate, -&BLACKOUTPER., 1);
UneligEnd   = ExpireDt + &FUPWASHPER.;          /* dateonly = 'N' */
```

This package had no outcome shave at all. The clue was that its
rupture and splenectomy denominators were IDENTICAL while SAS's differ
by 23 days — and the two cohorts' exclusion lists turned out to be
identical too (57 shared codes), leaving the outcome as the only thing
that distinguishes them.

Fixed in two places: the shave itself, and the denominator config key,
which now includes `fup_wash_per` and the outcome codes so cohorts that
differ only in outcome no longer share a denominator.

| | before | after |
|---|--:|--:|
| denominator member-days vs SAS | +753 | **-34** |
| cohorts exact on members AND member-days | 0 / 40 | **30 / 40** |

Every prevalent cohort is now exact, along with ten incident ones.

### What remains

-34 member-days across ten incident cohorts, -1 to -5 each, in
identical rupture/splenectomy pairs that differ by drug. Outcome-
independent and drug-dependent places it in the WASHOUT shave — the
stockpile-chain ordering already measured against `_UneligGroupIndex`
(10,071 of 10,706 periods exact).

### Not yet implemented

The matching IOC washout shave (POV6, ms_cidadenom.sas:521-540: start
`ADate + 1`, end `ExpireDt + FUPWASHPER`). wp307 has no IOC codes, so
it cannot be verified here and was left out rather than added untested.
The event shave also takes each claim's end as `ADate + CodeSupply - 1`;
a `dateonly = 'Y'` code with a supply would differ, and no such code
exists in this study.


### The remaining 34 days: the washout chain ordering, re-tested properly

An earlier entry rejected building the denominator's washout chain from
ENROLLED claims ("filter before chaining") because it measured far worse
(+66,151 member-days). **That experiment was flawed**: its chain kept
only `codecat = 'RX'` claims, silently dropping every procedure-sourced
exposure — the peg/fil J-codes — from the washout shave altogether.

Re-run correctly (procedure claims kept, unchained, as SAS treats them;
the same chain-start rule as the exposure chain):

| | baseline (kept) | enrolled-first chain |
|---|--:|--:|
| cohorts exact on members and days | 30 / 40 | **32 / 40** |
| other incident cohorts | -1 to -5 | **-1** (six cohorts) |
| warfarin incident pair | -1 each | **+181 days, +1 member each** |
| total member-days | **-34** | +356 |

Enrolled-first fixes the drift for 38 of 40 cohorts but loses what looks
like one 183-day washout window for one warfarin patient. A plausible
mechanism: a dispensing filled just outside an enrolment span whose
STOCKPILED date falls inside it — kept when chaining happens before the
enrolment join (as SAS's `_groupindex` is built), dropped when it
happens after. Neither ordering alone reproduces SAS. Chaining within
each enrolment span would reconcile both observations, but that is a
hypothesis, not a measured result.

Reverted; the baseline has the smaller total error. Naming that one
warfarin patient by diffing against `_uneliggroupindex` for the warfarin
incident cohort is the cheapest next step.


### The warfarin patient, named — and the chain rule it implies

Diffing the warfarin incident cohort against `_uneliggroupindex37`
named the patient the enrolled-first chain loses: **patid 64384723**.
Two things are unusual about them:

* every dispensing is recorded TWICE (two identical 30-day rows per
  fill), so stockpiling sums each fill to 60 days while refills come
  every ~30 — the chain drifts years ahead, past 2027;
* their enrolment has a GAP: 2014-2021, then 2023.

For this patient the baseline (chain every claim, then join to
enrolment) matches SAS exactly; enrolled-first discards the claims filed
during the 2022 gap and breaks the chain. Yet enrolled-first was closer
for most other patients. Both observations fit one rule: **chain claims
within the patient's OVERALL enrolment range** — drop claims before the
first span or after the last, keep those in gaps between spans — then
join to enrolment on the stockpiled date.

| | baseline | enrolled-first | **overall range** |
|---|--:|--:|--:|
| member-days vs SAS | -34 | +356 | **-24** |
| cohorts exact | 30 | 32 | **32** |
| cohorts worse than baseline | — | 2 | **0** |

Adopted. Remaining: -24 member-days in eight incident cohorts
(antixa/doac -4, apix -3, riva -1), still in rupture/splenectomy pairs,
so still the washout chain.


## The denominator is exact

### Found: SAS restarts the washout chain in each enrolment span

Diffing `antixa_rupture_inc` against `_uneliggroupindex1` after the
overall-range rule left only five patients. One of them showed the rule
directly:

```
patid 98573092   enrolled 2011-01-01..2019-07-31, then 2019-10-01..2020-02-29
fills            2019-05-13 (90d), 2019-07-22 (90d), 2019-10-21 (90d)
continuous chain 2019-10-21 pushed to 2019-11-09 by July's leftover supply
SAS              2019-10-21 stays on 2019-10-21
```

SAS's stockpile chain for the denominator RESTARTS at every enrolment
span. That also reinterprets the warfarin patient (64384723): SAS's 2023
periods step by exactly 30 then 60 days from 2023-01-27 — a fresh chain
of 2023 fills, not one inherited from 2014-21. The overall-range rule
had fixed that patient only incidentally, by dropping the claims that
let drift cross the gap.

| | baseline | overall range | **per-span chain** |
|---|--:|--:|--:|
| member-days vs SAS | -34 | -24 | **0** |
| cohorts exact on members AND days | 30 / 40 | 32 / 40 | **40 / 40** |
| washout periods matching SAS exactly | — | — | **194,122 / 194,122** |

The last row is the strongest evidence: not just the totals but every
one of the 194,122 washout periods across all 20 incident cohorts is
identical to SAS's `_UneligGroupIndex`.

A behavioural test pins it — the first fill in every span must keep its
own date — and was confirmed to FAIL with the per-span reset removed.

## Final parity on wp307

| metric | this package | SAS | |
|---|--:|--:|---|
| patients | 19,738 | 19,738 | **exact** |
| episodes | 31,464 | 31,464 | **exact** |
| episode end dates | 31,464 / 31,464 | | **exact** |
| outcomes | 3 | 3 | **exact** |
| denominator members | 2,502,986 | 2,502,986 | **exact** |
| denominator member-days | 2,268,962,961 | 2,268,962,961 | **exact** |
| `daysupp` | | | +21 days in 6.4M |
| `amtsupp` | | | -0.016% |

Every count in the comparison now matches SAS exactly. What remains is
in the supply/amount columns: 11 episodes' supply and 599 episodes'
prorated amount differ slightly, most likely a same-day aggregation
detail.


## Getting the speed back: outcome scopes

Putting the outcome into the denominator config key made the
denominator exact, but doubled its cost: rupture and splenectomy cohorts
no longer shared a config, so every member's window was computed twice.

| | before the outcome fix | outcome in key | **outcome scopes** |
|---|--:|--:|--:|
| denominator configs | 20 | 40 | **20** |
| rows in each per-member table | 1.36M | 2.71M | **1.36M** |
| denominator stage | ~5s | 12.5s | **6.1s** |
| wp307 end to end | ~25s | 34.5s | **24.8s** |
| cohorts exact | 30 / 40 | 40 / 40 | **40 / 40** |

The outcome touches only 38 members in this study. The config key is
outcome-free again, and each cohort's event patients are computed in two
small scopes alongside the shared one:

    counts(cohort) = '*' (all members, shared periods)
                   + 'a:<cohort>' (its event patients, shared + event periods)
                   - 'b:<cohort>' (the same patients, shared periods only)

Member-days and distinct members are both additive over disjoint sets
of patients, so this is exact rather than an approximation — confirmed
by all 40 cohorts still matching SAS.

The test for it had to be rebuilt to mean anything. Its first version
compared two demo cohorts with DIFFERENT exposures, which have separate
configs anyway, so removing the line that keeps each cohort's scopes to
itself changed nothing and the test still passed. It now builds two
cohorts identical except for the outcome, asserts that they share a
config, and was confirmed to FAIL with that line removed.

---

## Covariates: first comparison with SAS, and what it found

Covariates had never been compared with SAS per episode. Against SAS's
master-list flags on wp307 (32 covariates):

| fix | SAS-only flags | mine-only flags |
|---|--:|--:|
| (start: every flag doubled) | — | — |
| `createbaseline`: covariates and baseline only for 'Y' cohorts | 783 | 252 |
| each covariate code keeps its own category | 447 | 252 |
| combinations evaluated in covariate-number order | **276** | **252** |

* **createbaseline.** SAS computes covariates and a baseline table only
  for cohorts with `createbaseline = 'Y'` in the cohort file
  (ms_cidacov.sas:142) — the 20 rupture cohorts here. The field reached
  the JSON but was never read. Baseline: 40 rows -> 20, matching SAS.
* **Per-code category.** A covariate can mix dispensing and procedure
  codes; taking the category from the first row matched only one.
  'Pegfilgrastim Post-Index' found 15 of SAS's 224 episodes.
* **Combination order.** SAS evaluates combinations one at a time in
  covariate-number order (ms_cidacov.sas:1201-1220), so covar32 sees
  covar31. All 171 of covar32's missing episodes recovered.

### Next: covariate dispensings are STOCKPILED in SAS

The remaining two-way mismatches are post-index drug covariates
(`dateonly = 'Y'`, days 1-30). SAS stockpiles covariate dispensings
(ms_cidacov_codeextraction.sas:590-672, 855-905): clipped to enrolment
spans, chained per patient by `covarnum` and `stockgroup` with the
STOCKPILE_COVAR parameters (defaults as for exposure), clipped to
enrolment again. This package uses raw fill dates.

A crude test — one chain per patient, no stockgroups, no enrolment
clipping — already cuts the mismatches sharply (SAS-only / mine-only):

| | raw dates | crude chain |
|---|--:|--:|
| covar22 warfarin | 67 / 32 | 5 / 10 |
| covar19 apixaban | 8 / 55 | 0 / 24 |
| covar23 hydrochlorothiazide | 107 / 160 | 59 / 95 |

covar23 improves least, as expected: hydrochlorothiazide spans many
stockgroups (combination products) that SAS chains separately.

**Implemented**, following SAS's sequence: covariate dispensings clipped
to the cohort's enrolment spans, chained per (enrolment configuration,
patient, covariate, stockgroup) with same-day supply summed, then
clipped to enrolment again. Drug covariates are looked up in that chain;
everything else as before.

| | SAS-only flags | mine-only flags | covariates exact |
|---|--:|--:|--:|
| before stockpiling | 276 | 252 | 22 / 32 |
| **stockpiled** | **14** | **99** | **28 / 32** |

Four still differ: covar14 (0 / 23), covar15 (0 / 38), covar22 (5 / 10)
and covar23 (9 / 28). Mostly flags SAS does NOT have — a sign the chain
admits claims SAS leaves out, as the exposure chain did before its
chain-start rule.

The test for it was mutation-checked and initially proved nothing:
clipping to enrolment alone moves fills (onto a span start), so "some
fill moved" held with the chain bypassed. It now counts only moves that
clipping cannot explain, and fails when the chain is removed.


### Covariate chain-start rule: 31 of 32 exact

The remaining mismatches were mostly flags SAS does NOT have, concentrated
in the PRE-index drug covariates — the signature of a chain admitting
claims SAS leaves out. The exposure chain's entry rule applies to
covariate dispensings too: a dispensing whose supply ends before the
cohort's enrolment window opens (`start_date - enr_days`) never enters
the chain, so old fills cannot push later ones forward.

| | SAS-only flags | mine-only flags | covariates exact |
|---|--:|--:|--:|
| stockpiled, no entry rule | 14 | 99 | 28 / 32 |
| **with the entry rule** | **0** | **1** | **31 / 32** |

`enr_days` is part of the chain key, so cohorts with different values
never share a chain. One flag remains: covar15 (hydrochlorothiazide
pre-index), one episode flagged here and not in SAS.

Over this session, covariates went from every flag doubled to
12,364 against SAS's 12,363.


### The last covariate episode: a fill straddling an enrolment gap

Patient 124844251 (hctz_rupture_prev, index 2023-08-15) is enrolled
2019-07-01..2022-09-30 and 2023-01-01..2024-12-31, with a 180-day fill
on 2022-07-19 that straddles the gap. Clipping to enrolment rightly gives
one piece per span — but each piece kept the FULL 180-day supply, so the
2023 piece pushed the patient's 2023 fills forward into the pre-index
window. Clipped, the pieces carry 74 and 14 days.

## Covariates: exact

| | SAS | this package |
|---|--:|--:|
| covariate flags, all 32 covariates | 12,363 | **12,363** |
| flags SAS has, this package does not | | **0** |
| flags this package has, SAS does not | | **0** |
| baseline table rows | 20 | **20** |

From every flag doubled at the start of this work. Six fixes, each found
by comparing per episode against SAS's master list: createbaseline,
per-code categories, combination order, stockpiled covariate
dispensings, the chain's entry rule, and clipped supply across
enrolment gaps.


## Amounts: medical exposure claims carry RXAmt = 1

The `amtsupp` residue sat almost entirely in the pegfilgrastim and
filgrastim cohorts (444 of 599 mismatched episodes in peg alone), and
this package was ALWAYS lower. Those cohorts' exposure comes largely
from procedure claims (J-codes). SAS sets `RXAmt=1` and
`NumDispensing=1` for medical claims (ms_cidanum.sas, the
`if b or c or d or g` block); this package used NULL, under a comment
wrongly asserting SAS did the same.

| | before | after |
|---|--:|--:|
| `amtsupp` vs SAS | -2,367 | **+15** in 15.1M |
| episodes with a different amount | 599 | **15** |

Every count stayed exact (rxamt is also an index tie-break). The 15
remaining largely coincide with the 11 episodes whose SUPPLY differs.


## Supply: exact

The 11 episodes whose supply (`TotRxSup`) differed had two causes:

* **Counting stops at the first event.** SAS counts supply to where
  follow-up ends, and an outcome event ends it. Warfarin patient
  82014737: SAS 10 + 19 days, clipped at the event on 2024-05-13; this
  package counted 10 + 30 to the episode end.
* **Duplicate same-day procedure claims count twice.** Filgrastim patient
  60364873 has each injection recorded twice; SAS sums every row (6),
  while stockpiling collapses same-day procedure claims to one (3). The
  supply count now takes procedure and diagnosis claims from the raw
  rows — they are not chained, so their dates are unchanged — while
  episode construction keeps the collapsed set that made episode ends
  exact.

| | before | after |
|---|--:|--:|
| episodes with different supply | 11 | **0** |
| `daysupp` vs SAS | +21 | **+0** in 6.4M |

## Amount: exact

All 12 remaining episodes were one patient, 153716000: two dispensings
on the index date (21 days / 42 units and 60 days / 60 units), 50 days
of follow-up. This package prorated once: 102 x 50/81 = 62.963. SAS:
92 x 50/71 = 64.7887.

The first reading — SAS clips each fill to the WINDOW before combining
same-day fills — was implemented and was WRONG: it fixed this patient
and broke two others. Patient 164261173 also has two same-day fills
running past the window (90 days / 180 and 28 days / 74), and SAS
prorates them once: 254 x 64/118 = 137.7627.

The difference is enrolment. Patient 153716000's enrolment ends on
2023-10-31, cutting the 60-day fill to 50 days / 50 units; patient
164261173's enrolment outlasts both fills. SAS shaves each fill to
ENROLMENT before combining same-day fills — as it shaves claims before
stockpiling everywhere else — then prorates the combination over its
overlap with the window.

| | SAS | this package |
|---|--:|--:|
| `daysupp` | 6,384,382 | **exact** |
| `amtsupp` | 15,109,779 | **exact** |
| episodes with different supply | | **0** |
| episodes with different amount | | **0** |

`test_supply_and_amount_replay_wp307_patients` replays the four patients
behind the supply and amount rules and was mutation-checked against
each: prorating once, the rejected window clip, and removing the event
clip each fail it on exactly the patient that rule concerns.


## Utilization: exact

Utilization had never been compared with SAS. Every count was wrong:
medical visits ~3x SAS, drug counts all zero. Four causes:

* **The utilization file's wide format was not read.** SAS's UTILFILE has
  one row per group with medutilfrom/to AND drugutilfrom/to. Only the
  long shape (utiltype, utilfrom, utilto) was parsed, so each row became
  a MEDICAL window with the default -365..-1, and no DRUG window existed.
* **The drug class file was dropped entirely.** It keys on `rx`; the
  parser required `code` and silently discarded all 301,245 rows. SAS
  counts NumGeneric as distinct GENERIC names, via an inner join on NDC
  (ms_computeutilization.sas:396-421); this counted distinct NDCs.
* **createbaseline again.** SAS computes utilization only for cohorts
  with createbaseline = 'Y'; every total was exactly double.
* **Visits come from the encounter table.** Counting from diagnosis
  claims missed encounters with no diagnosis (NumAV -56, NumOA -559).
  The encounter table is now an optional input; without one, visits fall
  back to diagnosis claims with a warning. It is not fingerprinted when
  missing by name (that costs 0.8-1.6s per run); map it with table_map.

| per episode, all 31,464 | differing before | differing now |
|---|--:|--:|
| NumAV | 28,917 | **0** |
| NumOA | 20,048 | **0** |
| NumIP | 4,366 | **0** |
| NumED | 7,133 | **0** |
| numrx / NumGeneric / NumClass | 15,708 | **0** |

Distinct encounter days and every encounter row give the same counts on
this extract, so the data cannot say which SAS uses; distinct days are
kept.


## MFU: matches SAS wherever the result is determined

wp307 produced no MFU table at all. Three silent failures, all in how
SAS-format MFU rows were read:

* a row naming no group applies to every cohort; such rows were dropped
  (wp307's only MFU row has no group);
* SAS spells countmethod 'P' / 'C'; only 'PATCOUNT' / 'CODECOUNT' were
  recognised, so 'P' fell back to ranking by claims;
* codetype restricts the code system (ICD-10 here) and was ignored.

Against SAS's `r01_mfu`:

| | SAS | this package |
|---|--:|--:|
| rows / cohorts | 400 / 40 | **400 / 40** |
| same code, different counts | | **0** |
| rank positions with a different patient count | | **0 of 400** |
| codes at a different rank | | 114 |
| SAS codes missing (a tie at the top-10 cutoff) | | 24 |

The last two rows are entirely ties: every rank position holds the same
patient count in both, and wherever the codes differ they have the same
patient count. SAS's order among tied codes fits no rule tried — fewer
claims first and more claims first each contradict it somewhere, and
code order does too. SAS appears to sort on patient count alone and keep
tied rows in whatever order its grouping produced, which cannot be
reproduced outside SAS. This package keeps a deterministic tie-break
(more claims, then code), so at the cutoff it can choose a different code
from a tie than SAS did.


## Risk scores: exact

The CCI this package reported disagreed with SAS on every episode — and
was not CCI at all. Three silent failures:

* **The risk score file was never read.** Only the codes file was, so
  each score's window fell back to -365..-1 instead of the file's
  -183..0.
* **Every score in the shared code library was computed** — seven in
  wp307's — though the study asks only for CCI.
* **A score without an intercept row vanished.** Intercepts are
  cross-joined into the result, and only scores WITH an intercept were
  listed. Of wp307's seven library scores only FRAILTY has one, so the
  value reported as CCI was FRAILTY: fractional (0.04-0.14) where SAS's
  CCI is an integer from -2 upward.

| | SAS | this package |
|---|--:|--:|
| CCI, sum over 31,464 episodes | 39,316 | **39,316** |
| episodes with a different CCI | | **0** |

Unlike covariates and utilization, risk scores are computed for every
cohort in SAS, not only createbaseline ones.

The master list still names the column `RiskScore`; SAS names it after
the score (`CCI`).


## Master list: 88 of SAS's 92 columns

Added, each matching SAS on all 31,464 wp307 episodes — including where
SAS leaves a value NULL:

| column | definition |
|---|---|
| `ExactNumVisit` | distinct visit DAYS in the window, across encounter types |
| `NumVisits` | SAS's category of it: `0`, `1`, `2-7`, `8+` (it held the numeric total) |
| `CCI` | each requested risk score as a column named after it, every cohort |
| `fupdays_value_cat` | `followuptime` bucketed `0-90` / `91-180` / `181+` |
| `Censorcat_sort` | that bucket's position, 1-3 |

Utilization columns are now NULL, not zero, for cohorts SAS computes none
for (createbaseline = 'N'), with `NumVisits` blank — as in SAS.

Still missing: `death_source` and `death_enctype` (blank on every wp307
row, so there is nothing to verify a definition against) and
`distindexexp` / `distindexhoi` (they index into SAS's index-code
distribution map).


## Index-code distribution: distindexexp / distindexhoi

Implemented from ms_codedistribution.sas. Per cohort and type, every
index entity is numbered by row position: drug stockgroups sorted, then
medical codes sorted by (codecat, codetype, enctype, pdx, code) and
expanded in place — care setting '**' into IP IS ED AV OA, a DX flag '*'
into P S X '', a PX into X ''. An episode's value is the ids of its
claims on the index date (exposure) or first event date (outcome),
joined with '_' in character order.

| | episodes differing (of 31,464) |
|---|--:|
| `distindexhoi` | **0** |
| `distindexexp` | 10 |

All 10 are one mechanism, three patients. Patient 423995's J2506 claim is
recorded at an AMBULATORY visit on the index date, 2022-12-24, but the
patient has an INPATIENT stay from 12-20 to 12-24. SAS reports the IP /
flag-X entity; this package the AV / blank one. SAS reassigns claims that
fall inside an inpatient stay to that stay — its "envelope" processing
(RUN_ENVELOPE) — and this package does not.

**Enveloping is the next step, and it is broader than this column:** it
changes the care setting of every claim inside an inpatient stay, which
matters wherever a care-setting restriction applies (exposure, events,
covariates). wp307 sets none elsewhere, so this column is the only place
it shows here. Not covered either: pregnancy (PO) and cause-of-death (CD)
entities, and SAS's skip for cohorts defined by labs, dates or death.

Master list: 90 of SAS's 92 columns. Still absent: `death_source`,
`death_enctype` (blank on every wp307 row).


## Enveloping: every claim, as SAS

Enveloping (ms_envelope.sas) now happens in the cdm_diagnosis and
cdm_procedure views, so every stage sees it, as SAS envelopes its whole
claim extraction (combo.sas and ms_cidanum.sas's claim set). A non-IP
claim dated within an inpatient stay becomes care setting IP, flag X;
the procedure view carries a flag column for this (blank unless
enveloped). Stays come from the encounter table; RUN_ENVELOPE is read
from the study (0: admit day included; 2: off; otherwise from the day
after admit). A study without an encounter table, or without `ddate`,
runs with one-day stays or none, rather than failing.

**Correction to an earlier entry.** Enveloping was first limited to the
index-code distribution because enveloping every claim appeared to break
wp307 cohorts (patients +28, episodes +48). It did not: that run already
carried an unrelated regression — dispensings filtered by fill date
rather than supply — which alone produced exactly those numbers. With
the regression fixed, enveloping every claim leaves every wp307
comparison exact.

## A regression, and how it got through

The risk-score fix (reading the risk score file) narrowed the claim
read window: the widest look-back fell from an accidental 365 days —
the wrong risk-score default — to the correct 183. Exposure dispensings
were filtered by FILL date, so long fills dated before the window were
dropped, though their supply reached it: patients +28, episodes +48.
Fixed by filtering on supply. It shipped in two packages because each
change was verified only against the SAS output it targeted, and the
suite runs on demo data with no SAS comparison. Every commit is now
followed by the full wp307 comparison, not just the feature changed.

## wp307: every comparison exact

| | |
|---|---|
| patients, episodes, all 31,464 ends, outcomes | exact |
| denominator members and member-days | exact |
| supply (`daysupp`), amount (`amtsupp`) | exact |
| 32 covariates per episode; baseline covariate counts | exact |
| utilization: NumAV, NumOA, NumIP, NumED, numrx, NumGeneric, NumClass | exact |
| CCI | exact |
| distindexexp, distindexhoi | exact |
| ExactNumVisit, NumVisits, fupdays_value_cat, Censorcat_sort | exact |
| MFU | per-code counts exact; patient count matches at all 400 ranks; ties ordered differently (SAS's tie order is not reproducible) |

Master list: 90 of SAS's 92 columns; `death_source` and
`death_enctype` are blank on every wp307 row, so there is nothing to
verify a definition against.
