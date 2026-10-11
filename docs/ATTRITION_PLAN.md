# Attrition: matching SAS's table

Status: **implemented; every level matches SAS on wp307** (all 27 levels,
all 40 cohorts, zero differences in the parity tool). The table is built
in `70_outputs.sql` from the same flags the cohort is built from. The
original plan and its findings follow; the account of levels 7 and 12 at
the end supersedes the gaps recorded along the way.

## How SAS builds it

`ms_attrition.sas` runs the funnel. Each step calls
`ms_attrition_cidacompute`, which counts what remains in a working
dataset after excluding one flag, records it with SAS's wording, and
increments the level. A step that does not apply calls it with
`attritiontype=skip`: a **blank row that still uses up a level number**,
so the levels line up across studies. Member steps count distinct
patients; episode steps count episodes.

## The steps, as wp307 reports them (Type 2)

| level | kind | step (SAS wording, shortened) | SAS flag | skipped when | duckqrp: where the criterion is applied |
|--:|---|---|---|---|---|
| 1 | Member | Initial member count — non-missing birth date/sex | — | never | `10_normalize` / `45_pov1` (demographics) |
| 2 | Member | at least one enrollment span meeting coverage | — | never | `20_enrollment` |
| 3 | Member | age range within the query period | — | never | `45_pov1` (`age_in_unit`) |
| 4 | Member | chart availability | — | never | `20_enrollment` (chart) |
| 5 | Member | demographic (sex, race, hispanic) condition | — | never | `45_pov1` |
| 6 | Member | a claim with cohort-identifying codes | — | never | `30_exposure` |
| 7 | Episode | Initial episode count (among eligible members) | information | never | `40_index` / `50_episodes` |
| 8–9 | — | birth type / infant linkage (Type 4) | — | not Type 4 | — (blank) |
| 10 | Episode | index date within the age range | `AgeGroupMet` | never | `45_pov1` / `50_episodes` |
| 11 | — | prior look period | `priorlookindex` | no prior look | — (blank on wp307) |
| 12 | Episode | index claims during the query period | `MetIncWRTitself` | never | `40_index` / `50_episodes` |
| 13–15 | — | cumulative dose; min / max daily dose | `cumdosemet`, `mincfddmet`, `maxcfddmet` | no dose limits | `55_dose_censor` / `_claim_dose_view` (blank on wp307) |
| 16 | Episode | pre-index enrollment | `MinEnrdays` | never | `50_episodes` (`enr_days`) |
| 17 | — | HOI-defined enrollment (Type 3) | `MinHoiEnrCrit` | not Type 3 | — (blank) |
| 18 | Episode | all exclusion and inclusion criteria | `lackincl_hasexcl` | no criteria | `52_inclusion` |
| 18.n | Episode | Information: episodes excluded for condition *n* | — | no criteria | `52_inclusion`, per condition |
| 19 | Episode | HOI incidence | `HOIIncidenceCrit` | not Type 2 | `60_followup` (follow-up washout) |
| 20 | Episode | post-index enrollment and available data | `MinEnrAft` | Type 4 | **not found as a separate step — verify** |
| 21 | Episode | at least minimum days supplied | `MinDaySupMet` | POINT = Y | `50_episodes` |
| 22 | Episode | at least minimum days duration | `MinEpiDurMet` | not Type 2 | `50_episodes` (`min_epis_dur`) |
| 23 | Episode | longer than blackout days | `PostBlackoutper` | not Type 2 | `50_episodes` / `60_followup` |
| 24 | Episode | HOI blackout | `HOIBlackout` | — | `60_followup` |
| 25–26 | — | supply/amount outside ranges (information) | — | no ranges set | — (blank on wp307) |
| 27 | Member | Information: unique members in final cohort | — | never | `cohort_final` |

Not on wp307, but in the source: cohort definition 03
(`MetCohortDef03`), Type 3 HOI windows (`T3AtLeastOneEvent`), and the
monitoring-mode rows (members lost to follow-up / still at risk).

## Approach

1. **An attrition log.** Each stage that applies one of these criteria
   writes its count before and after, keyed by SAS's step, into one
   table. Static SQL, a few rows per stage. No counts are recomputed
   afterwards from other tables, so each count is taken exactly where
   the exclusion happens.
2. **The table is presentation.** One final step assembles SAS's
   table from the log: SAS's level numbers, blank rows for skipped
   steps, the per-condition `.n` rows, and SAS's wording.
