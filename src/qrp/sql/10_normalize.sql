-- =====================================================================
-- 10_normalize.sql — the schema contract.
--
-- Everything downstream may assume: lowercase column names, DATE-typed
-- dates, no duplicate patients in demographics. That assumption is
-- established exactly here and nowhere else.
--
-- This single file replaces the 306 `x in df.columns` guards, the ~130
-- lowercase-map rebuilds, and the runtime dtype branch in ms_loopenc
-- that the PySpark port needed because no such contract existed.
--
-- The read patterns are resolved in Python (qrp.scdm.resolve_table) and
-- substituted here, so the SQL does not dictate a directory layout. A
-- site may use one folder per table, one file per table, several files
-- per table, or CSV — see docs/RUNBOOK.md. Column projection and filters
-- are still pushed into the scan, so unused columns are never read.
-- =====================================================================

CREATE OR REPLACE VIEW cdm_enrollment AS
SELECT
    CAST(patid AS BIGINT)      AS patid,
    CAST(enr_start AS DATE)    AS enr_start,
    CAST(enr_end   AS DATE)    AS enr_end,
    upper(CAST(medcov  AS VARCHAR)) AS medcov,
    upper(CAST(drugcov AS VARCHAR)) AS drugcov,
    upper(COALESCE(CAST({enrollment_chart} AS VARCHAR), 'N')) AS chart
FROM {read_enrollment}
WHERE enr_start IS NOT NULL
  AND enr_end   IS NOT NULL
  AND enr_end >= enr_start;

-- One demographic row per patient, plus the two exception sets the
-- pipeline needs. Computed once for the whole run rather than per cohort.
CREATE OR REPLACE TABLE demographics AS
SELECT
    CAST(patid AS BIGINT)   AS patid,
    CAST(birth_date AS DATE) AS birth_date,
    -- SAS collapses anything outside F/M to 'O' for the sex covariate
    -- but keeps the raw value for the eligibility filter.
    upper(CAST(sex AS VARCHAR))      AS sex_raw,
    CASE WHEN upper(CAST(sex AS VARCHAR)) IN ('F','M')
         THEN upper(CAST(sex AS VARCHAR)) ELSE 'O' END AS sex,
    upper(COALESCE(CAST({opt_race} AS VARCHAR), 'M'))     AS race,
    upper(COALESCE(CAST({opt_hispanic} AS VARCHAR), 'U')) AS hispanic,
    CAST({opt_postalcode} AS VARCHAR)      AS zip,
    CAST({opt_postalcode_date} AS DATE)    AS zip_date
FROM {read_demographic}
-- The ORDER BY is a TOTAL order, not just `birth_date`. Two demographic
-- rows sharing a birth date would otherwise be resolved arbitrarily and
-- the run would not be reproducible. Any tie-break that is not a total
-- order is a latent parity failure; see tests/test_determinism.py.
QUALIFY row_number() OVER (
    PARTITION BY patid
    -- the tie-break must use the RESOLVED optional columns too,
    -- or an extract lacking them fails here instead
    ORDER BY birth_date, sex, {opt_race}, {opt_hispanic},
             {opt_postalcode}
) = 1;
-- ^ QUALIFY: the whole row_number()-then-filter-then-drop dance from the
--   PySpark port collapses to one clause. Used throughout this package.

CREATE OR REPLACE TABLE demographics_multi AS
SELECT CAST(patid AS BIGINT) AS patid
FROM {read_demographic}
GROUP BY 1 HAVING count(*) > 1;

CREATE OR REPLACE TABLE demographics_missing AS
SELECT DISTINCT CAST(patid AS BIGINT) AS patid
FROM {read_demographic}
WHERE birth_date IS NULL OR sex IS NULL;

-- The dispensed-code column is `rx` in SCDM. An earlier version of this
-- file read `ndc`, which is the code VOCABULARY, not the column name —
-- so real dispensing data failed with "column ndc not found". The
-- COLUMNS(...) form accepts either, since some extracts use `ndc`.
CREATE OR REPLACE VIEW cdm_dispensing AS
SELECT
    CAST(patid AS BIGINT)          AS patid,
    CAST(rxdate AS DATE)           AS adate,
    CAST({dispensing_code} AS VARCHAR) AS code,
    CAST({dispensing_codetype} AS VARCHAR) AS codetype,
    CAST(rxsup AS INTEGER)         AS rxsup,
    CAST(rxamt AS DOUBLE)          AS rxamt
