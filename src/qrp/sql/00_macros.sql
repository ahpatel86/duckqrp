-- =====================================================================
-- 00_macros.sql — the shared vocabulary of the pipeline.
--
-- These replace the small SAS utility macros (%ms_periodsoverlap,
-- %ms_agestrat, %isdata ...) and their PySpark helper-function ports.
-- A DuckDB MACRO is inlined by the optimiser, so unlike a Python UDF
-- there is zero per-row cost and the predicate stays pushable.
-- =====================================================================

-- %ms_periodsoverlap. Two closed intervals share at least one day.
CREATE OR REPLACE MACRO periods_overlap(a_start, a_end, b_start, b_end) AS
    a_end >= b_start AND a_start <= b_end;

-- Days in a closed interval, SAS convention (inclusive of both ends).
CREATE OR REPLACE MACRO span_days(d_start, d_end) AS
    date_diff('day', d_start, d_end) + 1;

-- Length of the overlap between two closed intervals, 0 when disjoint.
-- Used by the shave operations to recompute RxSup after trimming.
CREATE OR REPLACE MACRO overlap_days(a_start, a_end, b_start, b_end) AS
    greatest(0, date_diff('day',
        greatest(a_start, b_start),
        least(a_end, b_end)) + 1);

-- Age in whole years on `as_of`, matching SAS INTCK('YEAR', ..., 'C')
-- semantics (birthday-aware, not the /365.25 approximation the PySpark
-- port used — that drifts by a day near boundaries and is the source of
-- the documented age-bucket tolerance drift).
CREATE OR REPLACE MACRO age_years(birth_date, as_of) AS
    date_diff('year', birth_date, as_of)
    - CASE WHEN (month(as_of), day(as_of)) < (month(birth_date), day(birth_date))
           THEN 1 ELSE 0 END;

-- 1 if 29 February of year y falls in (lo, hi], else 0.
CREATE OR REPLACE MACRO leap_day_in(lo, hi, y) AS
    CASE WHEN (y % 4 = 0 AND (y % 100 <> 0 OR y % 400 = 0))
          AND CAST(make_date(y, 3, 1) - INTERVAL 1 DAY AS DATE) > lo
          AND CAST(make_date(y, 3, 1) - INTERVAL 1 DAY AS DATE) <= hi
         THEN 1 ELSE 0 END;

-- Age as SAS reports it: yrdif(birth_date, date, 'AGE'). Whole years,
-- plus the days since the last birthday over 365 — always 365, with any
-- 29 February in between NOT counted. (Dividing by the age year's actual
-- length, 365 or 366, differed from SAS on 8,440 of 31,464 wp307
-- episodes; this matches.) age_years (whole years) still drives age
-- groups and age criteria; this is the reported value only.
CREATE OR REPLACE MACRO age_yrdif(birth_date, as_of) AS
    age_years(birth_date, as_of)
    + (date_diff('day',
                 CAST(birth_date + to_years(age_years(birth_date, as_of)) AS DATE),
                 as_of)
       - leap_day_in(CAST(birth_date + to_years(age_years(birth_date, as_of)) AS DATE),
                     as_of,
                     year(CAST(birth_date + to_years(age_years(birth_date, as_of)) AS DATE)))
       - CASE WHEN year(as_of)
                   <> year(CAST(birth_date + to_years(age_years(birth_date, as_of)) AS DATE))
              THEN leap_day_in(CAST(birth_date + to_years(age_years(birth_date, as_of)) AS DATE),
                               as_of, year(as_of))
              ELSE 0 END
      ) / 365.0;

-- Exact age in a named unit. Age strata are declared as data (see
-- age_strata table) rather than parsed from a space-delimited macro
-- string at runtime.
CREATE OR REPLACE MACRO age_in_unit(birth_date, as_of, unit) AS
    CASE unit
        WHEN 'years'  THEN age_years(birth_date, as_of)
        WHEN 'months' THEN date_diff('month', birth_date, as_of)
                           - CASE WHEN day(as_of) < day(birth_date) THEN 1 ELSE 0 END
        WHEN 'days'   THEN date_diff('day', birth_date, as_of)
        WHEN 'weeks'  THEN date_diff('day', birth_date, as_of) // 7
        ELSE NULL
    END;

-- Coverage predicate (%ms_episoderec2 step 1). Kept as a macro so the
-- three coverage modes stay in one place instead of three Python branches.
CREATE OR REPLACE MACRO has_coverage(medcov, drugcov, coverage) AS
    CASE upper(coverage)
        WHEN 'MD' THEN upper(medcov) IN ('Y','A') AND upper(drugcov) = 'Y'
        WHEN 'M'  THEN upper(medcov) IN ('Y','A')
        WHEN 'D'  THEN upper(drugcov) = 'Y'
        ELSE TRUE
    END;

-- Date <-> integer day number. The stockpiling closed form needs to do
-- arithmetic on dates as integers; keeping the conversion in one macro
-- pair means the epoch choice is stated once.
CREATE OR REPLACE MACRO day_num(d) AS
    date_diff('day', DATE '1970-01-01', d);

CREATE OR REPLACE MACRO from_day_num(n) AS
    DATE '1970-01-01' + CAST(n AS INTEGER);

-- SAS `missing()` on a CHARACTER variable is true for NULL *and* for a
-- blank or empty string. `IS NULL` alone is not equivalent, and the
-- difference is invisible until a source column uses '' rather than
-- NULL — which real SCDM postalcode does.
CREATE OR REPLACE MACRO is_missing(v) AS
    v IS NULL OR trim(CAST(v AS VARCHAR)) = '';
