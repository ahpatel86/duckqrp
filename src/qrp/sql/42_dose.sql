-- =====================================================================
-- 35_dose.sql — %ms_pov1dose: cumulative dose and current filled daily
--               dose restrictions on candidate index dates.
--
-- Only built when some cohort sets a dose limit; `StudyConfig.any_dose`
-- decides that in Python, so an unused stage costs zero rather than the
-- unconditional `.limit(1).count()` probe the port paid on every call
-- (documented as root cause 2c of its own perf regression).
--
-- Cumulative dose over a lookback window is the textbook case for a
-- RANGE-framed window. `RANGE BETWEEN INTERVAL n DAY PRECEDING AND
-- INTERVAL 1 DAY PRECEDING` frames by VALUE rather than row position, so
-- "all dispensings in the 180 days before this one" is a frame spec
-- rather than a self-join with a BETWEEN predicate. One sort, no fan-out.
-- =====================================================================

-- Dose per dispensing. Strength comes from the code lookup, keyed on the
-- dispensed code — a config table, not a hardcoded map.
--
-- Views, not tables: each of these is consumed exactly once, so keeping
-- them lazy lets the optimiser fuse the whole chain into a single
-- pipeline and push filters into the scan.
-- Prior cumulative dose in the lookback window, per candidate index date.
CREATE OR REPLACE VIEW dose_lookback AS
SELECT
    d.cohortgrp,
    d.patid,
    d.adate,
    d.cfdd,
    sum(d.cumdose) OVER (
        PARTITION BY d.cohortgrp, d.patid
        ORDER BY d.adate
        RANGE BETWEEN INTERVAL (c.cum_dose_per) DAY PRECEDING
                  AND INTERVAL 1 DAY PRECEDING
    ) AS prior_cumdose
FROM claim_dose d
JOIN cfg_cohort c
  ON c.cohortgrp = d.cohortgrp
WHERE c.cum_dose_per IS NOT NULL;

-- Index dates failing a dose criterion. Kept as its own table because
-- attrition reports the exclusion count.
CREATE OR REPLACE TABLE dose_excluded AS
SELECT DISTINCT
    i.cohortgrp,
    i.patid,
    i.adate,
    CASE
        WHEN c.min_cum_dose IS NOT NULL
             AND round(COALESCE(l.prior_cumdose, 0)) < c.min_cum_dose
             THEN 'min_cumdose'
        WHEN c.max_cum_dose IS NOT NULL
             AND round(COALESCE(l.prior_cumdose, 0)) > c.max_cum_dose
             THEN 'max_cumdose'
        WHEN c.min_cfdd IS NOT NULL AND round(l.cfdd) < c.min_cfdd
             THEN 'min_cfdd'
        WHEN c.max_cfdd IS NOT NULL AND round(l.cfdd) > c.max_cfdd
             THEN 'max_cfdd'
    END AS reason
FROM index_candidates i
JOIN cfg_cohort c
  ON c.cohortgrp = i.cohortgrp
LEFT JOIN dose_lookback l
  ON l.cohortgrp = i.cohortgrp
 AND l.patid     = i.patid
 AND l.adate     = i.orig_adate
WHERE
    -- SAS compares round(cumdose,1) and round(cfdd,1). In SAS the second
    -- argument is the rounding UNIT, so that is round-to-nearest-integer,
    -- not one decimal place. Comparing raw values disagrees at the
    -- boundary: cumdose 99.6 against mincumdose 100 is included by SAS
    -- and was excluded here.
    (c.min_cum_dose IS NOT NULL
     AND round(COALESCE(l.prior_cumdose, 0)) < c.min_cum_dose)
 OR (c.max_cum_dose IS NOT NULL
     AND round(COALESCE(l.prior_cumdose, 0)) > c.max_cum_dose)
 OR (c.min_cfdd IS NOT NULL AND (l.cfdd IS NULL OR round(l.cfdd) < c.min_cfdd))
 OR (c.max_cfdd IS NOT NULL AND (l.cfdd IS NULL OR round(l.cfdd) > c.max_cfdd));

-- Apply the restriction. Rebuilding index_candidates in place keeps
-- every downstream stage unaware of whether dose logic ran at all.
CREATE OR REPLACE TABLE index_candidates AS
SELECT i.*
FROM index_candidates i
LEFT JOIN dose_excluded x
  ON x.cohortgrp = i.cohortgrp
 AND x.patid     = i.patid
 AND x.adate     = i.adate
WHERE x.patid IS NULL;
