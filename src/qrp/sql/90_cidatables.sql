-- =====================================================================
-- 90_cidatables.sql — the T2_CIDA output table (ms_cidatables.sas).
--
-- This is the study deliverable: one row per (cohort, level, stratum)
-- with cohort metrics. SAS stacks one block per USERSTRATA level, each
-- labelled with its `Level`, which is what `cfg_strata` reproduces here.
--
-- The two-stage aggregation matters
-- --------------------------------
-- SAS computes numerators in two passes (ms_cidatables.sas:115-140):
--
--   pass 1: class group PatId <levelvars>   -> max(Patient)=Npts,
--                                              sum(...)=everything else
--   pass 2: class group <levelvars>         -> sum= over pass 1
--
-- `max(Patient)` per patient followed by `sum` is how SAS counts
-- DISTINCT patients without a distinct operator. `count(DISTINCT patid)`
-- is the same thing, so this is written as one pass — but the two-stage
-- shape is why every other metric is a plain sum: they are summed within
-- a patient first, then across patients, which for a sum is associative.
--
-- Metric names are SAS's, because this table leaves the site and is read
-- against SAS documentation.
-- =====================================================================

CREATE OR REPLACE TABLE _t2_num AS
WITH base AS (
    SELECT
        c.cohortgrp                                   AS "group",
        c.patid,
        c.agegroup,
        c.agegroupnum,
        c.sex,
        c.race,
        c.hispanic,
        year(c.indexdt)::VARCHAR                      AS index_year,
        1                                             AS patient,
        -- numdispensing is the count of same-day-collapsed claims that
        -- formed the index dispensing; SAS's RawDisp is the raw claim
        -- count and AdjustedDisp the post-stockpiling count.
        c.numdispensing                               AS rawdisp,
        1                                             AS adjusteddisp,
        c.episode_rxsup                               AS totrxsup,
        c.rxamt                                       AS totrxamt,
        c.numevents                                   AS numevents,
        c.has_event                                   AS hadevent,
        c.person_days                                 AS followuptime,
        -- time from index to the end of available data, which is what
        -- SAS calls timetocensor
        span_days(c.indexdt, c.dataavail_dt)          AS timetocensor{covar_base}
    FROM cohort_final c
)
SELECT
    lv.level_id                                        AS level,
    b."group",
    -- Stratum values are NULL for a level that does not stratify on
    -- them, which is what GROUPING SETS produces and what the SAS shell
    -- leaves blank.
    CASE WHEN lv.has_agegroup THEN b.agegroup    END   AS agegroup,
    CASE WHEN lv.has_agegroup THEN b.agegroupnum END   AS agegroupnum,
    CASE WHEN lv.has_sex      THEN b.sex         END   AS sex,
    CASE WHEN lv.has_race     THEN b.race        END   AS race,
    CASE WHEN lv.has_hispanic THEN b.hispanic    END   AS hispanic,
    CASE WHEN lv.has_year     THEN b.index_year  END   AS index_year{covar_select},
    count(DISTINCT b.patid)                            AS npts,
    sum(b.patient)                                     AS episodes,
    sum(b.adjusteddisp)                                AS adjustedcodecount,
    sum(b.rawdisp)                                     AS rawcodecount,
    sum(b.totrxsup)                                    AS daysupp,
    sum(b.totrxamt)                                    AS amtsupp,
    sum(b.numevents)                                   AS all_events,
    sum(b.hadevent)                                    AS eps_wevents,
    sum(b.followuptime)                                AS followuptime,
    sum(b.timetocensor)                                AS timetocensor
FROM base b
CROSS JOIN (SELECT * FROM cfg_strata
            WHERE tableid = 't2cida') lv
GROUP BY
    lv.level_id, b."group",
    CASE WHEN lv.has_agegroup THEN b.agegroup    END,
    CASE WHEN lv.has_agegroup THEN b.agegroupnum END,
    CASE WHEN lv.has_sex      THEN b.sex         END,
    CASE WHEN lv.has_race     THEN b.race        END,
    CASE WHEN lv.has_hispanic THEN b.hispanic    END,
    CASE WHEN lv.has_year     THEN b.index_year  END{covar_group}
;

