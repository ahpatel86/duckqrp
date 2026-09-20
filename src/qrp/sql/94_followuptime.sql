-- =====================================================================
-- 94_followuptime.sql — msoc.<runid>_followuptime_cida
--
-- Requested via USERSTRATA `tableid='t2followuptime'`
-- (ms_cidanum.sas:2826-2831). Same macro as censor_cida with different
-- parameters:
--
--            censor                 followuptime
--   convar   timetocensor           followuptime
--   catvar   censdays_value_cat     fupdays_value_cat
--   flags    cens_elig cens_dth     adds fup_episend fup_spec fup_event
--            cens_dpend cens_qryend
--
-- Despite the docstring saying "every day of follow-up", rows are NOT
-- one per day. `fupdays_value_cat` buckets the follow-up duration, and
-- the table is grouped `by group level <censorstrat>` exactly as
-- censor_cida is (ms_createcensortable.sas:240-246).
--
-- Note the flag RENAME: dplocal carries `fup_*`, msoc carries `cens_*`
-- (the dplocalflaglist/msocflaglist pair at ms_cidanum.sas:2829-2830).
-- So the msoc column names are identical to censor_cida's, and only the
-- meaning of the underlying censor date differs.
-- =====================================================================

CREATE OR REPLACE TABLE followuptime AS
WITH censored AS (
    SELECT
        c.cohortgrp,
        c.patid,
        c.indexdt,
        c.episode,
        c.agegroup,
        c.sex,
        c.race,
        c.hispanic,
        year(c.indexdt)::VARCHAR AS index_year,
        cfg.blackout_per,
        cfg.at_risk_start,
        cfg.max_epis_dur,
        c.origepisenddt,
        c.deathdt,
        c.enr_end,
        -- For FOLLOWUPTIME the censor date is the earliest of episode
        -- end, disenrollment and the event — NOT the censoring date
        -- used by censor_cida, which ignores the event and uses the
        -- query/DP end instead (ms_finalizeptsmasterlist.sas:312).
        least(c.episodeenddt, c.enr_end,
              coalesce(c.eventdt, DATE '9999-12-31'))  AS censor_dt,
        c.eventdt,
        -- followuptime = Max(0, censor_dt - IndexDt - BLACKOUTPER
        --                       - ATRISKSTART + 1)   (line 300)
        greatest(0,
            date_diff('day', c.indexdt,
                      least(c.episodeenddt, c.enr_end,
                            coalesce(c.eventdt, DATE '9999-12-31')))
            - cfg.blackout_per - cfg.at_risk_start + 1
        )                                             AS followuptime
    FROM cohort_final c
    JOIN cfg_cohort cfg ON cfg.cohortgrp = c.cohortgrp
),
flagged AS (
    SELECT
        *,
        -- Censored due to enrollment. Reset when the episode was
        -- truncated by death and enrollment did not end on the death
        -- date — `enrend_death='C'` (lines 147-151). This package
        -- truncates enr_end at death, so the two coincide and the reset
        -- is expressed as "not also a death".
        CASE WHEN censor_dt = enr_end
              AND NOT (deathdt IS NOT NULL AND censor_dt = deathdt)
             THEN 1 ELSE 0 END                        AS fup_elig,
        CASE WHEN deathdt IS NOT NULL AND censor_dt = deathdt
             THEN 1 ELSE 0 END                        AS fup_dth,
        CASE WHEN censor_dt = DATE '{end_date}'
             THEN 1 ELSE 0 END                        AS fup_qryend,
        CASE WHEN censor_dt = DATE '{censor_date}'
              AND DATE '{censor_date}' <> DATE '{end_date}'
             THEN 1 ELSE 0 END                        AS fup_dpend,
        -- Episode ended of its own accord, either at its natural end or
        -- at the maximum episode duration (lines 349-350).
        CASE WHEN censor_dt = origepisenddt
               OR (max_epis_dur >= 0
                   AND censor_dt = indexdt + max_epis_dur - 1)
             THEN 1 ELSE 0 END                        AS fup_episend,
        -- Requester-defined truncation. `trunkdt` is the mock
        -- surveillance IndexLookEndDt, which this package does not
        -- model, so this is structurally 0 rather than omitted — the
        -- column must exist for the output to match.
        0                                             AS fup_spec,
        CASE WHEN eventdt IS NOT NULL AND censor_dt = eventdt
             THEN 1 ELSE 0 END                        AS fup_event
    FROM censored
),
bucketed AS (
    SELECT
        f.*,
        -- fupdays_value_cat: the follow-up duration bucketed by the
        -- study's FOLLOWUPTIME_OUTPUT_CAT cut points
        -- (ms_createcensortable.sas:54). With no cut points supplied
        -- the value is reported ungrouped, which is what SAS does when
        -- the parameter is blank.
        CAST(f.followuptime AS VARCHAR) AS fupdays_value_cat
    FROM flagged f
)
SELECT
    b.cohortgrp              AS "group",
    lv.level_id              AS level,
    b.fupdays_value_cat,
    -- Stratum values are NULL for a level that does not stratify on
    -- them — the same shape t2_cida produces.
    -- ALL the USERSTRATA levelvars, not just two. `censorstrat` is
    -- the levelvars (ms_cidanum.sas:123), so race, hispanic and year
    -- stratify this table exactly as agegroup and sex do. Honouring
    -- only agegroup and sex meant a level asking for `year` got the
    -- UNSTRATIFIED totals labelled as that level — every level came
    -- back with identical row counts. The production study seen
    -- stratifies on `year`.
    CASE WHEN lv.has_agegroup THEN b.agegroup END AS agegroup,
    CASE WHEN lv.has_sex      THEN b.sex      END AS sex,
    CASE WHEN lv.has_race     THEN b.race     END AS race,
    CASE WHEN lv.has_hispanic THEN b.hispanic END AS hispanic,
    CASE WHEN lv.has_year     THEN b.index_year END AS "year",
    count(*)                 AS episodes,
    -- msoc carries the cens_* names even for followuptime; the rename
    -- is in the macro call, not in the data.
    sum(b.fup_elig)          AS cens_elig,
    sum(b.fup_dth)           AS cens_dth,
    sum(b.fup_qryend)        AS cens_qryend,
    sum(b.fup_dpend)         AS cens_dpend,
    sum(b.fup_episend)       AS cens_episend,
    sum(b.fup_spec)          AS cens_spec,
    sum(b.fup_event)         AS cens_event
FROM bucketed b
-- USERSTRATA is optional; SAS still produces the table, unstratified,
-- at level 1 (`_censorlevels` always has a row). Without this fallback
-- the CROSS JOIN yields NOTHING for a study that defines no strata —
-- an msoc output silently absent rather than unstratified.
CROSS JOIN (
    SELECT level_id, has_agegroup, has_sex, has_race,
           has_hispanic, has_year FROM cfg_strata
    UNION ALL
    SELECT '1', FALSE, FALSE, FALSE, FALSE, FALSE
    WHERE NOT EXISTS (SELECT 1 FROM cfg_strata)
) lv
GROUP BY ALL
ORDER BY 1, 2, 3;
