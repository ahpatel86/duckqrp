-- =====================================================================
-- 92_cidadenom.sql — enrolled member-days (ms_cidadenom.sas).
--
-- The BACKGROUND denominator: how much eligible enrolled time existed,
-- whether or not anyone was exposed. Distinct from the exposed
-- person-time in `denominators`, and it is what SAS merges onto the
-- CIDA numerators to produce rates against the enrolled population.
--
-- The window (ms_cidadenom.sas:126-138, 700-720):
--
--   DenomEnrStartDt = Enr_Start + ENRDAYS
--       an index date needs ENRDAYS of prior enrollment, so eligible
--       time cannot begin until that lookback is satisfied.
--
--   AdjustedEnrEndDt = min(Enr_End, censordate)
--                    - max(0, MinEpisDur-1, MinDaySupp-1, BlackoutPer,
--                             ReqDaysAftInd,
--                             ReqDaysAftEpi + max(MinEpisDur-1,
--                                                 MinDaySupp-1,
--                                                 BlackoutPer))
--       symmetrically, eligible time must end early enough that an index
--       date there could still satisfy every forward-looking
--       requirement.
--
--   MemberDays = DenomEnrEndDt - DenomEnrStartDt + 1, kept when > 0.
--
-- Only spans with `Enr_End >= startdate` are considered, and the
-- enrollment spans already carry death censoring where the study asks
-- for it.
--
-- NOT included: the inclusion/exclusion shaving (`excl_incl = Y`) that
-- restricts eligible time by INCLUSIONCODES conditions. See
-- docs/SAS_PARITY.md.
-- =====================================================================

-- Split into three passes rather than one nine-way join. Written as a
-- single statement this tripped `test_no_stage_joins_more_than_six_
-- relations`, which exists because POV1 in exactly this shape set the
-- memory floor: concurrent hash tables mean peak memory is the SUM of
-- the build sides, not the largest.
CREATE OR REPLACE TEMP TABLE _denom_windows AS
WITH windows AS (
    SELECT
        -- Keyed on the denominator CONFIG. Cohorts agreeing on every
        -- parameter this stage reads produce identical denominators;
        -- one pass per config and a fan-out at the end turns 14 cohorts
        -- into 5 passes on the study seen.
        c.denom_cfg_id,
        e.patid,
        e.episode                       AS eligepisode,
        e.enr_start,
        e.enr_end,
        -- start: enrollment start plus the required prior-enrollment
        -- start: enrollment start plus the required prior enrollment,
        -- clipped to the query period
        greatest(e.enr_start + c.enr_days, DATE '{start_date}')
                                        AS denom_start,
        -- end: censor date or enrollment end, pulled back by the
        -- longest forward-looking requirement an index date would need
        -- end: the earlier of enrollment end and the query period end,
        -- pulled back by the longest forward-looking requirement an
        -- index date on the last eligible day would still have to
        -- satisfy.
        --
        -- Both halves were established against real SAS output. The
        -- window used to be clipped to `censor_date` rather than the
        -- query period end, which put member-days 45% high; clipping
        -- without the pullback put MEMBERS 1.4% high. Only the two
        -- together agree: 2,503,074 members against SAS's 2,502,986
        -- (0.0035%) and member-days within 0.02%.
        least(e.enr_end, DATE '{end_date}')
          - greatest(
                0,
                c.min_epis_dur - 1,
                c.min_days_supp - 1,
                c.blackout_per,
                c.req_days_aft_ind,
                c.req_days_aft_epi + greatest(c.min_epis_dur - 1,
                                              c.min_days_supp - 1,
                                              c.blackout_per)
            )                           AS denom_end
    FROM cfg_denom_cohort c
    JOIN enrollment_spans e
      ON e.enr_cfg_id = c.enr_cfg_id
    WHERE e.enr_end >= DATE '{start_date}'
)
SELECT
    w.*,
    span_days(w.denom_start, w.denom_end) AS memberdays
FROM windows w
-- SAS keeps only positive member days: the end can be truncated past
-- the start.
WHERE w.denom_end >= w.denom_start;

-- Demographics for stratification, on the same eligibility rules the
-- numerator uses. Age is taken at the START of the eligible window,
-- which is the earliest date an index could occur in it.
-- Pass 2a: demographic eligibility, same rules the numerator applies.

