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
    cohortgrp, step_no, step, records, patients,
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
CREATE OR REPLACE TABLE censoring AS
SELECT
    cohortgrp,
    exit_reason,
    count(*)          AS episodes,
    sum(person_days)  AS person_days,
    round(100.0 * count(*)
          / sum(count(*)) OVER (PARTITION BY cohortgrp), 1) AS pct
FROM cohort_final
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