FROM {read_dispensing}
WHERE rxdate IS NOT NULL
  AND rxsup IS NOT NULL AND rxsup > 0
  -- SAS's dispensing extraction keeps `rxsup > 0 and rxamt > 0`
  -- (ms_cidanum.sas:617). This was once applied, measured worse, and
  -- reverted — but the cause was the TEST EXTRACT, whose rxamt had been
  -- written as INTEGER, truncating 54,995 fractional amounts (0.6 mL
  -- syringes) to 0. With real amounts there are no zeros at all and
  -- this filter changes nothing on that study; it stays because it is
  -- SAS's rule and a true zero amount is not a dispensing.
  AND rxamt IS NOT NULL AND rxamt > 0;

-- ------------------------------------------------------------------
-- ENVELOPING (ms_envelope.sas). A diagnosis or procedure claim dated
-- within an inpatient stay, and not itself coded inpatient, is re-filed
-- to the stay: care setting 'IP', principal flag 'X'.
--
-- SCOPE: every diagnosis and procedure claim, through the cdm_diagnosis
-- and cdm_procedure views — as SAS envelopes its whole claim extraction
-- (combo.sas, and ms_cidanum.sas's claim set). With it, every wp307
-- comparison is exact.
--
-- CORRECTION: an earlier note here said enveloping every claim broke
-- wp307 cohorts (patients +28, episodes +48). It did not. That run already
-- carried an unrelated regression (dispensings filtered by fill date, not
-- supply), which alone produced exactly those numbers; with it fixed,
-- global enveloping leaves every cohort count exact.
--
-- With RUN_ENVELOPE 0 SAS first merges touching or overlapping IP
-- encounters and envelopes admit..discharge inclusive; merged stays
-- cover exactly the union of the encounters' days, so the union is used
-- directly. Any other value except 2 envelopes from the day AFTER admit,
-- per encounter; 2 switches enveloping off. A missing discharge date is
-- a one-day stay. SAS's mindate cut (stays ending before extraction
-- start) cannot change an extracted claim, so it is not applied.
--
-- wp307: re-filing one ambulatory J2506 claim on a discharge day is what
-- separated SAS's distindexexp from this package's on 10 episodes.
-- ------------------------------------------------------------------
CREATE OR REPLACE TABLE _ip_days AS
WITH ip AS (
    SELECT DISTINCT CAST(patid AS BIGINT) AS patid, CAST(adate AS DATE) AS a,
           greatest(CAST(adate AS DATE),
                    coalesce(CAST({opt_ddate} AS DATE), CAST(adate AS DATE))) AS e
    FROM {read_encounter}
    WHERE upper(trim(CAST(enctype AS VARCHAR))) = 'IP' AND adate IS NOT NULL
),
stays AS (
    SELECT patid,
           CASE WHEN {run_envelope} = 0 THEN a ELSE a + 1 END AS s, e
    FROM ip
    WHERE {run_envelope} <> 2
)
SELECT DISTINCT patid,
       CAST(unnest(generate_series(s, e, INTERVAL 1 DAY)) AS DATE) AS day
FROM stays
WHERE s <= e;

CREATE OR REPLACE VIEW _cdm_diagnosis_raw AS
SELECT
    CAST(patid AS BIGINT)  AS patid,
    CAST(adate AS DATE)    AS adate,
    CAST(dx AS VARCHAR)    AS code,
    CAST(dx_codetype AS VARCHAR) AS codetype,
    upper(COALESCE(CAST(pdx AS VARCHAR), 'X')) AS pdx,
    upper(COALESCE(CAST(enctype AS VARCHAR), 'XX')) AS enctype
FROM {read_diagnosis}
WHERE adate IS NOT NULL;

CREATE OR REPLACE VIEW cdm_diagnosis AS
SELECT r.* REPLACE (
    CASE WHEN i.patid IS NOT NULL AND r.enctype <> 'IP' THEN 'X'  ELSE r.pdx END AS pdx,
    CASE WHEN i.patid IS NOT NULL AND r.enctype <> 'IP' THEN 'IP' ELSE r.enctype END AS enctype)
FROM _cdm_diagnosis_raw r
LEFT JOIN _ip_days i ON i.patid = r.patid AND i.day = r.adate;

