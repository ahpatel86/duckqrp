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
-- PX and DX claims carry no supply of their own: rxsup is CODESUPPLY,
-- or 1 (a point event). rxamt is 1 — SAS sets `RXAmt=1` and
-- `NumDispensing=1` for medical claims (ms_cidanum.sas, the
-- `if b or c or d or g` block). An earlier version used NULL and
-- claimed SAS did too; each J-code administration then contributed
-- nothing to `amtsupp`, and the peg/fil cohorts' amounts ran low on
-- every mismatched episode.
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
    d.rxamt,
    'RX'             AS codecat
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
-- A dispensing counts if its SUPPLY reaches the window, not only its
-- fill date: the exposure chain admits fills whose supply reaches the
-- enrolment window. Filtering on the fill date dropped long fills
-- dated before claims_from; exact parity on wp307 had rested on an
-- accidental 365-day look-back from a risk-score default, and broke
-- (patients +28, episodes +48) when that default was corrected.
WHERE d.adate + CAST(d.rxsup - 1 AS INTEGER) >= DATE '{claims_from}'
  AND d.adate <= DATE '{claims_to}'

UNION ALL

SELECT
    k.cohortgrp, k.stockgroup, x.patid, x.adate, x.code,
    -- A procedure has no days-supply of its own, which is exactly why
    -- CODESUPPLY exists: all 150 PX exposure codes in the real study
    -- file carry it. The hardcoded 1 was right only because every one
    -- of them happens to be 1 — a study specifying 30 got 1-day
    -- episodes.
    COALESCE(k.code_supply, 1) AS rxsup,
    1.0::DOUBLE      AS rxamt,
    'PX'             AS codecat
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
    1.0::DOUBLE      AS rxamt,
    'DX'             AS codecat
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
        x.cohortgrp,
        x.stockgroup,
        x.patid,
        x.adate,
        sum(x.rxsup)::INTEGER AS rxsup,
        sum(x.rxamt)          AS rxamt,
        count(*)::INTEGER     AS numdispensing
    -- DISPENSINGS ONLY. SAS stockpiles `_ITDrugs`
    -- (ms_cidanum.sas:1545); procedure- and diagnosis-sourced exposure
    -- never passes through it. Chaining them pushed each successive
    -- administration a day later and stretched the episode end past
    -- SAS's — visible on a filgrastim patient whose three same-day
    -- J-code administrations became a six-day run.
    FROM exposure_claims x
    JOIN cfg_cohort cc ON cc.cohortgrp = x.cohortgrp
    WHERE x.codecat = 'RX'
      -- A dispensing joins the stockpile chain only if its SUPPLY
      -- still runs at the start of the required prior-enrolment
      -- window — the same rule the truncation chain follows.
      --
      -- Chaining from the beginning of the extract pushed every
      -- expiry forward, which closed gaps that SAS leaves open. One
      -- patient's claims were 35 days apart on their own dates, past
      -- the 30-day `episodegap`, so SAS starts a new episode there;
      -- accumulated push made the gap 5 days here and the episodes
      -- merged, losing the later index date entirely.
      --
      -- This closed the last of the membership gap: episodes go from
      -- 31,440 to 31,464, exactly SAS's count, with no episode in
      -- either output missing from the other.
      AND x.adate + CAST(x.rxsup - 1 AS INTEGER)
          >= DATE '{start_date}' - cc.enr_days
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
FROM solved

UNION ALL

-- Non-dispensing exposure keeps its own dates: one row per claim date,
-- with the supplies of same-day claims summed as SAS does.
SELECT
    cohortgrp,
    stockgroup,
    patid,
    adate,
    -- MAX, not sum: two administrations of the same drug on one day
    -- are one day of exposure, not two. Summing them stretched the
    -- episode a day per repeat and pushed its end past SAS's.
    adate + CAST(max(rxsup) - 1 AS INTEGER) AS expiredt,
    adate                                   AS orig_adate,
    max(rxsup)::INTEGER                     AS rxsup,
    sum(rxamt)                              AS rxamt,
    count(*)::INTEGER                       AS numdispensing
FROM exposure_claims
WHERE codecat <> 'RX'
GROUP BY 1, 2, 3, 4;

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
        -- the dispensing vocabulary is real and must not be discarded:
        -- hardcoding NULL here made every configured RX vocabulary
        -- match any dispensing code string
        SELECT patid, adate, code, codetype, '**' AS enctype,
               '' AS pdx, 'RX' AS codecat
        FROM cdm_dispensing
    ) x
    JOIN cfg_codes k
      ON k.code    = x.code
     AND k.role    = 'EVENT'
     AND k.codecat = x.codecat
     -- Same vocabulary policy as the exposure join. Without it a code
     -- configured as ICD-10 matched the same string recorded as ICD-9,
     -- counting outcomes the study never defined.
     AND (k.codetype = '' OR k.codetype IS NULL
          OR x.codetype IS NULL
          OR upper(x.codetype) = k.codetype)
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
         WHERE adate <= DATE '{claims_to}'
    ) t
    JOIN cfg_codes k
      ON k.role = 'TRUNK' AND k.code = t.code AND k.codecat = t.codecat
     AND (k.codetype = '' OR k.codetype IS NULL
          OR t.codetype IS NULL
          OR upper(t.codetype) = k.codetype)
    JOIN cfg_cohort cc ON cc.cohortgrp = k.cohortgrp
    -- A claim enters the chain only if its SUPPLY still runs at the
    -- start of the required prior-enrolment window. Taking every claim
    -- back to `claims_from` instead added claims SAS never sees, and
    -- each one pushes the rest of the chain further forward.
    WHERE t.adate + CAST(t.rxsup - 1 AS INTEGER)
          >= DATE '{start_date}' - cc.enr_days
      -- Only patients with exposure in this cohort. Truncation can only
      -- cut a patient's own episodes, and the chain runs per patient, so
      -- other patients' claims cannot move these dates. On wp307 96% of
      -- the 2.7M truncation rows came from patients never in the cohort
      -- and were built, stockpiled and never matched.
      AND EXISTS (SELECT 1 FROM exposure_claims x
                  WHERE x.cohortgrp = k.cohortgrp AND x.patid = t.patid)
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
 -- Same missing-vocabulary policy as the drug branch above: a NULL
 -- SOURCE vocabulary cannot contradict the configured one. The two
 -- branches differed, so a diagnosis-sourced truncation code was
 -- dropped by an extract that omits codetype while a drug-sourced one
 -- was kept.
 AND (k.codetype = '' OR k.codetype IS NULL
      OR t.codetype IS NULL
      OR upper(t.codetype) = k.codetype)
-- as above: only patients with exposure in this cohort
WHERE EXISTS (SELECT 1 FROM exposure_claims x
              WHERE x.cohortgrp = k.cohortgrp AND x.patid = t.patid);
