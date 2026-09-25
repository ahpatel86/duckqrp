-- =====================================================================
-- 30_exposure.sql — %ms_extractmeds + %ms_stockpiling.
--
-- Two things worth reading closely:
--
-- 1. Code matching is a semi-join against a config table, not a giant
--    generated IN-list. This keeps the SQL static across studies and
--    lets DuckDB build one hash table over the code set.
--
-- 2. Stockpiling is solved in CLOSED FORM with window functions.
--    The SAS algorithm is sequential: push each overlapping dispensing
--    forward so supplies never overlap —
--        s(1) = a(1);  e(i) = s(i) + r(i) - 1;  s(i) = max(a(i), e(i-1)+1)
--    The PySpark port implemented the classification step with a Python
--    UDF (`F.udf(...)`), which forces a JVM->Python round trip per row
--    and blocks all predicate pushdown. It is the single most expensive
--    construct in that file.
--
--    STOCKGROUP: SAS runs `by PatId &GROUPING.` where the caller passes
--    GROUPING=StockGroup ... (ms_stockpiling.sas:399), so two drugs in
--    one cohort are pushed forward INDEPENDENTLY. Partitioning by
--    (cohortgrp, patid) alone merged them and pushed dates further than
--    SAS. Codes with no stockgroup share '_default', which reproduces
--    the single-drug case exactly.
--
--    That recurrence has an exact closed form. With C(i) the running sum
--    of supply,
--        e(i) = C(i) - 1 + max over j<=i of ( a(j) - C(j-1) )
--    which is a cumulative sum and a running max — two ordered window
--    functions over one sort, fully vectorised, no UDF, no recursion.
--    Proof by induction on the recurrence above.
-- =====================================================================

-- Dispensings matching a cohort's exposure definition.
-- One row per (cohort, dispensing).
-- Exposure is extracted from the domain each code names, not from
-- dispensing alone. A real study defines exposure across RX, PX and DX
-- simultaneously — 960 / 150 / 14 codes in the file seen — and two of
-- its cohorts are defined purely by HCPCS procedure codes. Extracting
-- only from dispensing left those cohorts EMPTY, with no error.
--
-- PX and DX claims carry no supply: rxsup is 1 (a point event, so the
-- episode is one day) and rxamt is NULL. That is what SAS gets too,
-- since those columns do not exist on those tables.
-- Exposure extraction.
 -- codetype is the CODE SYSTEM, and it must MATCH, not merely be
 -- carried along. One cohort's codes span several systems (DX/10,
 -- PX/10, PX/HC, PX/ND, RX/ND in the real study), and the same code
 -- string can exist in two of them. Matching on (code, codecat) alone
 -- found 1,171 members where SAS found 1,113.
 --
 -- An empty codetype means the input file did not say, and matches
 -- anything — so a file without the column behaves as before.
