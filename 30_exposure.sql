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
CREATE OR REPLACE TABLE exposure_claims AS
SELECT
    k.cohortgrp,
    k.stockgroup,
    d.patid,
    d.adate,
    d.code,
    d.rxsup,
    d.rxamt
FROM cdm_dispensing d
JOIN cfg_codes k
  ON k.code    = d.code
 AND k.role    = 'DEF'
 AND k.codecat = 'RX'
WHERE d.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'

UNION ALL

SELECT
    k.cohortgrp, k.stockgroup, x.patid, x.adate, x.code,
    1                AS rxsup,
    NULL::DOUBLE     AS rxamt
FROM cdm_procedure x
JOIN cfg_codes k
  ON k.code    = x.code
 AND k.role    = 'DEF'
 AND k.codecat = 'PX'
WHERE x.adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'

UNION ALL

SELECT
    k.cohortgrp, k.stockgroup, x.patid, x.adate, x.code,
    1                AS rxsup,
    NULL::DOUBLE     AS rxamt
FROM cdm_diagnosis x
JOIN cfg_codes k
  ON k.code    = x.code
 AND k.role    = 'DEF'
 AND k.codecat = 'DX'
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
    code
FROM (
    SELECT
        k.cohortgrp,
        x.patid,
        x.adate,
        x.code,
        c.event_count,
        row_number() OVER (
            PARTITION BY k.cohortgrp, x.patid, x.adate,
                         -- key widens with eventcount: by code for 1,
                         -- by nothing more for 2, and 0 never dedups.
                         CASE WHEN c.event_count = 2 THEN NULL
                              ELSE x.code END
            ORDER BY x.code
        ) AS rn
    FROM cdm_diagnosis x
    JOIN cfg_codes k
      ON k.code = x.code
     AND k.role = 'EVENT'
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
