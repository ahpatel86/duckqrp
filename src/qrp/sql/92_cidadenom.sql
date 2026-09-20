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
        e.enr_start + c.enr_days        AS denom_start,
        -- end: censor date or enrollment end, pulled back by the
        -- longest forward-looking requirement an index date would need
        least(e.enr_end, DATE '{censor_date}')
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
    sum(s.memberdays)                                  AS dennummemdays
FROM _denom_strat s
-- one row per cohort sharing this config
JOIN cfg_denom_map m ON m.denom_cfg_id = s.denom_cfg_id
CROSS JOIN cfg_strata lv
GROUP BY
    lv.level_id, m.cohortgrp,
    CASE WHEN lv.has_agegroup THEN s.agegroup    END,
    CASE WHEN lv.has_agegroup THEN s.agegroupnum END,
    CASE WHEN lv.has_sex      THEN s.sex         END,
    CASE WHEN lv.has_race     THEN s.race        END,
    CASE WHEN lv.has_hispanic THEN s.hispanic    END,
    CASE WHEN lv.has_year     THEN s.index_year  END;

DROP TABLE _denom_strat;
