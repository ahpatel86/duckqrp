-- =====================================================================
-- 20_enrollment.sql — %ms_episoderec2 + %ms_create_enrollment_spans.
--
-- Builds continuous enrollment episodes for EVERY distinct enrollment
-- configuration in one pass. The PySpark port looped
-- `for enr in range(1, num_enroll_groups + 1)` and re-scanned the full
-- enrollment table each time; here the config is a table and the scan
-- happens once regardless of how many configurations exist.
--
-- Distinct configurations, not distinct cohorts: two cohorts that share
-- (coverage, enrol_gap, chart_required) share one enrollment build.
-- That deduplication is free here and was structurally impossible in a
-- Python loop keyed on cohort.
-- =====================================================================

-- Chart restriction and span selection are CTEs, not temp tables: each
-- has exactly one consumer, so materialising them was a barrier that
-- cost a write and a read for nothing. Same rule as everywhere else in
-- this package — materialise on fan-out, stay lazy on single use.
CREATE OR REPLACE TABLE enrollment_spans AS
WITH nochart AS (
    -- Patients with a non-chart span overlapping the study window are
    -- dropped entirely, per configuration.
    SELECT DISTINCT c.enr_cfg_id, e.patid
    FROM cfg_enrollment c
    JOIN cdm_enrollment e
      ON e.chart <> 'Y'
     AND periods_overlap(e.enr_start, e.enr_end,
                         DATE '{start_date}', DATE '{end_date}')
    WHERE c.chart_required
),
spans AS (
    -- Qualifying raw spans per configuration.
    SELECT
        c.enr_cfg_id,
        e.patid,
        e.enr_start,
        e.enr_end,
        e.medcov,
        e.drugcov,
        c.enrol_gap,
        c.coverage
    FROM cfg_enrollment c
    JOIN cdm_enrollment e
      ON has_coverage(e.medcov, e.drugcov, c.coverage)
    LEFT JOIN nochart nc
      ON nc.enr_cfg_id = c.enr_cfg_id AND nc.patid = e.patid
    WHERE nc.patid IS NULL
),
flagged AS (
    -- Collapse consecutive spans separated by <= enrol_gap days into one
    -- episode. A span also ruptures when the coverage flags change,
    -- matching the SAS BY-group logic.
    SELECT
        *,
        lag(enr_end)  OVER w AS prev_end,
        lag(medcov)   OVER w AS prev_medcov,
        lag(drugcov)  OVER w AS prev_drugcov
    FROM spans
    WINDOW w AS (PARTITION BY enr_cfg_id, patid ORDER BY enr_start, enr_end)
),
breaks AS (
    SELECT
        *,
        CASE
            WHEN prev_end IS NULL THEN 1
            WHEN date_diff('day', prev_end, enr_start) - 1 > enrol_gap THEN 1
            WHEN coverage = 'MD'
                 AND (medcov <> prev_medcov OR drugcov <> prev_drugcov) THEN 1
            WHEN coverage = 'M' AND medcov  <> prev_medcov  THEN 1
            WHEN coverage = 'D' AND drugcov <> prev_drugcov THEN 1
            ELSE 0
        END AS is_break
    FROM flagged
),
numbered AS (
    -- The break flag plus a running SUM is the standard gap-and-islands
    -- form; here it costs one sort rather than three Window specs.
    SELECT
        *,
        sum(is_break) OVER (
            PARTITION BY enr_cfg_id, patid
            ORDER BY enr_start, enr_end
            ROWS UNBOUNDED PRECEDING
        ) AS episode
    FROM breaks
)
SELECT
    enr_cfg_id,
    patid,
    episode,
    min(enr_start) AS enr_start,
    max(enr_end)   AS enr_end
FROM numbered
GROUP BY 1, 2, 3;
