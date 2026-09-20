# Outputs


---

## msoc naming is a contract, not a convention

`msoc/` goes to the Operations Center, where downstream tooling matches
on **dataset name**. A plausible-looking rename is a breakage, not a
cosmetic difference. Every msoc output is therefore either named exactly
as SAS names it, or carries a `note` in `manifest.json` saying why not.

### `attrition` — column names and units corrected

SAS emits `group level descr claim_level remaining excluded`
(`ms_attrition_cidacompute.sas:104-115`). This was
`cohortgrp step_no step records patients ...` — the right information
under names no downstream reader would match on.

**`claim_level` matters more than it looks.** It declares which unit
`remaining`/`excluded` are counted in, and SAS never subtracts across a
change of unit because each step computes its own counts inside the
macro. Steps 1-3 here narrow **claims**; step 4 onward narrow
**episodes**.

The first version of this fix lagged straight through the boundary and
reported **66,921 excluded against 53,107 remaining** — a
plausible-looking number that meant nothing, because it differenced a
claim count against an episode count. `excluded` is now NULL at the unit
change. A missing value is honest; a wrong one is not.

Both units are still carried, after the contract columns, since they
were already computed:

```
group level descr claim_level remaining excluded records patients ...
```

### `distindexmap` — five columns were missing

SAS keeps `group distindextype stockgroup codecat codetype enctype pdx
code distindexid` (`ms_codedistribution.sas:433-435`). This emitted four
of the nine.

The missing ones are not decoration: **`stockgroup` and `codecat`
identify which code list a code came from**, so a code appearing in two
of them was indistinguishable. `enctype`/`pdx` are the care setting and
are legitimately NULL for `distindextype='EXP'` — care settings attach
to EVENT codes, not exposure codes, which was confirmed rather than
assumed. `codetype` is NULL because the CDM carries it on the claim, not
on the code definition; emitted rather than guessed.

`distindex` itself matched, plus an extra `npts`.

### `denomcounts` — the key metrics were misnamed

SAS calls them **`DenNumPts`** and **`DenNumMemDays`**, in both
`denomcounts` and `t2_cida` (nine uses each in `ms_cidatables.sas`).
This had `eligible_members` and `memberdays` — descriptive, but not what
a downstream merge references by name. Also `year`, not `index_year`.

