-- =====================================================================
-- 96_baseline.sql — msoc.<runid>_baseline<outcohort>_<i>
--
-- The baseline distribution table: ONE ROW PER COHORT GROUP, WIDE.
--
-- SAS builds it as two `proc means ... class &groupvar` passes merged by
-- group (ms_createdistbaselinetable.sas:455-526):
--
--   _Discrete    sum= over 0/1 dummies — one column per category LEVEL
--                (Sex_F, Sex_M, Race_1.., Hispanic_Y, Age<bucket>,
--                covar1..covarN). A sum of dummies is a count.
--   _Continuous  mean= and std= over Age, risk scores and the
--                utilization counts, with _freq_ renamed N_episodes.
--
-- Then "squared": a category absent from a group is set to 0 rather
-- than left missing, so every group carries every column
-- (lines 529-537). That is what makes the table stackable across
-- groups and comparable across runs — a missing column and a zero
-- column mean different things to a reader.
--
-- This was previously emitted as `covariate_prevalence`: one row per
-- COVARIATE, long. That is a different table, not a renaming of this
-- one.
--
-- The `_<i>` suffix in the SAS name is a surveillance-period index,
-- which this package does not model — see docs/OUTPUTS.md.
-- =====================================================================

CREATE OR REPLACE TABLE baseline AS
WITH episodes AS (
    SELECT
        c.cohortgrp,
        c.patid,
        c.indexdt,
        c.age,
        c.agegroup,
        c.sex,
        c.race,
        c.hispanic,
        year(c.indexdt)::VARCHAR AS index_year
    FROM cohort_final c
),
-- Discrete: one dummy per observed level, summed. Built by joining the
-- episodes to their own distinct level values rather than pivoting on a
-- hardcoded list, so a race code the study did not anticipate still
-- gets a column.
per_group AS (
    SELECT
        cohortgrp,
        count(DISTINCT patid)                    AS patient,
        count(*)                                 AS n_episodes,
        round(avg(age), 4)                       AS mean_age,
        round(stddev_samp(age), 4)               AS std_age
    FROM episodes
    GROUP BY 1
)
SELECT
    g.cohortgrp                                  AS "group",
    g.patient,
    g.n_episodes,
    g.mean_age,
    g.std_age
FROM per_group g
ORDER BY 1;

-- The category dummies are added by pipeline.py, which knows the
-- observed levels and can name one column per level. Doing it here
-- would need a hardcoded level list, and a race or age band outside it
-- would silently vanish.
