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
WITH med AS (
    -- One row per (episode, care setting) with the encounter count.
    -- Distinct on (patid, adate, enctype): SAS counts encounters, and
    -- several claims can share one encounter.
    SELECT
        m.cohortgrp,
        m.patid,
        m.indexdt,
        x.enctype,
        count(*) AS n
    FROM ptsmasterlist m
    JOIN cfg_utilization u
      ON u.cohortgrp = m.cohortgrp
     AND u.utiltype  = 'MED'
    JOIN (SELECT DISTINCT patid, adate, enctype FROM cdm_diagnosis) x
      ON x.patid = m.patid
     AND x.adate BETWEEN m.indexdt + u.utilfrom AND m.indexdt + u.utilto
    GROUP BY 1, 2, 3, 4
)
SELECT
        cohortgrp, patid, indexdt,
        sum(CASE WHEN enctype = 'AV' THEN n ELSE 0 END) AS enc_av,
        sum(CASE WHEN enctype = 'OA' THEN n ELSE 0 END) AS enc_oa,
        sum(CASE WHEN enctype = 'IP' THEN n ELSE 0 END) AS enc_ip,
        sum(CASE WHEN enctype = 'IS' THEN n ELSE 0 END) AS enc_is,
        sum(CASE WHEN enctype = 'ED' THEN n ELSE 0 END) AS enc_ed,
        sum(n)                                          AS enc_total
FROM med
GROUP BY 1, 2, 3;

CREATE OR REPLACE TEMP TABLE _util_drug AS
SELECT
        m.cohortgrp,
        m.patid,
        m.indexdt,
        count(*)                     AS numrx,
        count(DISTINCT d.code)       AS numgeneric,
        count(DISTINCT g.classname)  AS numclass
    FROM ptsmasterlist m
    JOIN cfg_utilization u
      ON u.cohortgrp = m.cohortgrp
     AND u.utiltype  = 'DRUG'
    JOIN cdm_dispensing d
      ON d.patid = m.patid
     AND d.adate BETWEEN m.indexdt + u.utilfrom AND m.indexdt + u.utilto
    LEFT JOIN cfg_drugclass g
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
    coalesce(r.numrx, 0)      AS numrx,
    coalesce(r.numgeneric, 0) AS numgeneric,
    coalesce(r.numclass, 0)   AS numclass
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