SAS's shell additionally carries geography (`zip_uncertain zip3 state
hhs_reg cb_reg`) and finer time (`month quarter`) as stratification
dimensions. This package does not offer those as CIDA strata, so they
are emitted NULL — the same convention already used for a level that
does not stratify on agegroup or sex, and it keeps the column set
matching.

Verified after the rename: `denomcounts` and `t2_cida` reconcile at
154,300 `dennumpts`.

## Which tables stratify, and which correctly do not

Checked exhaustively after the same defect turned up in three tables:

| output | stratifies by levelvars? | verified |
|---|---|---|
| `t2_cida` | yes, incl. `&covarstrat.` | fixed |
| `censoring` | yes | fixed |
| `followuptime` | yes | fixed |
| `denomcounts` | yes | already correct |
| `numcounts` | yes | already correct |
| `baseline` | **no** — SAS classes on `&groupvar.` only | correct |
| `utilization`, `labs`, `mfu`, `riskscores`, `codedistribution` | **no** | correct |

The last group is not an omission: none of those SAS macros references
`levelvars` either. They feed the baseline and CIDA tables rather than
producing stratified output of their own.

### Reconciliation holds at every level, not just the first

`t2_cida` is a merge of `numcounts` and `denomcounts` BY NAME, and that
had only ever been checked at the unstratified level. Verified across
three levels on the fixture and all four on the production study:

```
level 000: t2_cida 564,468  denomcounts 564,468
level 001: t2_cida 597,826  denomcounts 597,826
level 002: t2_cida 564,468  denomcounts 564,468
level 003: t2_cida 597,826  denomcounts 597,826
```

**Note what correct looks like.** A year-stratified level reports MORE
patients than the unstratified one — 597,826 against 564,468 — because
a patient enrolled across two years counts in both. That is a stratified
count, not double counting, and a test asserting equality across levels
would have been wrong.

## The same defect was in three tables, not one

USERSTRATA levelvars stratify **every** table that takes them.
`censorstrat` is the levelvars (`ms_cidanum.sas:123`), so race, hispanic
and year stratify `censoring` and `followuptime` exactly as agegroup and
sex do.

Both honoured **only agegroup and sex**. A level asking for `year`
received the unstratified totals labelled as that level, and every level
came back with an identical row count:

```
rows per level:  1: 3081   2 (year): 3081   3 (race): 3081
```

After the fix:

```
rows per level:  1: 3081   2 (year): 9439   3 (race): 12725
episodes:        1: 64663  2: 64663         3: 64663
```

Each level stratifies, and each remains a complete partition.

**This affects the production study**, which stratifies on `year`
(levels `001` and `003`). Its censoring table now varies by level —
937 / 1,037 / 1,003 / 1,057 rows — where all four were previously the
same numbers under different labels.

The test is parameterised across `censoring`, `followuptime` and
`t2_cida`, because the defect was found in one, fixed there, and left
in the other two.

## `t2_cida` is built from USERSTRATA, and the dynamic part was missing

SAS's retain list for `t2_cida` ends with **`&covarstrat.`** — the
USERSTRATA levelvars beginning with `covar`
(`ms_cidatables.sas:403-412, 425`). A study can stratify the CIDA table
by a **covariate**, which adds a column per covariate.

This package parsed `covar1` into levelvars and then ignored it, so a
study asking to stratify by covariate 1 received the **unstratified
totals labelled as that level**. Silently wrong, which is worse than a
missing column.

Implemented and verified across four levels:

| level | levelvars | episodes | rows w/ covar1 | rows w/ covar12 |
|---|---|--:|--:|--:|
| 1 | *(none)* | 64,663 | 0 | 0 |
| 2 | `covar1` | 64,663 | 4 | 0 |
| 3 | `covar12` | 64,663 | 0 | 4 |
| 4 | `sex covar1` | 64,663 | 8 | 0 |

Every level is a complete partition, and **`covar1` does not match
`covar12`**: the levelvars list is compared as a space-padded string,
because a bare `LIKE '%covar1%'` would collide.

The standard strata (sex, agegroup, year, geography) are a FIXED list in
SAS's retain — emitted always, NULL where a level does not use them —
which is the existing behaviour.

## `baseline` was missing a dummy group and five continuous columns

SAS sums `patient, Age:, Sex_:, year_:, race_:, hispanic_:, covar1..N`
and takes mean/std over Age, the risk scores, and the utilization counts
(`ms_createdistbaselinetable.sas:457-500`).

**`year_` was missing entirely** — an index-year distribution the study
asks for and did not get.

The continuous columns exposed a second bug. The utilization stage names
its encounter counts `enc_av` / `enc_oa` / ...; SAS calls them `NumAV` /
`NumOA` / .... The first version listed only the SAS names, and the
"skip if the column is absent" branch **silently dropped five of the
eight**. There is now an explicit source-to-SAS mapping, and a missing
column warns rather than being skipped quietly.

Geography (`cb_reg_`, `sdi_`) and the continuous means are gated on
their optional stage, exactly as SAS gates them on `&geog = Y`.

### A population difference that looks like a bug

`utilization` covers every master-list episode (71,350 on the fixture);
`baseline` averages over the episodes that survive the follow-up washout
(64,663). The two means differ — 0.3355 against 0.3179 for `NumIP` — and
both look plausible.

SAS builds `_RawData` from the master list, so the baseline figure is
the right one. Restricting the utilization mean to `cohort_final`
reproduces the baseline value exactly, and a test pins that rather than
the raw average.

## Extra columns are a defect too

A data partner noticed `attrition` carrying more columns than the query
requires. It was — four of them.

The reasoning behind them was wrong: the values (`records`, `patients`,
`records_dropped`, `patients_dropped`) were already computed, so they
were appended after the contract columns rather than discarded. **For an
msoc output the column SET is the contract, not just the column names.**
A table with four unexpected columns is one a downstream reader has to
be taught to ignore, and it is one more thing to explain at a disclosure
review.

SAS's attrition data step writes `level, claim_level, descr, remaining,
excluded` and `group`, and stops
(`ms_attrition_cidacompute.sas:125-134`).

Swept across every contract output:

| output | was | now |
|---|--:|--:|
| `attrition` | 10 columns | **6** |
| `distindex` | 5 | **4** |
| `t2_cida` | `index_year`, 8 strata missing | **27, exact** |
| `censoring` | 10 | 10 — correct already |
| `distindexmap` | 9 | 9 — correct already |

`t2_cida` had the same `year` / `index_year` mismatch fixed in
`denomcounts` earlier and never carried across — so the two tables named
the same column differently, and **the merge between them is by name**.
It was also missing `month`, `quarter` and the five geography columns
from SAS's retain list; those are now emitted NULL, the same convention
already used for a level that does not stratify on agegroup or sex.

`censoring` keeping `agegroup` and `sex` is correct: the SAS keep is
`group level <censorstrat> episodes <msocflaglist>`, and `censorstrat`
is the USERSTRATA levelvars (`ms_cidanum.sas:123`).

`test_output_column_names_match_the_sas_contract` now asserts the column
set is EXACT, not merely a superset. The earlier version checked only
that the contract columns were present and in order, which is why four
extras sat there unnoticed.

## The sweep, and what it found

Every output was checked against the SAS macro library. **All of them
were wrong**, each differently:

| output | what was wrong |
|---|---|
| `mstr` | **wrong table** — 6,687 extra episodes, no event columns |
| `censor_cida` | name *and* shape |
| `attrition` | column names *and* a cross-unit subtraction |
| `distindexmap` | 5 of 9 columns missing |
| `denomcounts` | the merge metrics misnamed |
| `geography` | not a SAS dataset; belongs on `mstr` |
| `numcounts` | a real SAS output, computed and then discarded |
| `mstr_final`, `inclexcl`, `risk_scores` | names that do not exist in SAS |

The root cause was consistent: outputs were built from what the pipeline
naturally produced, then labelled with SAS names that seemed to fit.
That reads as verification and is not.

`test_every_sas_contract_output_really_exists_in_sas` now checks the
contract list against dataset names confirmed present in the macros. It
caught two more entries while being written — `inclexcl` and
`risk_scores` — that had survived the manual pass.

### `mstr` was also missing seven SAS column names

Having fixed WHICH table `mstr` is, the columns inside it were still
wrong. Downstream SAS steps read these off mstr by name
(`ms_finalizeptsmasterlist.sas`), and a step selecting any of them would
have found nothing:

| SAS | was |
|---|---|
| `group` | `cohortgrp` |
| `FEventDt` | `eventdt` |
| `Event` / `Event_flag` | `has_event` |
| `episodelength` | `episode_days` |
| `followuptime` | **absent** — computed only inside the CIDA stage |
| `timetocensor` | **absent** — computed only as a CTE |
| `EpisodeEndDt_Censor` | **absent** |

**Added rather than renamed.** The internal names are used across every
other stage, and renaming would be churn for no gain. The contract is
that the SAS names exist and are correct, not that they are the only
ones — the same choice made for `attrition`.

`followuptime` is now computed in two places: onto mstr, and in
`94_followuptime.sql`. A test asserts the two agree (3,378,353 on the
fixture). Two independent expressions of one SAS formula either agree or
one of them is wrong.

### `numcounts` — a SAS output that was being thrown away

`DPLocal.&RUNID._numcounts` is the numerator detail behind `t2_cida`,
written alongside the msoc table (`ms_cidatables.sas:418`). This package
computed it as `_t2_num` and then dropped it, so a DP had the CIDA
numerators only in merged form, with no way to check them against the
denominators separately. Now emitted.

### `baseline` was a different table entirely

The last deliverable in the sweep that was structurally wrong rather
than misnamed.

SAS's baseline is **WIDE and SQUARED**
(`ms_createdistbaselinetable.sas:455-537`): one row per cohort group,
built from two `proc means ... class &groupvar` passes merged by group.

* **`_Discrete`** — `sum=` over 0/1 dummies, one column per category
  LEVEL: `Sex_F`, `Sex_M`, `Race_*`, `Hispanic_*`, `Age<bucket>`,
  `covar1..covarN`. A sum of dummies is a count.
* **`_Continuous`** — `mean=` and `std=` over age and the utilization
  counts, with `_freq_` renamed `N_episodes`.
* **Squared** — a level absent from a group becomes `0`, not a missing
  column. That is what makes the table stackable across groups and
  comparable across runs: a missing column and a zero column mean
  different things to a reader.

This package emitted `covariate_prevalence` under the name — **one row
per covariate, long**. A different table, not a renaming, so the name
has been returned to `covariate_prevalence` and the real baseline
implemented.

The category columns are generated from the levels OBSERVED in the
data rather than a hardcoded list, so a race code or age band the study
did not anticipate still gets a column. Level values are sanitised to
`[A-Za-z0-9]` before becoming identifiers — they come from claims data
and are never interpolated raw.

Verified: one row per group, dummies sum to `n_episodes`, `n_episodes`
totals to `cohort_final`, zero NULL cells, and a cohort restricted to
females still carries `Sex_M = 0`.

**A near-miss worth recording.** The first read of this macro landed on
the `%else` branch labelled *"No patients in query - create empty
baseline table"* and nearly took its three-column shell for the real
structure. Reading a fragment and generalising is how most of the errors
in this document were made.

### `mstr` was the WRONG TABLE

The most consequential finding of the sweep.

SAS has **one** master list. `DPLocal.&RUNID._mstr` is set from
`_PtsMasterList` *after* `ms_finalizeptsmasterlist` attaches the event
and censoring columns (`ms_createmicohorts.sas:2117`), so SAS's `mstr`
is the **finalised** list. There is no `&RUNID._mstr_final` anywhere in
the macro library — that name was invented here.

This package splits the list in two, which SAS does not, and mapped the
**pre-follow-up intermediate** to `mstr`. A DP reading `<runid>_mstr`
therefore got:

* **6,687 extra episodes** that had never been through the follow-up
  washout, and
* **no `eventdt`, `has_event` or `numevents` columns at all**

Corrected: `cohort_final` → `<runid>_mstr`, and the intermediate is
`<runid>_mstr_episodes`, flagged as an addition. Verified on the file
itself, not just the manifest: 64,663 rows carrying the event columns.

### `geography` — SAS has no such dataset

Checked across the whole macro library, not assumed: there is **no
`&RUNID._geography`**. SAS carries `zip3 state hhs_reg cb_reg
zip_uncertain` as **columns on `mstr`**
(`ms_geographicvars.sas:158`).

Emitting them only as a separate table meant `mstr` was missing columns
a downstream step reads from it by name. They are now merged onto
`ptsmasterlist`; the standalone table is kept as a convenience and is
flagged `"sas_contract": false` with a note.

Verified: 71,350 episodes in, 71,350 out, zero join failures.

### Named exactly as SAS

| file | SAS source |
|---|---|
| `<runid>_attrition` | `msoc.&RUNID._attrition` |
| `<runid>_censor_cida` | `msoc.&RUNID._censor_cida` |
| `<runid>_distindex` | `msoc.&RUNID._distindex` |
| `<runid>_distindexmap` | `msoc.&RUNID._distindexmap` |
| `<runid>_runtimes` | `msoc.&RUNID._runtimes` |
| `<runid>_signature` | `msoc.&RUNID._signature` |
| `<runid>_t2_cida` | `msoc.&RUNID._&table._cida` |

**`censor_cida` was wrong twice over and both are fixed.**

The *name* was `<runid>_censoring`; SAS calls it `censor_cida`
(`ms_createcensortable.sas:22, 200`).

The *shape* was a per-exit_reason summary with person_days and
percentages — readable, but not the dataset the Operations Center
expects. SAS emits `group level <censorstrat> episodes <msocflaglist>`
(lines 246-250), i.e. `censdays_value_cat` plus counts of
`cens_elig / cens_dth / cens_qryend / cens_dpend`. **For an msoc output
the shape is part of the contract, not a presentation choice.**

Two details worth knowing when reading it:

* **The flags are not mutually exclusive.** They indicate which dates
  EQUAL the censor date, so an episode disenrolling on the query end
  date sets both `cens_elig` and `cens_qryend`. On the 100k fixture the
  flags sum to 64,705 against 64,663 episodes — the 42-episode excess is
  exactly the coincident-date episodes, not a double count. A test
  asserting the flags sum to the episode count would be wrong.
* **USERSTRATA is optional.** SAS still emits the table unstratified at
  level 1. Without a fallback the level CROSS JOIN produces nothing, so
  a study defining no strata got an msoc output that was silently
  absent rather than unstratified.

### Named differently, and why

| file | SAS name | reason |
|---|---|---|
| `<runid>_baseline` | `<runid>_baseline<outcohort>_<i>` | `_<i>` is a surveillance-period index. This package does not model surveillance periods, so there is no value to supply. |
| `<runid>_mfu` | `<runid>_baseline_<mfu>_<i>` | same period-index caveat |

### Additions — not SAS outputs at all

`lab_summary`, `risk_score_summary`, `utilization_summary`,
`denomstrata` and `geography` are additions this package makes. They are aggregate, so
`msoc` is the correct disclosure library for them, but they are **not**
part of the QRP output contract and are flagged `"sas_contract": false`
in the manifest.

`denomstrata` deserves a note: SAS has exactly one `<runid>_denomcounts`
and it is `ms_cidadenom`'s, emitted here under that name. The
per-stratum aggregate from `70_outputs.sql` is a separate thing and was
previously mapped to the same name, so one output silently overwrote the
other.

### Reading the manifest

```json
"censoring": {
  "library": "msoc",
  "file": "r01_censor_cida",
  "sas_contract": true
}
```

`sas_contract: false` always comes with a `note`. A DP preparing a
submission can filter on that field rather than diffing filenames
against the SAS package by hand.

### `followuptime_cida` — IMPLEMENTED

`<runid>_followuptime_cida` is **opt-in**, requested via USERSTRATA
`tableid='t2followuptime'`.
USERSTRATA dispatches on `tableid` (`ms_cidanum.sas:2820-2831`), and it
is produced only when a study asks for `t2followuptime`. It is the same
macro as `censor_cida` with different parameters:

| | censor | followuptime |
|---|---|---|
| `convar` | `timetocensor` | `followuptime` |
| `catvar` | `censdays_value_cat` | `fupdays_value_cat` |
| flags | `cens_elig cens_dth cens_dpend cens_qryend` | adds `fup_episend fup_spec fup_event` |

Neither the demo studies nor the production input file requests it, so
nothing has been losing it.

**What was wrong is that a study asking for it got silence.**
`cida_levels()` filtered to `t2cida` and dropped every other tableid
with no error — so the table would be ABSENT rather than empty, and a
downstream step expecting it would find nothing. That now warns at load,
through both `load_study` and `load_study_dict` (the dict path was
previously silent even for the inclusion warnings beside it).

**The docstring is misleading about the shape.** It says "for every day
of follow-up", which reads as a per-day row expansion. It is not: rows
are grouped `by group level <censorstrat>` exactly as `censor_cida` is
(`ms_createcensortable.sas:240-246`), and `fupdays_value_cat` buckets
the follow-up DURATION. Reading the macro rather than the docstring
saved building the wrong thing.

The censor date is the real difference between the two tables:

| | censor date |
|---|---|
| `censor_cida` | `min(Enr_End, DeathDt, QueryEnd, DPEnd)` — ignores the event |
| `followuptime_cida` | `min(EpisodeEndDt, Enr_End, FEventDt)` — **the event counts** |

so `followuptime = max(0, censor_dt - IndexDt - BLACKOUTPER -
ATRISKSTART + 1)` (`ms_finalizeptsmasterlist.sas:300, 312`).

One naming quirk reproduced deliberately: dplocal carries `fup_*` flags
and msoc carries `cens_*` — the rename is in the macro call
(`ms_cidanum.sas:2829-2830`), not in the data. So the msoc columns are
identical to `censor_cida`'s and only their meaning differs.

`fup_spec` (requester-defined truncation via `trunkdt`) is structurally
0: that is mock-surveillance `IndexLookEndDt`, which this package does
not model. The column is emitted rather than omitted so the output
shape matches.

Verified: both levels reconcile against `cohort_final` (65,052
episodes), and **every episode carries at least one censoring reason** —
the flags are exhaustive, not a sample.


---

## Rerun safety

Two hazards, both verified as real before fixing.

### Stale outputs from a previous run

Rerunning into a populated directory left files the current run did not
produce, with nothing marking them stale:

* **A feature removed between runs.** Drop the covariates and
  `covariates`, `baseline` and `covariate_prevalence` survived from the
  previous run. A reader got covariate results for a study that defines
  no covariates.
* **Switching `--names`** between `sas` and `logical` wrote the same
  table under BOTH names — `censor_cida` and `censoring` side by side,
  with nothing indicating they are one table.

Fixed by clearing this run's previous outputs before writing.

### Scoped to the run id, not the directory

The clear matches `<runid>_*` only. A data partner may legitimately keep
several runs' outputs together, and wiping a sibling run's results would
be worse than the staleness being fixed. A test asserts `studyB` leaves
all eight of `studyA`'s outputs intact.

### What a failure does

| failure point | previous outputs |
|---|---|
| during the pipeline | **intact** — the clear lives inside the output-writing block, which only runs after the pipeline succeeds |
| during the write loop | lost, and the new set is partial |

The second case is a real window and is not closed. Writing to a temp
tree and swapping would close it, but doubles peak disk — and disk is
already the binding constraint at scale, since the pipeline spills
roughly 2.6x the input size at its memory floor. The cure would risk
causing the disease. Re-running regenerates the outputs; a half-full
disk does not.

**`manifest.json` is written LAST**, so its presence is the signal that
a run completed and its outputs are the full set. A tree with tables but
no manifest is a partial write.


---

## When denominators are NOT computed

`denomcounts` needs a USERSTRATA file. That is not a quirk of this
implementation — SAS gates on it explicitly:

> Only compute denominators if OUTPUTDENOM ne N and USERSTRATA file is
> specified and contains relevant tables — `ms_cidadenom.sas:113`

So a study with no USERSTRATA correctly gets no denominators.

Checking that turned up **a second gate this package was ignoring
entirely**.

### `OUTPUTDENOM`

A per-cohort field on the type2 file, and it was not parsed at all:

| value | meaning |
|---|---|
| `Y` | members and member-days |
| `M` | members only — `DenNumMemDays` is blanked (`ms_cidadenom.sas:1347`) |
| `N` | no denominator for that cohort |

A cohort setting `N` was getting a denominator anyway — a plausible
number with no SAS counterpart.

### `minrxdays` forces it off

**`minrxdays > 1` in ANY inclusion rule disables the denominator for
Types 1-2**, with a warning:

> Outputdenom set to N for &itgroup. because minrxdays is used in
> inclusion/exclusion criteria — `ms_setnumloopmacrovars.sas:898-900`

A pro-rated supply requirement makes the eligible-member count
incoherent, so SAS refuses to emit one rather than emit a wrong one.
This package emitted one regardless.

Both gates are now applied, and both warn at load rather than silently
dropping a deliverable.

**The production study is unaffected**: all 14 cohorts are
`OUTPUTDENOM=Y` and no inclusion rule uses `minrxdays`, so its output is
unchanged at 238 rows. The divergence would only have appeared on a
different study — which is exactly the kind that gets noticed late.