CREATE OR REPLACE TABLE exposure_claims AS
SELECT
    k.cohortgrp,
    k.stockgroup,
    d.patid,
    d.adate,
    d.code,
    -- CODESUPPLY overrides the claim's own RxSup when the study sets
    -- it (SAS's CODESUPPLY handling (exact line unverified)). It was parsed, validated against
    -- the CFDD limits, and never applied.
    COALESCE(k.code_supply, d.rxsup) AS rxsup,
    d.rxamt
FROM cdm_dispensing d
JOIN cfg_codes k
  ON k.code    = d.code
 AND k.role    = 'DEF'
 AND k.codecat = 'RX'
 AND (k.codetype = '' OR k.codetype IS NULL
      -- A NULL SOURCE codetype cannot contradict the configured one.
      -- SCDM extracts may omit the column entirely, in which case the
      -- normalised view supplies NULL; rejecting those rows silently
      -- removed EVERY claim for a study that names a vocabulary.
      OR d.codetype IS NULL
      OR upper(d.codetype) = k.codetype)
WHERE d.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'

UNION ALL

SELECT
    k.cohortgrp, k.stockgroup, x.patid, x.adate, x.code,
    -- A procedure has no days-supply of its own, which is exactly why
    -- CODESUPPLY exists: all 150 PX exposure codes in the real study
    -- file carry it. The hardcoded 1 was right only because every one
    -- of them happens to be 1 — a study specifying 30 got 1-day
    -- episodes.
    COALESCE(k.code_supply, 1) AS rxsup,
    NULL::DOUBLE     AS rxamt
FROM cdm_procedure x
JOIN cfg_codes k
  ON k.code    = x.code
 AND k.role    = 'DEF'
 AND k.codecat = 'PX'
 AND (k.codetype = '' OR k.codetype IS NULL
      -- A NULL SOURCE codetype cannot contradict the configured one.
      -- SCDM extracts may omit the column entirely, in which case the
      -- normalised view supplies NULL; rejecting those rows silently
      -- removed EVERY claim for a study that names a vocabulary.
      OR x.codetype IS NULL
      OR upper(x.codetype) = k.codetype)
WHERE x.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'

UNION ALL

SELECT
    k.cohortgrp, k.stockgroup, x.patid, x.adate, x.code,
    COALESCE(k.code_supply, 1) AS rxsup,
    NULL::DOUBLE     AS rxamt
FROM cdm_diagnosis x
JOIN cfg_codes k
  ON k.code    = x.code
 AND k.role    = 'DEF'
 AND k.codecat = 'DX'
 AND (k.codetype = '' OR k.codetype IS NULL
      -- A NULL SOURCE codetype cannot contradict the configured one.
      -- SCDM extracts may omit the column entirely, in which case the
      -- normalised view supplies NULL; rejecting those rows silently
      -- removed EVERY claim for a study that names a vocabulary.
      OR x.codetype IS NULL
      OR upper(x.codetype) = k.codetype)
WHERE x.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}';

-- Closed-form stockpiling.
--
-- The same-day collapse (SAS `sameday` = 'aa': sum supply, sum amount)
-- is a CTE, not a materialised temp table. It has exactly one consumer,
-- so materialising it was a barrier that cost a full 5.5M-row write and
-- read for nothing — the same "materialise on fan-out only" rule this
-- package applies everywhere else, violated here.
CREATE OR REPLACE TABLE stockpiled AS
WITH sameday AS (
    SELECT
        cohortgrp,
        stockgroup,
        patid,
        adate,
        sum(rxsup)::INTEGER AS rxsup,
        sum(rxamt)          AS rxamt,
        count(*)::INTEGER   AS numdispensing
    FROM exposure_claims
    GROUP BY 1, 2, 3, 4
),
running AS (
    SELECT
        cohortgrp,
        stockgroup,
        patid,
        adate,
        rxsup,
        rxamt,
        numdispensing,
        -- C(i): cumulative supply through this dispensing
        sum(rxsup) OVER w AS cum_sup,
        -- a(j) - C(j-1), expressed in day numbers so it stays integer
        day_num(adate) - (sum(rxsup) OVER w - rxsup) AS anchor
    FROM sameday
    -- ORDER BY adate alone is a TOTAL order here: the sameday CTE has
    -- already collapsed to one row per (cohortgrp, patid, adate), so no
    -- tie is possible. Stated explicitly because the guarantee comes
    -- from the previous CTE rather than from this clause.
    WINDOW w AS (
        PARTITION BY cohortgrp, stockgroup, patid
        ORDER BY adate
        ROWS UNBOUNDED PRECEDING
    )
),
solved AS (
    SELECT
        *,
        max(anchor) OVER (
            PARTITION BY cohortgrp, stockgroup, patid
            ORDER BY adate
            ROWS UNBOUNDED PRECEDING
        ) AS running_anchor
    FROM running
)
SELECT
    cohortgrp,
    stockgroup,
    patid,
    -- e(i) = C(i) - 1 + running_max(anchor)
    -- s(i) = e(i) - r(i) + 1
    from_day_num(cum_sup - 1 + running_anchor - rxsup + 1) AS adate,
    from_day_num(cum_sup - 1 + running_anchor)                          AS expiredt,
    adate       AS orig_adate,
    rxsup,
    rxamt,
    numdispensing
