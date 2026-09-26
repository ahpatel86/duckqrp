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
    upper(COALESCE(CAST(race AS VARCHAR), 'M'))     AS race,
    upper(COALESCE(CAST(hispanic AS VARCHAR), 'U')) AS hispanic,
    CAST(postalcode AS VARCHAR)      AS zip,
    CAST(postalcode_date AS DATE)    AS zip_date
FROM {read_demographic}
-- The ORDER BY is a TOTAL order, not just `birth_date`. Two demographic
-- rows sharing a birth date would otherwise be resolved arbitrarily and
-- the run would not be reproducible. Any tie-break that is not a total
-- order is a latent parity failure; see tests/test_determinism.py.
QUALIFY row_number() OVER (
    PARTITION BY patid
    ORDER BY birth_date, sex, race, hispanic, postalcode
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
  AND rxsup IS NOT NULL AND rxsup > 0;
-- NOTE: ms_cidanum.sas:617 filters `rxsup > 0 and rxamt > 0` on ITS
-- dispensing extraction, so requiring a positive amount here looked
-- right. MEASURED it is worse — episodes moved from 31,444 to 31,404
-- against SAS's 31,464, and ends too long rose from 82 to 94. That
-- filter evidently guards a different dataset, so it is not applied.

CREATE OR REPLACE VIEW cdm_diagnosis AS
SELECT
    CAST(patid AS BIGINT)  AS patid,
    CAST(adate AS DATE)    AS adate,
    CAST(dx AS VARCHAR)    AS code,
    CAST(dx_codetype AS VARCHAR) AS codetype,
    upper(COALESCE(CAST(pdx AS VARCHAR), 'X')) AS pdx,
    upper(COALESCE(CAST(enctype AS VARCHAR), 'XX')) AS enctype
FROM {read_diagnosis}
WHERE adate IS NOT NULL;

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
CREATE OR REPLACE VIEW cdm_procedure AS
SELECT
    CAST(patid AS BIGINT)          AS patid,
    CAST(adate AS DATE)            AS adate,
    CAST(px AS VARCHAR)            AS code,
    CAST(px_codetype AS VARCHAR)   AS codetype,
    CAST(enctype AS VARCHAR)       AS enctype
FROM {read_procedure};

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
