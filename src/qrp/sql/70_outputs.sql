-- =====================================================================
-- 70_outputs.sql — %ms_attrition and %ms_cidadenom.
--
-- Both output families are GROUPING SETS problems, which is what SAS
-- was expressing the long way round with %ms_squaredtableshell: build an
-- explicit shell of every stratum combination, then left-join counts
-- onto it so empty cells appear as zero.
--
-- SQL has that built in. GROUPING SETS produces every requested
-- margin in ONE scan; the shell-and-join disappears, and so does the
-- cross-join-then-broadcast machinery the PySpark port needed for it.
-- Empty cells are handled by joining the shell only where the study
-- genuinely requires zero rows to be reported.
-- =====================================================================

-- =====================================================================
-- Attrition, as SAS reports it (ms_attrition.sas): a fixed funnel of
-- member steps (1-6), episode steps (7-24) and information rows. Each
-- count follows SAS's own definition, verified on wp307 (see
-- docs/ATTRITION_PLAN.md). Steps that do not apply keep their level as a
-- blank row, so the levels line up across studies.
--
-- Episode steps 12-24 count the SAME flags the cohort is built from
-- (episode_criteria, inclusion, episode_hoi), applied in SAS's order, so
-- the last level's episodes are exactly cohort_final.
-- =====================================================================