3. **Order matters.** SAS applies the steps in the order above. Where
   duckqrp applies two criteria together (for example in one statement
   of `50_episodes`), the stage has to count them in SAS's order, so
   that each step's "excluded" is what SAS would report. This is the
   main risk in the work.
4. **Verification.** The parity tool compares the table with wp307's
   row by row; the funnel must end at the cohorts duckqrp already
   matches (e.g. `antixa_rupture_inc`: 765 episodes, 699 members).

## Open questions

* **Level 20, post-index enrollment and available data.** duckqrp has
  no separate step for it. On wp307 it excludes nothing (776 → 776), so
  parity cannot show whether duckqrp applies it implicitly or not at
  all. It needs to be checked against `MinEnrAft`'s definition.
* **Information rows 25–26.** Blank on wp307 because no supply or
  amount ranges are set; their content for a study that sets ranges is
  read from the source, not verified against SAS output.

## Verified definitions (reproduced from duckqrp's tables, against wp307)

| level | definition, as SAS computes it | duckqrp source | wp307 |
|--:|---|---|---|
| 1 | members with a birth date and sex, and any enrollment overlapping the query period | `demographics`, `cdm_enrollment` | 78,678 = SAS |
| 2 | ...with an enrollment *span* (coverage, gap bridging) overlapping the query period | `enrollment_spans` (per `enr_cfg_id`) | 73,940 = SAS |
| 3–5 | age range, chart, demographics | — | exclude nobody on wp307; definitions still to verify |
| 6 | ...with at least one **stockpiled** exposure record (`_groupindex`), **shaved** to a qualifying span (start clipped to the span start), whose clipped start is in the exposure period (`ms_attrition.sas:211-260`) | `stockpiled` × qualifying spans | **40/40 cohorts = SAS** |

| 7 | distinct (member, start date) among `_groupindex` records in the exposure period. `_groupindex` is the stockpiled records shaved to the member's spans by `ms_shaveoutside` with `KeepPartBf=N`: a record starting BEFORE a span is **deleted**, not clipped; its end is trimmed to the span. Spans are truncated at death (`censor_dth`). | `stockpiled` with start inside a death-truncated qualifying span | **24/40 exact; 16 cohorts +1 to +3** |

Level 7's gap (and level 12's) is CLOSED — see "Levels 7 and 12" below.

Level 6 is a cautionary case: counting raw claims, filtered claims, claims
inside spans or claims whose supply reaches a span gave 8, 24, 32, 16 and
32 of 40 cohorts. The SAS source, read afterwards, settled it at once.
The remaining levels are read from the source FIRST.

Level 20 (post-index enrollment and available data) needs no logic change:
SAS's Type 2 block (`ms_attrition.sas:405-434`) requires data available
after the index date plus `reqdaysaftind` AND after the episode end plus
`reqdaysaftepi`, exactly as `50_episodes.sql` does. It only needs counting.


## Levels 7 and 12: closed

Both gaps came from how SAS prepares the dispensings, in
`ms_cidanum.sas`, before the washout, the episodes or the attrition use
them. SAS shaves them to enrollment TWICE:

1. **Before stockpiling** (line 1408; `ms_shaveoutside`, `KeepPartBf=N`,
   `shaverx=Y`): a fill starting outside the member's enrollment spans is
   dropped; supply past a span's end is trimmed, its amount prorated.
2. **After stockpiling** (lines 1641-1650, "Need to reshave because
   Stockpiling may have pushed claims outside enrollment period"): a
   record pushed to a start outside the spans is dropped; supply running
   past a span's end is trimmed.

duckqrp chained the raw fills and never re-shaved. A fill in an
enrollment gap pushed later fills to dates SAS never has (level 7: 24/40
→ 40/40 with the first shave), and a pushed supply crossing a gap
blocked the washout of a later index date SAS accepts (level 12: 38/40 →
40/40 with the second). Both are applied in `30_exposure.sql`, to the
real stockpiling, not just to attrition: every wp307 output was
unchanged, and on data with more enrollment gaps (the demo study) they
change many intermediate records.

Searching record properties for the cause (dates moved by stockpiling,
span boundaries, death dates, merged fills, …) reproduced the
per-cohort pattern of differences in no case. Reading where SAS builds
`_groupindex` found it at once.

## Still unverified

Steps that exclude nobody on wp307 are implemented but not checked
against SAS output: levels 3-5 (member age range, chart, demographic
conditions — currently carried from level 2), and the steps emitted
blank (8-9 Type 4, 11 prior look, 13-15 dose limits, 17 Type 3, 25-26
supply/amount ranges). A study that uses them is needed.