-- ---------------------------------------------------------------
-- Remove exposed-plus-washout time from the denominator.
--
-- SAS makes a member INELIGIBLE from the day after an exposure claim
-- until its supply expires plus the washout
-- (ms_cidadenom.sas:461-478), then shaves those periods out of the
-- enrolled window with %ms_shaveinside. A window split by an exposure
-- becomes two windows.
--
-- Missing this made every cohort's denominator identical — 62,730 for
-- all 40 in the study compared — where SAS gives 62,462 for the
-- incident cohorts (washout 183) and 62,729 for the prevalent ones
-- (washout 0, where SAS skips the block entirely).
--
-- The `washout <> 0` gate is SAS's, not an optimisation: with no
-- washout the block does not run at all.
-- ---------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE _denom_unelig AS
WITH raw AS (
    SELECT DISTINCT
        m.denom_cfg_id,
        x.patid,
        x.adate + 1                                   AS unelig_start,
        x.adate + CAST(x.rxsup - 1 AS INTEGER)
                + CAST(c.wash_per AS INTEGER)         AS unelig_end
    FROM exposure_claims x
    JOIN cfg_denom_map m ON m.cohortgrp = x.cohortgrp
    JOIN cfg_cohort    c ON c.cohortgrp = x.cohortgrp
    WHERE c.wash_per <> 0

    UNION ALL

    -- Periods disqualified by an EXCLUSION condition. SAS shaves these
    -- out of the denominator too (ms_cidadenom.sas:145, `excl_incl`),
    -- so eligibility depends on the exclusion criteria, not only on
    -- enrolment and exposure.
    --
    -- The mapping is the inverse of the rule: an index at T is excluded
    -- when a matching code falls in [T + condfrom, T + condto], so a
    -- code at D disqualifies indices in [D - condto, D - condfrom].
    SELECT DISTINCT
        m.denom_cfg_id,
        s2.patid,
        s2.adate - r.condto                           AS unelig_start,
        s2.adate - r.condfrom                         AS unelig_end
    FROM cohort_claims s2
    JOIN cfg_inclusion_codes k ON k.code = s2.code
    JOIN cfg_inclusion r
      ON r.cohortgrp = k.cohortgrp AND r.criteria = k.criteria
     AND r.cond = k.cond AND r.subcond = k.subcond
     AND r.codecat = s2.codecat
    JOIN cfg_denom_map m ON m.cohortgrp = r.cohortgrp
    WHERE r.criteria = 'EXC'
      -- only plain index-anchored windows; an episode-end anchor has no
      -- meaning without an episode
      AND coalesce(r.condfromanchor, '') <> 'EPISODEENDDT'
      AND coalesce(r.condtoanchor, '')   <> 'EPISODEENDDT'
),
-- merge overlapping periods per member: a running max of the end seen
-- so far starts a new block whenever a gap appears
marked AS (
    SELECT *,
           CASE WHEN unelig_start <= max(unelig_end) OVER (
                    PARTITION BY denom_cfg_id, patid ORDER BY unelig_start
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                THEN 0 ELSE 1 END AS new_block
    FROM raw
),
blocks AS (
    SELECT *, sum(new_block) OVER (
                 PARTITION BY denom_cfg_id, patid ORDER BY unelig_start) AS blk
    FROM marked
)
SELECT denom_cfg_id, patid, min(unelig_start) AS unelig_start,
       max(unelig_end) AS unelig_end
FROM blocks GROUP BY 1, 2, blk;

-- Split each enrolled window around the ineligible periods inside it.
CREATE OR REPLACE TEMP TABLE _denom_windows_shaved AS
WITH overlapping AS (
    SELECT w.denom_cfg_id, w.patid, w.eligepisode, w.enr_start, w.enr_end,
           w.denom_start, w.denom_end,
           greatest(u.unelig_start, w.denom_start) AS us,
           least(u.unelig_end, w.denom_end)        AS ue
    FROM _denom_windows w
    JOIN _denom_unelig u
      ON u.denom_cfg_id = w.denom_cfg_id AND u.patid = w.patid
     AND u.unelig_start <= w.denom_end AND u.unelig_end >= w.denom_start
),
-- the gap BEFORE each ineligible period ...
gaps AS (
    SELECT denom_cfg_id, patid, eligepisode, enr_start, enr_end,
           coalesce(lag(ue) OVER (PARTITION BY denom_cfg_id, patid,
                                  eligepisode ORDER BY us) + 1,
                    denom_start)                    AS seg_start,
           us - 1                                   AS seg_end
    FROM overlapping
    UNION ALL
    -- ... and the tail after the last one
    SELECT denom_cfg_id, patid, eligepisode, enr_start, enr_end,
           max(ue) + 1, max(denom_end)
    FROM overlapping
    GROUP BY 1, 2, 3, 4, 5
)
SELECT denom_cfg_id, patid, eligepisode, enr_start, enr_end,
       seg_start AS denom_start, seg_end AS denom_end,
       span_days(seg_start, seg_end) AS memberdays
FROM gaps
WHERE seg_end >= seg_start
UNION ALL
-- windows with no ineligible period inside them pass through untouched
SELECT w.denom_cfg_id, w.patid, w.eligepisode, w.enr_start, w.enr_end,
       w.denom_start, w.denom_end, w.memberdays
FROM _denom_windows w
WHERE NOT EXISTS (
    SELECT 1 FROM _denom_unelig u
    WHERE u.denom_cfg_id = w.denom_cfg_id AND u.patid = w.patid
      AND u.unelig_start <= w.denom_end AND u.unelig_end >= w.denom_start);

DROP TABLE _denom_windows;
ALTER TABLE _denom_windows_shaved RENAME TO _denom_windows;

CREATE OR REPLACE TEMP TABLE _denom_demog AS
SELECT
    el.denom_cfg_id,
    el.patid,
    el.eligepisode,
    el.denom_start,
    el.denom_end,
    el.memberdays,
    dm.birth_date,
    dm.sex,
    dm.race,
    dm.hispanic
FROM _denom_windows el
JOIN demographics dm ON dm.patid = el.patid
LEFT JOIN demographics_multi   mx ON mx.patid = el.patid
LEFT JOIN demographics_missing ms ON ms.patid = el.patid
JOIN cfg_denom_demog ds ON ds.denom_cfg_id = el.denom_cfg_id
                 AND ds.dimension = 'sex' AND ds.value = dm.sex_raw
JOIN cfg_denom_demog dr ON dr.denom_cfg_id = el.denom_cfg_id
                 AND dr.dimension = 'race' AND dr.value = dm.race
JOIN cfg_denom_demog dh ON dh.denom_cfg_id = el.denom_cfg_id
                 AND dh.dimension = 'hispanic' AND dh.value = dm.hispanic
WHERE mx.patid IS NULL AND ms.patid IS NULL;

DROP TABLE _denom_windows;

-- Pass 2b: age band. Age is taken at the START of the eligible window,
-- the earliest date an index could occur in it.
CREATE OR REPLACE TEMP TABLE _denom_strat AS
SELECT
    d.denom_cfg_id,
    d.patid,
    d.eligepisode,
    d.denom_start,
    d.denom_end,
    d.memberdays,
    d.sex,
    d.race,
    d.hispanic,
    a.label                      AS agegroup,
    a.ordinal                    AS agegroupnum,
    year(d.denom_start)::VARCHAR AS index_year
FROM _denom_demog d
JOIN cfg_denom_strata a
  ON a.denom_cfg_id = d.denom_cfg_id
 AND age_in_unit(d.birth_date, d.denom_start, a.unit) BETWEEN a.lo AND a.hi;

DROP TABLE _denom_demog;

CREATE OR REPLACE TABLE denomcounts AS
SELECT
    lv.level_id                                        AS level,
    -- Fan out: one row per COHORT, from the config that produced it.
    m.cohortgrp                                        AS "group",
    CASE WHEN lv.has_agegroup THEN s.agegroup    END   AS agegroup,
    CASE WHEN lv.has_agegroup THEN s.agegroupnum END   AS agegroupnum,
    CASE WHEN lv.has_sex      THEN s.sex         END   AS sex,
    CASE WHEN lv.has_race     THEN s.race        END   AS race,
    CASE WHEN lv.has_hispanic THEN s.hispanic    END   AS hispanic,
    -- SAS column names (ms_cidadenom.sas:1386). `year`, not
    -- `index_year`; `DenNumPts`/`DenNumMemDays`, not
    -- `eligible_members`/`memberdays`. These are the metrics a
    -- downstream merge references by name, so they are a contract, not
    -- a description.
    CASE WHEN lv.has_year     THEN s.index_year  END   AS "year",
    -- Geography and finer time strata appear in SAS's shell and are
    -- stratified on only when a level asks for them. This package does
    -- not offer them as CIDA strata, so they are emitted NULL — the
    -- same convention already used for a level that does not stratify
    -- on agegroup or sex, and it keeps the column set matching.
    NULL::VARCHAR                                      AS zip_uncertain,
    NULL::VARCHAR                                      AS zip3,
    NULL::VARCHAR                                      AS state,
    NULL::VARCHAR                                      AS hhs_reg,
    NULL::VARCHAR                                      AS cb_reg,
    NULL::SMALLINT                                     AS "month",
    NULL::SMALLINT                                     AS quarter,
    count(DISTINCT s.patid)                            AS dennumpts,
    -- OUTPUTDENOM='M' reports members only: SAS sets DenNumMemDays to
    -- MISSING for those cohorts, and 0 for the rest
    -- (ms_cidadenom.sas:1346-1347). NULL, not 0 — "we did not count
    -- this" and "we counted zero days" are different statements.
    CASE WHEN m.output_denom = 'M' THEN NULL
         ELSE sum(s.memberdays) END                    AS dennummemdays
FROM _denom_strat s
-- one row per cohort sharing this config
JOIN cfg_denom_map m ON m.denom_cfg_id = s.denom_cfg_id
CROSS JOIN cfg_strata lv
GROUP BY
    lv.level_id, m.cohortgrp, m.output_denom,
    CASE WHEN lv.has_agegroup THEN s.agegroup    END,
    CASE WHEN lv.has_agegroup THEN s.agegroupnum END,
    CASE WHEN lv.has_sex      THEN s.sex         END,
    CASE WHEN lv.has_race     THEN s.race        END,
    CASE WHEN lv.has_hispanic THEN s.hispanic    END,
    CASE WHEN lv.has_year     THEN s.index_year  END;

DROP TABLE _denom_strat;
