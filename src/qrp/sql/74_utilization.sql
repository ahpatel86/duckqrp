-- =====================================================================
-- 74_utilization.sql — healthcare utilization (ms_computeutilization).
--
-- Two families of covariate, both counted in a window anchored on the
-- index date:
--
--   MEDICAL  encounter counts per care setting. SAS's default list is
--            `AV OA IP IS ED` (ms_computeutilization.sas:63), and the
--            output is one column per setting plus a total.
--
--   DRUG     `count(rx)`, `count(distinct generic)` and
--            `count(distinct classname)` (line 415-418) — so a patient
--            filling the same drug ten times counts ten dispensings but
--            one generic.
--
-- The distinct counts are the point: number of dispensings measures
-- intensity, number of distinct generics or classes measures breadth of
-- treatment, and they answer different questions about a patient.
--
-- Window bounds come from the config (`utilfrom`/`utilto`, day offsets
-- from the index date), so a study can ask for utilization over any
-- lookback or follow-up period.
--
-- Encounters that carry no `enctype` are counted in the total but in no
-- setting column, which is what SAS's per-setting SUM produces.
-- =====================================================================

-- Split into separate passes rather than one statement of CTEs. Even
-- as CTEs these are one query plan, so the concurrent hash tables all
-- count toward peak memory — the same reason POV1 and the denominator
-- stage are split. Materialised, and exempt from the single-consumer
-- rule for that stated reason.
CREATE OR REPLACE TEMP TABLE _util_med AS
WITH enc AS (
    SELECT DISTINCT CAST(patid AS BIGINT) AS patid, CAST(adate AS DATE) AS adate,
           upper(trim(CAST(enctype AS VARCHAR))) AS enctype
    FROM {read_encounter}
),
-- Visits come from the ENCOUNTER table, as in SAS; only a study with no
-- encounter table falls back to diagnosis claims (which miss encounters
-- that carry no diagnosis).
visits AS (
    SELECT patid, adate, enctype FROM enc
    UNION ALL
    SELECT DISTINCT patid, adate, enctype FROM cdm_diagnosis
    WHERE NOT EXISTS (SELECT 1 FROM enc)
),
med AS (
    -- One row per (episode, care setting) with the encounter count.
    -- Distinct on (patid, adate, enctype): SAS counts encounters, and
    -- several claims can share one encounter.
    SELECT
        m.cohortgrp,
        m.patid,
        m.indexdt,
        x.enctype,
        x.adate
    FROM ptsmasterlist m
    JOIN cfg_utilization u
      ON u.cohortgrp = m.cohortgrp
     AND u.utiltype  = 'MED'
     -- only cohorts SAS builds a baseline for (createbaseline = 'Y');
     -- elsewhere SAS reports zero. Computing it for every cohort
     -- doubled each utilization total on wp307.
     AND u.cohortgrp IN (SELECT cohortgrp FROM cfg_cohort WHERE create_baseline)
    JOIN visits x
      ON x.patid = m.patid
     AND x.adate BETWEEN m.indexdt + u.utilfrom AND m.indexdt + u.utilto
)
SELECT
        cohortgrp, patid, indexdt,
        count(*) FILTER (WHERE enctype = 'AV')          AS enc_av,
        count(*) FILTER (WHERE enctype = 'OA')          AS enc_oa,
        count(*) FILTER (WHERE enctype = 'IP')          AS enc_ip,
        count(*) FILTER (WHERE enctype = 'IS')          AS enc_is,
        count(*) FILTER (WHERE enctype = 'ED')          AS enc_ed,
        count(*)                                        AS enc_total,
        -- SAS's ExactNumVisit: distinct visit DAYS across all encounter
        -- types (a day with an AV and an OA visit counts once). Matches
        -- SAS on all 15,732 wp307 baseline episodes.
        count(DISTINCT adate)                           AS enc_days
FROM med
GROUP BY 1, 2, 3;

CREATE OR REPLACE TEMP TABLE _util_drug AS
SELECT
        m.cohortgrp,
        m.patid,
        m.indexdt,
        -- As SAS (ms_computeutilization.sas:396-421): dispensings in the
        -- window are INNER-joined to the drug class file on NDC; numrx
        -- counts the joined rows, NumGeneric distinct generic names,
        -- NumClass distinct class names. A dispensing whose NDC is not in
        -- the file is not counted. This counted distinct NDCs as generics.
        count(*)                     AS numrx,
        count(DISTINCT g.generic)    AS numgeneric,
        count(DISTINCT g.classname)  AS numclass
    FROM ptsmasterlist m
    JOIN cfg_utilization u
      ON u.cohortgrp = m.cohortgrp
     AND u.utiltype  = 'DRUG'
     -- only cohorts SAS builds a baseline for (createbaseline = 'Y');
     -- elsewhere SAS reports zero. Computing it for every cohort
     -- doubled each utilization total on wp307.
     AND u.cohortgrp IN (SELECT cohortgrp FROM cfg_cohort WHERE create_baseline)
    JOIN cdm_dispensing d
      ON d.patid = m.patid
     AND d.adate BETWEEN m.indexdt + u.utilfrom AND m.indexdt + u.utilto
    JOIN (SELECT DISTINCT code, generic, classname FROM cfg_drugclass) g
      ON g.code = d.code
    GROUP BY 1, 2, 3;

-- LEFT from the master list: an episode with no encounters or no
-- dispensings in the window scores zero, not absent.
CREATE OR REPLACE TABLE utilization AS
SELECT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    coalesce(w.enc_av, 0)     AS enc_av,
    coalesce(w.enc_oa, 0)     AS enc_oa,
    coalesce(w.enc_ip, 0)     AS enc_ip,
    coalesce(w.enc_is, 0)     AS enc_is,
    coalesce(w.enc_ed, 0)     AS enc_ed,
    coalesce(w.enc_total, 0)  AS enc_total,
    coalesce(w.enc_days, 0)   AS enc_days,
    coalesce(r.numrx, 0)      AS numrx,
    coalesce(r.numgeneric, 0) AS numgeneric,
    coalesce(r.numclass, 0)   AS numclass,
    -- SAS computes utilization only for createbaseline cohorts and leaves
    -- it NULL elsewhere; the zeros here are for the summary, the master
    -- list reports NULL where this is false
    (m.cohortgrp IN (SELECT cohortgrp FROM cfg_cohort WHERE create_baseline))
                              AS util_computed
FROM ptsmasterlist m
LEFT JOIN _util_med w
       ON w.cohortgrp = m.cohortgrp AND w.patid = m.patid
      AND w.indexdt = m.indexdt
LEFT JOIN _util_drug r
       ON r.cohortgrp = m.cohortgrp AND r.patid = m.patid
      AND r.indexdt = m.indexdt;

DROP TABLE _util_med;
DROP TABLE _util_drug;

-- Distribution for the output tables.
CREATE OR REPLACE TABLE utilization_summary AS
SELECT
    cohortgrp,
    count(*)                     AS episodes,
    round(avg(enc_total), 2)     AS mean_encounters,
    round(avg(enc_ip), 3)        AS mean_inpatient,
    round(avg(enc_ed), 3)        AS mean_ed,
    round(avg(numrx), 2)         AS mean_dispensings,
    round(avg(numgeneric), 2)    AS mean_generics,
    round(avg(numclass), 2)      AS mean_classes
FROM utilization
GROUP BY 1
ORDER BY 1;
