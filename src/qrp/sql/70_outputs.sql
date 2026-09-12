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

-- Attrition waterfall. Each step is a named count over the same base,
-- so the steps stay legible and the order is data, not control flow.
CREATE OR REPLACE TABLE attrition AS
WITH steps AS (
    SELECT cohortgrp, 1 AS step_no, 'Exposure dispensings'        AS step,
           count(*) AS records, count(DISTINCT patid) AS patients
    FROM exposure_claims GROUP BY 1
    UNION ALL
    SELECT cohortgrp, 2, 'After stockpiling',
           count(*), count(DISTINCT patid)
    FROM stockpiled GROUP BY 1
    UNION ALL
    SELECT cohortgrp, 3, 'Met washout (potential index)',
           count(*), count(DISTINCT patid)
    FROM index_candidates GROUP BY 1
    UNION ALL
    SELECT cohortgrp, 4, 'With enrollment and demographics',
           count(*), count(DISTINCT patid)
    FROM pov1 GROUP BY 1
    UNION ALL
    SELECT cohortgrp, 5, 'Met episode and enrollment criteria',
           count(*), count(DISTINCT patid)
    FROM ptsmasterlist GROUP BY 1
    UNION ALL
    SELECT cohortgrp, 6, 'Met follow-up washout (final cohort)',
           count(*), count(DISTINCT patid)
    FROM cohort_final GROUP BY 1
)
SELECT
    -- SAS column names and order: group, level, descr, claim_level,
    -- remaining, excluded (ms_attrition_cidacompute.sas:104-115). This
    -- is an msoc output, so those names are a contract — a reader
    -- matching on `level` should not have to know it was called
    -- `step_no` here.
    --
    -- `claim_level` is "Member" or "Episode" and says which unit
    -- remaining/excluded are counted in. SAS carries ONE pair plus that
    -- label; this keeps both units as extra columns after the contract
    -- ones, since the information is already computed and losing it to
    -- match a shape would be a worse trade than carrying it.
    cohortgrp                       AS "group",
    step_no                         AS level,
    step                            AS descr,
    -- Steps 1-3 narrow CLAIMS; 4 onward narrow EPISODES. SAS declares
    -- the unit per step and computes the counts inside the macro, so it
    -- never subtracts across the boundary.
    CASE WHEN step_no <= 3 THEN 'Claim' ELSE 'Episode' END AS claim_level,
    CASE WHEN step_no <= 3 THEN records ELSE records END   AS remaining,
    -- NULL at the unit change, not a number. Differencing a claim count
    -- against an episode count produces a plausible-looking value that
    -- means nothing — the first version of this reported 66,921
    -- "excluded" against 53,107 remaining. A missing value is honest;
    -- a wrong one is not.
    CASE WHEN step_no = 4 THEN NULL
         ELSE lag(records) OVER w - records END            AS excluded,
    -- Beyond the contract: both units side by side.
    records,
    patients,
    lag(records)  OVER w - records  AS records_dropped,
    lag(patients) OVER w - patients AS patients_dropped
FROM steps
WINDOW w AS (PARTITION BY cohortgrp ORDER BY step_no)
ORDER BY cohortgrp, step_no;

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
        c.deathdt,
        c.enr_end,
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
),
flagged AS (
    SELECT
        *,
        -- Censored by disenrollment, unless that coincides with death,
        -- which is the enrend_death='C' reset (lines 147-151).
        CASE WHEN censor_dt = enr_end
              AND NOT (deathdt IS NOT NULL AND censor_dt = deathdt)
             THEN 1 ELSE 0 END                        AS cens_elig,
        CASE WHEN deathdt IS NOT NULL AND censor_dt = deathdt
             THEN 1 ELSE 0 END                        AS cens_dth,
        CASE WHEN censor_dt = DATE '{end_date}'
             THEN 1 ELSE 0 END                        AS cens_qryend,
        CASE WHEN censor_dt = DATE '{censor_date}'
              AND DATE '{censor_date}' <> DATE '{end_date}'
             THEN 1 ELSE 0 END                        AS cens_dpend
    FROM censored
)
SELECT
    f.cohortgrp              AS "group",
    lv.level_id              AS level,
    CAST(f.timetocensor AS VARCHAR) AS censdays_value_cat,
    CASE WHEN lv.has_agegroup THEN f.agegroup END AS agegroup,
    CASE WHEN lv.has_sex      THEN f.sex      END AS sex,
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
    SELECT level_id, has_agegroup, has_sex FROM cfg_strata
    UNION ALL
    SELECT '1', FALSE, FALSE
    WHERE NOT EXISTS (SELECT 1 FROM cfg_strata)
) lv
GROUP BY ALL
ORDER BY 1, 2, 3;