FROM solved;

-- Follow-up event claims (the outcome definition), extracted once for
-- all cohorts.
--
-- EVENTCOUNT controls how same-day events are collapsed
-- (ms_createpov56.sas:130-139):
--
--   0  no deduplication — every qualifying claim counts
--   1  nodupkey by (PatId, Adate, codecat, codetype, code)
--        one per distinct CODE per day
--   2  nodupkey by (PatId, Adate)
--        one per day, whatever the code
--
-- This previously applied `SELECT DISTINCT (cohortgrp, patid, adate)`
-- unconditionally, which is eventcount=2 hardcoded. It went unnoticed
-- because the only consumer took `min(adate)`, which is invariant under
-- all three — until `numevents` was added, at which point the setting
-- started changing the answer.
--
-- CARE SETTING: a code may restrict which encounter types and principal
-- diagnosis flags count (ms_caresettingprincipal.sas). SAS matches with
--   (EncType = '**' OR EncType = claim.enctype)
--   AND (Pdx  = '*'  OR Pdx     = claim.pdx)
-- An unrestricted code expands to ('**','*'), so the join is uniform.
CREATE OR REPLACE TABLE event_claims AS
SELECT
    cohortgrp,
    patid,
    adate,
    code,
    codetype
FROM (
    SELECT
        k.cohortgrp,
        x.patid,
        x.adate,
        x.code,
        -- codetype is part of SAS's eventcount=1 key
        -- (PatId, Adate, codecat, codetype, code). Dropping it
        -- collapsed an ICD-9 and an ICD-10 claim carrying the same code
        -- on the same day into one event. There are 26 such pairs in
        -- the real extract, so this is not hypothetical. Reported in
        -- review.
        x.codetype,
        c.event_count,
        row_number() OVER (
            PARTITION BY k.cohortgrp, x.patid, x.adate,
                         -- key widens with eventcount: by code for 1,
                         -- by nothing more for 2, and 0 never dedups.
                         CASE WHEN c.event_count = 2 THEN NULL
                              ELSE x.code END,
                         CASE WHEN c.event_count = 2 THEN NULL
                              ELSE x.codetype END,
                         -- codecat too: SAS's key is
                         -- (PatId, Adate, codecat, codetype, code).
                         -- Events are now drawn from DX, PX and RX, so
                         -- a same-day diagnosis and procedure sharing a
                         -- code and vocabulary collapsed into ONE
                         -- event instead of two.
                         CASE WHEN c.event_count = 2 THEN NULL
                              ELSE x.codecat END
            ORDER BY x.code
        ) AS rn
    -- Events come from the code's OWN domain, not from diagnosis alone.
    -- SAS sets _FUPEvent from _ITDrugs (RX), _ITMeds (DX and PX),
    -- _ITLabs, _itenc and _itDth (ms_cidanum.sas:1663-1672), so an
    -- outcome defined by a dispensing or a procedure is legitimate.
    -- Reading cdm_diagnosis only meant such an outcome NEVER fired —
    -- the same defect the exposure extraction had. Reported in review.
    FROM (
        SELECT patid, adate, code, codetype, enctype,
               COALESCE(pdx, '') AS pdx, 'DX' AS codecat
        FROM cdm_diagnosis
        UNION ALL
        SELECT patid, adate, code, codetype, enctype, '' AS pdx,
               'PX' AS codecat
        FROM cdm_procedure
        UNION ALL
        SELECT patid, adate, code, NULL AS codetype, '**' AS enctype,
               '' AS pdx, 'RX' AS codecat
        FROM cdm_dispensing
    ) x
    JOIN cfg_codes k
      ON k.code    = x.code
     AND k.role    = 'EVENT'
     AND k.codecat = x.codecat
    JOIN cfg_cohort c
      ON c.cohortgrp = k.cohortgrp
    JOIN cfg_care_setting cs
      ON cs.cohortgrp = k.cohortgrp
     AND cs.code      = k.code
     AND (cs.enctype = '**' OR cs.enctype = x.enctype)
     AND (cs.pdx     = '*'  OR cs.pdx     = COALESCE(x.pdx, ''))
    WHERE x.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
)
WHERE event_count = 0 OR rn = 1;