-- Members with a birth date and sex (SAS's level-1 population).
CREATE OR REPLACE TEMP TABLE _att_dem AS
SELECT DISTINCT patid FROM demographics
WHERE birth_date IS NOT NULL AND sex IS NOT NULL;

-- Qualifying enrollment spans overlapping the query period, per
-- ENROLLMENT CONFIGURATION (not per cohort: cohorts sharing one would copy
-- every member's spans, 3.2M rows for wp307's 40 cohorts and 3 of the
-- stage's 4 seconds). The death-truncated end is kept beside the plain
-- one: SAS re-shaves _groupindex against it where the cohort censors at
-- death (ms_attrition.sas:216).
CREATE OR REPLACE TEMP TABLE _att_spans AS
SELECT e.enr_cfg_id, e.patid, e.enr_start, e.enr_end,
       CASE WHEN d.deathdt IS NOT NULL THEN least(e.enr_end, d.deathdt)
            ELSE e.enr_end END AS enr_end_dth
FROM enrollment_spans e
JOIN _att_dem m ON m.patid = e.patid
LEFT JOIN deaths d ON d.patid = e.patid
WHERE e.enr_cfg_id IN (SELECT enr_cfg_id FROM cfg_cohort)
  AND e.enr_start <= DATE '{end_date}' AND e.enr_end >= DATE '{start_date}';

-- SAS's _groupindex at levels 6-10: stockpiled records SHAVED to the
-- spans by ms_shaveoutside with KeepPartBf=N, which DELETES a record
-- starting before a span (it does not clip it), with its start in the
-- exposure period. Distinct (member, start).
CREATE OR REPLACE TEMP TABLE _att_gi AS
-- Two plain range joins, by whether the cohort censors at death: a CASE
-- inside the range condition stops DuckDB using a range join (41s
-- instead of 0.16s on wp307).
WITH x AS (
    SELECT st.cohortgrp, st.patid, st.adate, c.enr_cfg_id, c.censor_death
    FROM stockpiled st JOIN cfg_cohort c ON c.cohortgrp = st.cohortgrp
    WHERE st.adate BETWEEN DATE '{start_date}' AND DATE '{end_date}'
)
SELECT DISTINCT cohortgrp, patid, adate FROM (
    SELECT x.cohortgrp, x.patid, x.adate FROM x
    SEMI JOIN _att_spans s
           ON s.enr_cfg_id = x.enr_cfg_id AND s.patid = x.patid
          AND x.adate BETWEEN s.enr_start AND s.enr_end_dth
    WHERE x.censor_death
    UNION ALL
    SELECT x.cohortgrp, x.patid, x.adate FROM x
    SEMI JOIN _att_spans s
           ON s.enr_cfg_id = x.enr_cfg_id AND s.patid = x.patid
          AND x.adate BETWEEN s.enr_start AND s.enr_end
    WHERE NOT x.censor_death
);

-- The episodes surviving level 18 (SAS's _keep), with every later flag.
CREATE OR REPLACE TEMP TABLE _att_ep AS
SELECT e.cohortgrp, e.patid, e.indexdt, e.min_enr_days_met, e.min_enr_aft_met,
       e.min_day_sup_met, e.min_epi_dur_met, e.post_blackout_met,
       h.hoi_incidence_met, h.hoi_blackout_met,
       NOT EXISTS (SELECT 1 FROM {incl_rel} x
                   WHERE x.cohortgrp = e.cohortgrp AND x.patid = e.patid
                     AND x.indexdt = e.indexdt)                 AS incl_met
FROM episode_criteria e
LEFT JOIN episode_hoi h
  ON h.cohortgrp = e.cohortgrp AND h.patid = e.patid AND h.indexdt = e.indexdt;

-- One count per counted level. A view: it has one reader, so it is
-- inlined rather than materialised (the single-consumer rule).
CREATE OR REPLACE TEMP VIEW _att_counts AS
-- level 1 is the same for every cohort: counted ONCE
SELECT c.cohortgrp, 1.0 AS level, l1.n
FROM cfg_cohort c
CROSS JOIN (SELECT count(*) AS n FROM _att_dem m
            SEMI JOIN cdm_enrollment e ON e.patid = m.patid
             AND e.enr_start <= DATE '{end_date}' AND e.enr_end >= DATE '{start_date}') l1
UNION ALL
-- level 2 once per enrollment configuration; 3-5 copy it (see note)
SELECT c.cohortgrp, t.lv, n.n
FROM cfg_cohort c
JOIN (SELECT enr_cfg_id, count(DISTINCT patid) AS n FROM _att_spans GROUP BY 1) n
  ON n.enr_cfg_id = c.enr_cfg_id
CROSS JOIN (VALUES (2.0), (3.0), (4.0), (5.0)) t(lv)
UNION ALL SELECT cohortgrp, 6.0,  count(DISTINCT patid) FROM _att_gi GROUP BY 1
UNION ALL SELECT cohortgrp, 7.0,  count(*) FROM _att_gi GROUP BY 1
UNION ALL
-- 10: the age on the record's date falls in one of the cohort's bands
SELECT g.cohortgrp, 10.0, count(*)
FROM _att_gi g JOIN demographics d ON d.patid = g.patid
WHERE EXISTS (SELECT 1 FROM cfg_age_strata a WHERE a.cohortgrp = g.cohortgrp
                AND age_in_unit(d.birth_date, g.adate, a.unit) BETWEEN a.lo AND a.hi)
GROUP BY 1
UNION ALL SELECT cohortgrp, 12.0, count(*) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 16.0, count(*) FILTER (WHERE min_enr_days_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 18.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 19.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 20.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met AND min_enr_aft_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 21.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met AND min_enr_aft_met AND min_day_sup_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 22.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met AND min_enr_aft_met AND min_day_sup_met AND min_epi_dur_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 23.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met AND min_enr_aft_met AND min_day_sup_met AND min_epi_dur_met AND post_blackout_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 24.0, count(*) FILTER (WHERE min_enr_days_met AND incl_met AND hoi_incidence_met AND min_enr_aft_met AND min_day_sup_met AND min_epi_dur_met AND post_blackout_met AND hoi_blackout_met) FROM _att_ep GROUP BY 1
UNION ALL SELECT cohortgrp, 27.0, count(DISTINCT patid) FROM cohort_final GROUP BY 1;
-- NOTE levels 3-5 (member age range, chart, demographic conditions) are
-- carried from level 2: on wp307 they exclude nobody, so their member-
-- level definitions are not yet verified against SAS. Likewise the steps
-- emitted blank below (8-9 Type 4, 11 prior look, 13-15 dose limits, 17
-- Type 3, 25-26 supply/amount ranges) are not itemised.

-- SAS's layout. kind: init (excluded missing), excl, blank (carries the
-- previous count, excluded 0), final (excluded missing).
CREATE OR REPLACE TABLE attrition AS
WITH skel(level, kind, claim_level, descr) AS (VALUES
    (1.0,  'init',  'Member',  'Initial Member Count - Members with a non-missing birth date/sex at any enrollment episode overlapping the query period'),
    (2.0,  'excl',  'Member',  'Exclusion - Members must have at least one enrollment span meeting the medical and/or drug coverage requirement during the query period'),
    (3.0,  'excl',  'Member',  'Exclusion - Members must satisfy the age range condition within the query period'),
    (4.0,  'excl',  'Member',  'Exclusion - Members must meet chart availability criterion within the query period'),
    (5.0,  'excl',  'Member',  'Exclusion - Members must satisfy the demographic (sex, race and hispanic) condition'),
    (6.0,  'excl',  'Member',  'Exclusion - Members must have at least one claim with cohort-identifying codes within the query period'),
    (7.0,  'init',  'Episode', 'Initial Episode Count (among eligible members from previous step) - All claims with cohort-identifying codes'),
    (8.0,  'blank', '', ''), (9.0, 'blank', '', ''),
    (10.0, 'excl',  'Episode', 'Exclusion - Cohort episode must have an index date within the age range condition'),
    (11.0, 'blank', '', ''),
    (12.0, 'excl',  'Episode', 'Exclusion - Episode-defining index claims must be during the query period'),
    (13.0, 'blank', '', ''), (14.0, 'blank', '', ''), (15.0, 'blank', '', ''),
    (16.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must satisfy the pre-index enrollment criterion'),
    (17.0, 'blank', '', ''),
    (18.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must satisfy all exclusion and inclusion criteria'),
    (19.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must meet HOI incidence criterion'),
    (20.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must satisfy the post-index enrollment and available data criteria'),
    (21.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must have at least minimum days supplied'),
    (22.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must have at least minimum days duration'),
    (23.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must be longer than blackout days duration'),
    (24.0, 'excl',  'Episode', 'Exclusion - Cohort episodes must meet HOI blackout criterion'),
    (25.0, 'blank', '', ''), (26.0, 'blank', '', ''),
    (27.0, 'final', 'Member',  'Information - Unique number of members in final cohort')
),
grid AS (
    SELECT c.cohortgrp, k.*, n.n
    FROM cfg_cohort c CROSS JOIN skel k
    LEFT JOIN _att_counts n ON n.cohortgrp = c.cohortgrp AND n.level = k.level
),
carried AS (
    SELECT *,
           -- a blank row carries the last count before it
           coalesce(n, last_value(n IGNORE NULLS) OVER w) AS remaining,
           lag(n) OVER (PARTITION BY cohortgrp, (kind IN ('init', 'excl'))
                        ORDER BY level)                        AS prev_n
    FROM grid
    WINDOW w AS (PARTITION BY cohortgrp ORDER BY level
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
main AS (
    SELECT cohortgrp, level, claim_level, descr, remaining,
           CASE kind WHEN 'excl'  THEN prev_n - n
                     WHEN 'blank' THEN 0 END                   AS excluded
    FROM carried
),
-- 18.n: episodes excluded for each condition, by the first one failed
cond_rows AS (
    SELECT DISTINCT cohortgrp, criteria, cond, cond_name FROM cfg_inclusion
),
info18 AS (
    SELECT r.cohortgrp,
           18.0 + row_number() OVER (PARTITION BY r.cohortgrp
                                     ORDER BY r.criteria, r.cond) / 10.0 AS level,
           'Episode' AS claim_level,
           'Information: Episodes excluded for ' || r.cond_name AS descr,
           NULL::BIGINT AS remaining,
           (SELECT count(*) FROM {incl_rel} x
            JOIN _att_ep e ON e.cohortgrp = x.cohortgrp AND e.patid = x.patid
                          AND e.indexdt = x.indexdt AND e.min_enr_days_met
            WHERE x.cohortgrp = r.cohortgrp AND x.failed_criteria = r.criteria
              AND x.first_failed_cond = r.cond)                   AS excluded
    FROM cond_rows r
)
SELECT cohortgrp AS "group", level, claim_level, descr,
       remaining::BIGINT AS remaining, excluded::BIGINT AS excluded
FROM (SELECT * FROM main UNION ALL SELECT * FROM info18)
ORDER BY 1, 2;


-- Stratified denominators and event rates.
-- GROUPING SETS gives the full table plus every requested margin in one
-- pass over cohort_final.
CREATE OR REPLACE TABLE denominators AS
SELECT
    cohortgrp,
    COALESCE(agegroup, 'ALL')            AS agegroup,
    COALESCE(sex, 'ALL')                 AS sex,
    COALESCE(year(indexdt)::VARCHAR, 'ALL') AS index_year,
    count(*)                             AS episodes,
    count(DISTINCT patid)                AS patients,
    sum(person_days)                     AS person_days,
    sum(has_event)                       AS events,
    round(1000.0 * 365.25 * sum(has_event)
          / nullif(sum(person_days), 0), 2) AS rate_per_1000_py
FROM cohort_final
GROUP BY GROUPING SETS (
    (cohortgrp, agegroup, sex, year(indexdt)),
    (cohortgrp, agegroup, sex),
    (cohortgrp, agegroup),
    (cohortgrp, sex),
    (cohortgrp, year(indexdt)),
    (cohortgrp)
)
ORDER BY cohortgrp, agegroup, sex, index_year;

-- Exit-reason distribution: why follow-up ended.
-- msoc.<runid>_censor_cida
--
-- Shape follows ms_createcensortable.sas:246-250:
--     group level <censorstrat> episodes <msocflaglist>
-- with censorstrat carrying `censdays_value_cat` and msocflaglist being
-- cens_elig / cens_dth / cens_qryend / cens_dpend.
--
-- This used to be a per-exit_reason summary with person_days and
-- percentages — readable, but not the dataset the Operations Center
-- expects. It is an msoc output, so the shape is part of the contract,
-- not a presentation choice.
--
-- The censor date here IGNORES the event: min(Enr_End, DeathDt,
-- QueryEnd, DPEnd) (ms_finalizeptsmasterlist.sas:308). That is the one
-- substantive difference from followuptime_cida, which does count the
-- event — see 94_followuptime.sql.
CREATE OR REPLACE TABLE censoring AS
WITH censored AS (
    SELECT
        c.cohortgrp,
        c.agegroup,
        c.sex,
        c.race,
        c.hispanic,
        year(c.indexdt)::VARCHAR AS index_year,
        c.deathdt,
        c.enr_end,
        -- the flags are defined ONCE, in 60_followup.sql
        ef.cens_elig, ef.cens_dth, ef.cens_qryend, ef.cens_dpend,
        least(c.enr_end,
              coalesce(c.deathdt, DATE '9999-12-31'),
              DATE '{end_date}',
              DATE '{censor_date}')                   AS censor_dt,
        -- timetocensor = censor_dt - IndexDt + 1 (line 318)
        date_diff('day', c.indexdt,
                  least(c.enr_end,
                        coalesce(c.deathdt, DATE '9999-12-31'),
                        DATE '{end_date}',
                        DATE '{censor_date}')) + 1    AS timetocensor
    FROM cohort_final c
    JOIN episode_flags ef
      ON ef.cohortgrp = c.cohortgrp AND ef.patid = c.patid AND ef.indexdt = c.indexdt
),
flagged AS (
    SELECT * FROM censored
)
SELECT
    f.cohortgrp              AS "group",
    lv.level_id              AS level,
    CAST(f.timetocensor AS VARCHAR) AS censdays_value_cat,
    -- ALL the USERSTRATA levelvars, not just two. `censorstrat` is
    -- the levelvars (ms_cidanum.sas:123), so race, hispanic and year
    -- stratify this table exactly as agegroup and sex do. Honouring
    -- only agegroup and sex meant a level asking for `year` got the
    -- UNSTRATIFIED totals labelled as that level — every level came
    -- back with identical row counts. The production study seen
    -- stratifies on `year`.
    CASE WHEN lv.has_agegroup THEN f.agegroup END AS agegroup,
    CASE WHEN lv.has_sex      THEN f.sex      END AS sex,
    CASE WHEN lv.has_race     THEN f.race     END AS race,
    CASE WHEN lv.has_hispanic THEN f.hispanic END AS hispanic,
    CASE WHEN lv.has_year     THEN f.index_year END AS "year",
    count(*)                 AS episodes,
    sum(f.cens_elig)         AS cens_elig,
    sum(f.cens_dth)          AS cens_dth,
    sum(f.cens_qryend)       AS cens_qryend,
    sum(f.cens_dpend)        AS cens_dpend
FROM flagged f
-- USERSTRATA is optional; SAS still produces the table, unstratified,
-- at level 1 (`_censorlevels` always has a row). Without this fallback
-- the CROSS JOIN yields NOTHING for a study that defines no strata —
-- an msoc output silently absent rather than unstratified.
CROSS JOIN (
    SELECT level_id, has_agegroup, has_sex, has_race,
           has_hispanic, has_year FROM cfg_strata WHERE tableid = 't2cida'
    UNION ALL
    SELECT '1', FALSE, FALSE, FALSE, FALSE, FALSE
    -- scoped to t2cida, matching the branch above. An unqualified
    -- `NOT EXISTS (SELECT 1 FROM cfg_strata)` was satisfied by a
    -- study that defines only t2followuptime levels, so neither branch
    -- produced a row and censoring exported EMPTY.
    WHERE NOT EXISTS (SELECT 1 FROM cfg_strata WHERE tableid = 't2cida')
) lv
GROUP BY ALL
ORDER BY 1, 2, 3;
