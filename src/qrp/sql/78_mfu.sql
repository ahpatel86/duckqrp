-- =====================================================================
-- 78_mfu.sql — Most Frequent Use analysis (ms_mfu.sas).
--
-- "Which codes appear most often in this cohort" — used to review what
-- a population is actually being treated for or diagnosed with, rather
-- than what the study assumed.
--
-- Two parameters shape the answer (ms_mfu.sas:318-344):
--
--   COUNTMETHOD  rank by `codecount` (total claims) or by `patcount`
--                (distinct patients). These give DIFFERENT orderings:
--                a code appearing 50 times in one patient outranks a
--                code appearing once in 40 patients by claim count, and
--                loses badly by patient count. Which one is right
--                depends on the question, so it is config.
--
--   TOPXX        how many to keep per group.
--
-- SAS ranks with a retained counter over a sorted set, keeping
-- `rank <= topxx`. That is `row_number()` with a QUALIFY, and the
-- ordering needs a tie-break or two codes with equal counts could
-- swap between runs.
-- =====================================================================

CREATE OR REPLACE TABLE mfu AS
WITH claims AS (
    -- Claims for cohort members within the analysis window. Both code
    -- domains, tagged so the output says which one a code came from.
    SELECT
        m.cohortgrp,
        a.analysisnum,
        d.code,
        'DX'   AS codecat,
        d.codetype,
        m.patid
    FROM ptsmasterlist m
    JOIN cfg_mfu a
      ON a.cohortgrp = m.cohortgrp
     AND a.codecat   = 'DX'
    JOIN cdm_diagnosis d
      ON d.patid = m.patid
     AND d.adate BETWEEN m.indexdt + a.mfufrom AND m.indexdt + a.mfuto

    UNION ALL

    SELECT
        m.cohortgrp,
        a.analysisnum,
        r.code,
        'RX'   AS codecat,
        'ND'   AS codetype,
        m.patid
    FROM ptsmasterlist m
    JOIN cfg_mfu a
      ON a.cohortgrp = m.cohortgrp
     AND a.codecat   = 'RX'
    JOIN cdm_dispensing r
      ON r.patid = m.patid
     AND r.adate BETWEEN m.indexdt + a.mfufrom AND m.indexdt + a.mfuto
),
counted AS (
    SELECT
        cohortgrp,
        analysisnum,
        code,
        codecat,
        codetype,
        count(*)              AS codecount,
        count(DISTINCT patid) AS patcount
    FROM claims
    GROUP BY 1, 2, 3, 4, 5
)
SELECT
    c.cohortgrp,
    c.analysisnum,
    c.code,
    c.codecat,
    c.codetype,
    c.codecount,
    c.patcount,
    row_number() OVER (
        PARTITION BY c.cohortgrp, c.analysisnum
        -- Rank by whichever measure the analysis asked for. The
        -- secondary keys are a tie-break: without them two codes with
        -- equal counts could swap between runs, and the top-N cut would
        -- not be reproducible.
        ORDER BY CASE WHEN a.countmethod = 'PATCOUNT' THEN c.patcount
                      ELSE c.codecount END DESC,
                 c.codecount DESC, c.code
    ) AS rank
FROM counted c
JOIN (SELECT DISTINCT cohortgrp, analysisnum, countmethod, topxx
      FROM cfg_mfu) a
  ON a.cohortgrp   = c.cohortgrp
 AND a.analysisnum = c.analysisnum
QUALIFY rank <= a.topxx
ORDER BY cohortgrp, analysisnum, rank;
