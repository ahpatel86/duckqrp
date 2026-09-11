-- =====================================================================
-- 72_codedistribution.sql — index code enumeration (ms_codedistribution).
--
-- Answers "which codes, and which COMBINATIONS of codes, defined the
-- index events" — the table a reviewer uses to sanity-check an exposure
-- definition before trusting the cohort.
--
-- SAS builds it in four steps (ms_codedistribution.sas:250-278):
--   1. sort by (patid, indexdate, distindexID)
--   2. concatenate the IDs per index date into `distindexlist`,
--      underscore-separated
--   3. count episodes per distinct list
--   4. emit the code -> ID map alongside, so the list can be decoded
--
-- The sort matters: it is what makes the list canonical, so two
-- episodes defined by the same set of codes in a different claim order
-- land in the same bucket. Aggregating without ordering would scatter
-- them.
--
-- Two output tables, both MSOC (aggregate only):
--   distindexmap    code -> numeric ID
--   distindex       episode counts per code combination
-- =====================================================================

-- Stable numeric IDs for the index-defining codes, assigned in code
-- order so a rerun on the same study produces the same map.
CREATE OR REPLACE TABLE distindexmap AS
SELECT
    cohortgrp                                   AS "group",
    'EXP'                                       AS distindextype,
    code,
    dense_rank() OVER (PARTITION BY cohortgrp ORDER BY code) AS distindexid
FROM (SELECT DISTINCT cohortgrp, code FROM cfg_codes WHERE role = 'DEF');

-- Which codes actually defined each index date, and the canonical list.
CREATE OR REPLACE TABLE distindex AS
WITH index_codes AS (
    -- The exposure claims falling on an episode's index date. Uses
    -- orig_adate: stockpiling may have shifted the supply window, but
    -- the CODE that defined the index is the one dispensed that day.
    SELECT DISTINCT
        m.cohortgrp,
        m.patid,
        m.indexdt,
        e.code
    FROM ptsmasterlist m
    JOIN exposure_claims e
      ON e.cohortgrp = m.cohortgrp
     AND e.patid     = m.patid
     AND e.adate     = m.indexdt
),
listed AS (
    SELECT
        ic.cohortgrp,
        ic.patid,
        ic.indexdt,
        -- ORDER BY inside the aggregate is what makes the list
        -- canonical; without it the same code set could produce
        -- different strings and split across buckets.
        string_agg(map.distindexid::VARCHAR, '_'
                   ORDER BY map.distindexid) AS distindexlist
    FROM index_codes ic
    JOIN distindexmap map
      ON map."group" = ic.cohortgrp
     AND map.code    = ic.code
    GROUP BY 1, 2, 3
)
SELECT
    cohortgrp        AS "group",
    'EXP'            AS distindextype,
    distindexlist,
    count(*)         AS episodes,
    count(DISTINCT patid) AS npts
FROM listed
GROUP BY 1, 2, 3
ORDER BY episodes DESC;