-- Merge numerators with denominators.
--
-- SAS: "The table to be generated in msoc will be the result of a merge
-- between numerators and denominators" (ms_cidatables.sas:16), joined on
-- the common level values. A FULL join, not a left one: a stratum can
-- have eligible members but no exposed episodes, and reporting that as
-- absent rather than as a zero numerator would understate the
-- denominator.
CREATE OR REPLACE TABLE t2_cida AS
SELECT
    coalesce(n.level, d.level)             AS level,
    coalesce(n."group", d."group")         AS "group",
    coalesce(n.agegroup, d.agegroup)       AS agegroup,
    coalesce(n.agegroupnum, d.agegroupnum) AS agegroupnum,
    coalesce(n.sex, d.sex)                 AS sex,
    coalesce(n.race, d.race)               AS race,
    coalesce(n.hispanic, d.hispanic)       AS hispanic,
    -- SAS calls this `year` (ms_cidatables.sas:425), not `index_year`.
    -- Fixed in denomcounts earlier and not carried across to here, so
    -- the two tables named the same column differently — and the merge
    -- between them is by name.
    -- Covariate strata (SAS `&covarstrat.`) come from the NUMERATOR
    -- only: denomcounts is built from enrolled members, who need not
    -- have an episode and so have no covariate value.
    coalesce(n.index_year, d."year")       AS "year"{covar_final},
    -- Finer time and geography strata appear in SAS's retain list and
    -- are populated only when a level stratifies on them. This package
    -- does not offer them as CIDA strata, so they are emitted NULL —
    -- the same convention already used for agegroup and sex on a level
    -- that does not stratify, and it keeps the column set matching.
    NULL::SMALLINT                         AS "month",
    NULL::SMALLINT                         AS quarter,
    NULL::VARCHAR                          AS zip3,
    NULL::VARCHAR                          AS state,
    NULL::VARCHAR                          AS hhs_reg,
    NULL::VARCHAR                          AS cb_reg,
    NULL::VARCHAR                          AS zip_uncertain,
    -- numerator metrics; zero where the stratum has no episodes
    coalesce(n.npts, 0)              AS npts,
    coalesce(n.episodes, 0)          AS episodes,
    coalesce(n.adjustedcodecount, 0) AS adjustedcodecount,
    coalesce(n.rawcodecount, 0)      AS rawcodecount,
    coalesce(n.daysupp, 0)           AS daysupp,
    coalesce(n.amtsupp, 0)           AS amtsupp,
    coalesce(n.all_events, 0)        AS all_events,
    coalesce(n.eps_wevents, 0)       AS eps_wevents,
    coalesce(n.followuptime, 0)      AS followuptime,
    coalesce(n.timetocensor, 0)      AS timetocensor,
    -- Denominator metrics. SAS calls these DenNumPts / DenNumMemDays
    -- in BOTH t2_cida and denomcounts (ms_cidatables.sas uses them 9
    -- times each). They were `eligible_members`/`memberdays` here —
    -- descriptive, but not what a downstream merge references.
    coalesce(d.dennumpts, 0)         AS dennumpts,
    -- A MISSING denominator row means zero; a PRESENT row with NULL
    -- member-days means "not counted" — OUTPUTDENOM='M' asks for
    -- members only, and `denomcounts` already writes NULL there.
    -- Coalescing both to 0 turned "not counted" into "counted zero",
    -- which is the one distinction that column carries.
    CASE WHEN d.level IS NULL THEN 0 ELSE d.dennummemdays END
                                     AS dennummemdays
FROM _t2_num n
FULL JOIN denomcounts d
  ON  d.level      = n.level
 AND  d."group"    = n."group"
 AND  d.agegroup    IS NOT DISTINCT FROM n.agegroup
 AND  d.sex         IS NOT DISTINCT FROM n.sex
 AND  d.race        IS NOT DISTINCT FROM n.race
 AND  d.hispanic    IS NOT DISTINCT FROM n.hispanic
 AND  d."year"      IS NOT DISTINCT FROM n.index_year
ORDER BY level, "group", agegroupnum, sex, race, hispanic, index_year;

-- dplocal.<runid>_numcounts — the numerator detail behind t2_cida.
-- SAS writes it alongside the msoc table (ms_cidatables.sas:418), and
-- it was being computed here and then thrown away: a DP had the CIDA
-- numerators only in their merged form, with no way to check them
-- against the denominators separately.
CREATE OR REPLACE TABLE numcounts AS
SELECT * FROM _t2_num;

DROP TABLE _t2_num;