CREATE OR REPLACE TABLE deaths AS
SELECT
    CAST(patid AS BIGINT) AS patid,
    min(CAST(deathdt AS DATE)) AS deathdt
FROM {read_death}
WHERE deathdt IS NOT NULL
GROUP BY 1;

-- Procedures. Real input files use codecat='PX' freely — 150 of 1,124
-- cohort codes, 80 covariate codes and 30 inclusion codes in the study
-- file seen — so this is a mainstream domain, not an edge case.
CREATE OR REPLACE VIEW _cdm_procedure_raw AS
SELECT
    CAST(patid AS BIGINT)          AS patid,
    CAST(adate AS DATE)            AS adate,
    CAST(px AS VARCHAR)            AS code,
    CAST(px_codetype AS VARCHAR)   AS codetype,
    CAST(enctype AS VARCHAR)       AS enctype
FROM {read_procedure};

-- enveloped like diagnoses; the procedure table has no principal flag,
-- so `pdx` is blank, or 'X' when the claim was enveloped
CREATE OR REPLACE VIEW cdm_procedure AS
SELECT r.* REPLACE (
    CASE WHEN i.patid IS NOT NULL AND coalesce(r.enctype, '') <> 'IP' THEN 'IP'
         ELSE r.enctype END AS enctype),
    CASE WHEN i.patid IS NOT NULL AND coalesce(r.enctype, '') <> 'IP' THEN 'X'
         ELSE '' END AS pdx
FROM _cdm_procedure_raw r
LEFT JOIN _ip_days i ON i.patid = r.patid AND i.day = r.adate;

-- Laboratory results.
--
-- Schema verified against a real SCDM lab extract (1.4M rows, 15k
-- patients). Several columns differ from what a naive reading suggests:
--
--   * there is NO `lab_code` column. LAB01 matches a seven-attribute
--     COMBINATION (test name, sub category, specimen source, result
--     unit, result type, fasting indicator, patient location) that a
--     lookup file maps a code onto — see 76_labs.sql.
--   * the numeric result is `ms_result_n`, the character result
--     `ms_result_c` — not `result_num`.
--   * `order_dt` and `result_dt` are SAS numeric dates stored as
--     DOUBLE, and in the extract seen they are entirely NULL. Only
--     `lab_dt` is a real DATE. The LABDATETYPE priority must therefore
--     fall through, which is exactly what it is for.
--   * `result_type` is dominated by 'U' (unknown) — 77% of rows in the
--     real extract, against 23% 'N' and 0.07% 'C'.
CREATE OR REPLACE VIEW cdm_lab AS
SELECT
    CAST(patid AS BIGINT)                 AS patid,
    CAST(lab_dt AS DATE)                  AS lab_dt,
    -- SAS numeric dates: days since 1960-01-01, stored as DOUBLE.
    CASE WHEN result_dt IS NULL THEN NULL
         ELSE DATE '1960-01-01' + CAST(result_dt AS INTEGER) END AS result_dt,
    CASE WHEN order_dt IS NULL THEN NULL
         ELSE DATE '1960-01-01' + CAST(order_dt AS INTEGER) END  AS order_dt,
    CAST(ms_result_n AS DOUBLE)           AS result_num,
    upper(trim(COALESCE(CAST(ms_result_c AS VARCHAR), ''))) AS result_char,
    upper(trim(COALESCE(CAST(result_type AS VARCHAR), 'U'))) AS result_type,
    CAST(loinc AS VARCHAR)                AS loinc,
    CAST(px AS VARCHAR)                   AS px,
    -- the LAB01 combination, upcased and trimmed as SAS does
    -- (ms_extractlabs.sas:227-233)
    upper(trim(COALESCE(CAST(ms_test_name AS VARCHAR), '')))         AS ms_test_name,
    upper(trim(COALESCE(CAST(ms_test_sub_category AS VARCHAR), ''))) AS ms_test_sub_category,
    upper(trim(COALESCE(CAST(specimen_source AS VARCHAR), '')))      AS specimen_source,
    upper(trim(COALESCE(CAST(ms_result_unit AS VARCHAR), '')))       AS ms_result_unit,
    upper(trim(COALESCE(CAST(fast_ind AS VARCHAR), '')))             AS fast_ind,
    upper(trim(COALESCE(CAST(pt_loc AS VARCHAR), '')))               AS pt_loc
FROM {read_lab_result};
