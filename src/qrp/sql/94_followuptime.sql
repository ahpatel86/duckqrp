-- ==================================================================
-- Follow-up time table (msoc <runid>_followuptime_cida), as SAS builds
-- it in ms_createcensortable.sas.
--
-- Per-episode values come from their ONE definition each: followuptime,
-- fupdays_value_cat and Censorcat_sort from the master list, the fup_*
-- flags from episode_flags (60_followup.sql). This file used to recompute
-- all of them; its copy drifted (follow-up days on some episodes) and
-- hard-coded fup_spec to 0.
--
-- SAS reports the fup_* flags under cens_* names here: the rename at
-- ms_finalizeptsmasterlist.sas:393 belongs to THIS output.
--
-- Levels come from USERSTRATA (tableid t2followuptime): censdays_value
-- groups by exact follow-up days; censdays_value_cat by SAS's category,
-- which brings censorcat_sort with it (lines 63-66), with values from
-- fupdays_value_cat (line 227). Shell rows square the table (see ep).
-- Other level variables stratify as in
-- t2_cida. Without USERSTRATA the table is still produced, unstratified,
-- at level 1.
-- ==================================================================
CREATE OR REPLACE TABLE followuptime AS
WITH ep AS (
    SELECT c.cohortgrp, c.agegroup, c.sex, c.race, c.hispanic,
           year(c.indexdt)::VARCHAR AS index_year,
           c.followuptime, c."Censorcat_sort" AS censorcat_sort, c.fupdays_value_cat,
           f.fup_elig, f.fup_dpend, f.fup_episend, f.fup_spec, f.fup_event,
           1 AS epi
    FROM cohort_final c
    JOIN episode_flags f
      ON f.cohortgrp = c.cohortgrp AND f.patid = c.patid AND f.indexdt = c.indexdt
    UNION ALL
    -- SAS's squared shell (ms_createcensortable.sas:170-185): one row per
    -- cohort in the cohort file, episode indicator 0, censdays_value = 1,
    -- crossed with every follow-up category. So every cohort has a row at
    -- 1 day and every category, with 0 episodes where none occur. The
    -- categories are the master list's buckets (62_mstr_wide.sql); both
    -- should come from the study's FOLLOWUPTIME_OUTPUT_CAT (follow-up).
    SELECT k.cohortgrp, NULL, NULL, NULL, NULL, NULL,
           1, cat.sort, cat.label, 0, 0, 0, 0, 0, 0
    FROM cfg_cohort k
    CROSS JOIN (VALUES ('0-90', 1), ('91-180', 2), ('181+', 3)) cat(label, sort)
)
SELECT
    ep.cohortgrp                                            AS "group",
    lv.level_id                                             AS level,
    CASE WHEN lv.has_censdays_value THEN ep.followuptime END       AS censdays_value,
    CASE WHEN lv.has_censdays_cat   THEN ep.censorcat_sort END     AS censorcat_sort,
    CASE WHEN lv.has_censdays_cat   THEN ep.fupdays_value_cat END  AS censdays_value_cat,
    CASE WHEN lv.has_agegroup THEN ep.agegroup END          AS agegroup,
    CASE WHEN lv.has_sex      THEN ep.sex END               AS sex,
    CASE WHEN lv.has_race     THEN ep.race END              AS race,
    CASE WHEN lv.has_hispanic THEN ep.hispanic END          AS hispanic,
    CASE WHEN lv.has_year     THEN ep.index_year END        AS "year",
    sum(ep.epi)                                             AS episodes,
    sum(ep.fup_elig)                                        AS cens_elig,
    sum(ep.fup_dpend)                                       AS cens_dpend,
    sum(ep.fup_episend)                                     AS cens_episend,
    sum(ep.fup_spec)                                        AS cens_spec,
    sum(ep.fup_event)                                       AS cens_event
FROM ep
CROSS JOIN (
    SELECT level_id, has_agegroup, has_sex, has_race, has_hispanic, has_year,
           has_censdays_value, has_censdays_cat
    FROM cfg_strata WHERE tableid = 't2followuptime'
    UNION ALL
    -- no USERSTRATA: unstratified, at level 1 (SAS's _censorlevels)
    SELECT '1', FALSE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE
    WHERE NOT EXISTS (SELECT 1 FROM cfg_strata)
) lv
GROUP BY ALL
ORDER BY 1, 2, 3, 4;
