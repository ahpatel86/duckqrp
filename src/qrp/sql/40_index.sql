-- =====================================================================
-- 40_index.sql — %ms_findgap: potential index dates meeting washout.
--
-- The rewrite that matters
-- ------------------------
-- SAS applies the washout twice: once against the immediately prior
-- claim (a LAG), then again against ALL claims via a range self-join
-- with %ms_periodsoverlap. The PySpark port reproduced both, so it pays
-- for a full range self-join of the claim set against itself — the most
-- expensive operation in the index stage, and it fans out before it
-- filters.
--
-- Observe what the second check actually asks. A candidate index date
-- `a` is rejected when some other claim's supply covers any day in
-- [a - washout, a - 1]. Claims starting on or after `a` cannot satisfy
-- `inc.adate <= a - 1`, so only PRIOR claims can ever reject it. Among
-- prior claims the condition reduces to `expiredt >= a - washout`, and
-- that holds for some prior claim exactly when it holds for the one with
-- the LATEST expiredt.
--
-- So the entire self-join is equivalent to a single running maximum:
--
--     keep iff prior_max_expiredt IS NULL          (first ever claim)
--           OR prior_max_expiredt < adate - washout
--
-- One ordered window over one sort, replacing an O(n²)-in-the-worst-case
-- range join. It also subsumes the LAG check, since the running max is
-- always >= the immediately prior expiredt, making that check redundant.
-- =====================================================================

CREATE OR REPLACE TABLE index_candidates AS
WITH deduped AS (
    -- SAS: proc sort nodupkey by patid adate descending expiredt.
    -- QUALIFY expresses "keep the longest supply per patient-day".
    SELECT *
    FROM stockpiled
    -- Total order: expiredt DESC is the SAS rule, but the remaining
    -- columns break residual ties deterministically so the same input
    -- always yields the same row.
    QUALIFY row_number() OVER (
        PARTITION BY cohortgrp, patid, adate
        ORDER BY expiredt DESC, rxsup DESC, rxamt DESC, orig_adate
    ) = 1
),
with_history AS (
    SELECT
        d.*,
        c.wash_per,
        max(d.expiredt) OVER (
            PARTITION BY d.cohortgrp, d.patid
            ORDER BY d.adate
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_max_expiredt
    FROM deduped d
    JOIN cfg_cohort c USING (cohortgrp)
)
SELECT
    cohortgrp,
    patid,
    adate,
    expiredt,
    orig_adate,
    rxsup,
    rxamt,
    numdispensing
FROM with_history
WHERE
    -- wash_per IS NULL: only the very first claim per patient qualifies
    CASE
        WHEN wash_per IS NULL THEN prior_max_expiredt IS NULL
        WHEN wash_per = 0     THEN TRUE
        ELSE prior_max_expiredt IS NULL
             OR prior_max_expiredt < adate - wash_per
    END;