-- ---------------------------------------------------------------
-- FUT (truncation) claims, STOCKPILED.
--
-- FUT codes carry a `stockgroup` exactly as exposure codes do, so
-- their claims stockpile the same way: a dispensing that arrives while
-- the previous one in its stockgroup is still supplying starts when
-- that supply runs out, not on its own fill date.
--
-- That shift IS the truncation date. Worked case: a FUT claim on
-- 2016-06-06 in stockgroup `valsartanhydrochlorothiazide`, with the
-- previous claim in that group (2016-03-18, 90 days) supplying until
-- 2016-06-15, stockpiles to 2016-06-16 — which is precisely the
-- `trunkdt` SAS used, and ten days later than the raw claim date.
--
-- Using raw claim dates truncated 854 episodes too early, by 1 to 17
-- days each: the leftover supply of the preceding claim.
-- ---------------------------------------------------------------
CREATE OR REPLACE TABLE trunc_claims AS
WITH raw AS (
    -- DISPENSINGS ONLY. SAS stockpiles `_ITDrugs`
    -- (ms_cidanum.sas:1545) — diagnosis and procedure claims never
    -- pass through it. Stockpiling them here, with a notional one-day
    -- supply, chained same-day codes into long artificial runs and
    -- pushed truncation dates years past the claim.
    SELECT k.cohortgrp, k.stockgroup, t.patid, t.adate, t.rxsup
    FROM (
        SELECT patid, adate, code, 'RX' AS codecat, codetype, rxsup
          FROM cdm_dispensing
         -- Same extraction window as the exposure claims. Without it
         -- the stockpile chain starts from the beginning of time and
         -- accumulates drift SAS never has: SAS builds `_ITDrugs` from
         -- the extracted claims, not the whole table.
         WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
    ) t
    JOIN cfg_codes k
      ON k.role = 'TRUNK' AND k.code = t.code AND k.codecat = t.codecat
     AND (k.codetype = '' OR k.codetype IS NULL
          OR t.codetype IS NULL
          OR upper(t.codetype) = k.codetype)
),
sameday AS (
    SELECT cohortgrp, stockgroup, patid, adate,
           sum(rxsup)::INTEGER AS rxsup
    FROM raw GROUP BY 1, 2, 3, 4
),
running AS (
    SELECT *, sum(rxsup) OVER w AS cum_sup,
           day_num(adate) - (sum(rxsup) OVER w - rxsup) AS anchor
    FROM sameday
    WINDOW w AS (PARTITION BY cohortgrp, stockgroup, patid
                 ORDER BY adate ROWS UNBOUNDED PRECEDING)
),
solved AS (
    SELECT *, max(anchor) OVER (
                 PARTITION BY cohortgrp, stockgroup, patid
                 ORDER BY adate ROWS UNBOUNDED PRECEDING) AS running_anchor
    FROM running
)
SELECT cohortgrp, patid,
       from_day_num(cum_sup - 1 + running_anchor - rxsup + 1) AS adate,
       adate AS orig_adate
FROM solved

UNION ALL

-- Non-drug truncation claims keep their own dates.
SELECT DISTINCT k.cohortgrp, t.patid, t.adate, t.adate AS orig_adate
FROM (
    SELECT patid, adate, code, 'DX' AS codecat, codetype FROM cdm_diagnosis
     WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
    UNION ALL
    SELECT patid, adate, code, 'PX', codetype FROM cdm_procedure
     WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
) t
JOIN cfg_codes k
  ON k.role = 'TRUNK' AND k.code = t.code AND k.codecat = t.codecat
 AND (k.codetype = '' OR k.codetype IS NULL
      OR upper(t.codetype) = k.codetype);
