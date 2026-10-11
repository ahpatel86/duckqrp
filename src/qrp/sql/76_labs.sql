-- =====================================================================
-- 76_labs.sql — laboratory result extraction (ms_extractlabs.sas).
--
-- Two mechanisms, both configurable per code, and both easy to get
-- wrong in ways that fail silently.
--
-- 1. LABDATETYPE — which date a lab record is dated by. A three
--    character priority string (`ms_extractlabs.sas:176-183`):
--
--        do i = 1 to 3 until (Adate ne .);
--            if level(i)='L' and lab_dt    ne . then Adate = lab_dt;
--            else if level(i)='R' and result_dt ne . then Adate = result_dt;
--            else if level(i)='O' and order_dt  ne . then Adate = order_dt;
--        end;
--
--    So 'LRO' means "lab date, else result date, else order date" — a
--    COALESCE whose ORDER comes from config. 'ROL' is a different
--    answer, not a stylistic variant.
--
-- 2. LABRESULT — a result filter written as a comparison STRING, e.g.
--    '>=7', '<=140', '~=0' or a range '3.5:5.5'
--    (`ms_extractlabs.sas:133-172`). Parsed here rather than in SQL,
--    because the operator is data.
--
--    Note the range separator is ':' not '-'. SAS says why in a
--    comment: a '-' is ambiguous with a negative lower bound.
--
-- Only numeric results (`ResultTyp = 'N'`) are filtered; a record with
-- no criteria is kept.
-- =====================================================================

CREATE OR REPLACE TABLE lab_results AS
WITH dated AS (
    SELECT
        k.cohortgrp,
        k.code       AS labcode,
        k.path,
        k.resulttyp,
        l.patid,
        l.result_num,
        l.result_type,
        -- Apply the LABDATETYPE priority. The CASE ladder is generated
        -- from the three positions of the string, so config drives the
        -- precedence rather than the SQL fixing it. In the real extract
        -- result_dt and order_dt are entirely NULL, so this fall-through
        -- is load-bearing, not defensive.
        coalesce(
            CASE substr(k.labdatetype, 1, 1)
                 WHEN 'L' THEN l.lab_dt WHEN 'R' THEN l.result_dt
                 WHEN 'O' THEN l.order_dt END,
            CASE substr(k.labdatetype, 2, 1)
                 WHEN 'L' THEN l.lab_dt WHEN 'R' THEN l.result_dt
                 WHEN 'O' THEN l.order_dt END,
            CASE substr(k.labdatetype, 3, 1)
                 WHEN 'L' THEN l.lab_dt WHEN 'R' THEN l.result_dt
                 WHEN 'O' THEN l.order_dt END
        ) AS adate,
        k.op,
        k.bound_lo,
        k.bound_hi
    FROM cdm_lab l
    -- THREE extraction paths, dispatched by substr(codetype,1,2)
    -- (ms_extractlabs.sas:201, 301):
    --
    --   '01' LOOKUP — NOT a code column. The lookup file maps a code to
    --        a seven-attribute COMBINATION and the lab record is
    --        matched on that combination (line 251-258). The real SCDM
    --        lab table has no code column at all, which is why.
    --   '02' LOINC  — match the LOINC directly
    --   other  PX   — match the procedure code
    --
    -- SAS runs all three and removes true duplicates afterwards, "as a
    -- record could have been extracted three times using three
    -- different criteria". Matching on the code SET means a record
    -- matched by two paths appears once, with no dedup pass.
    JOIN cfg_labcodes k
      ON (k.path = '01'
          AND k.ms_test_name         = l.ms_test_name
          AND k.ms_test_sub_category = l.ms_test_sub_category
          AND k.specimen_source      = l.specimen_source
          AND k.ms_result_unit       = l.ms_result_unit
          AND k.map_result_type      = l.result_type
          AND k.fast_ind             = l.fast_ind
          AND k.pt_loc               = l.pt_loc)
      OR (k.path = '02' AND k.code = l.loinc)
      OR (k.path NOT IN ('01', '02') AND k.code = l.px)
),
filtered AS (
    SELECT *
    FROM dated
    -- SAS warns and drops a record with no usable date
    -- (ms_extractlabs.sas:267).
    WHERE adate IS NOT NULL
      AND (
        op IS NULL
        -- substr(codetype,3,1) is the RESULT TYPE the criterion applies
        -- to. A numeric criterion does not constrain a non-numeric
        -- result — and in the real extract 77% of records are type 'U'
        -- (unknown), so this branch is the common case, not the edge.
        OR resulttyp <> 'N'
        OR result_type <> 'N'
        OR CASE op
             WHEN '<='  THEN result_num IS NOT NULL AND result_num <= bound_lo
             WHEN '<'   THEN result_num IS NOT NULL AND result_num <  bound_lo
             WHEN '>='  THEN result_num >= bound_lo
             WHEN '>'   THEN result_num >  bound_lo
             WHEN '~='  THEN result_num <> bound_lo
             WHEN ':'   THEN result_num BETWEEN bound_lo AND bound_hi
             WHEN '='   THEN result_num =  bound_lo
             ELSE TRUE
           END
      )
)
SELECT DISTINCT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    f.labcode,
    f.adate,
    f.result_num
FROM ptsmasterlist m
JOIN filtered f
  ON f.cohortgrp = m.cohortgrp
 AND f.patid     = m.patid
 AND f.adate BETWEEN m.indexdt - 365 AND m.indexdt - 1;

CREATE OR REPLACE TABLE lab_summary AS
SELECT
    cohortgrp,
    labcode,
    count(*)                    AS records,
    count(DISTINCT patid)       AS npts,
    round(avg(result_num), 3)   AS mean_result,
    min(result_num)             AS min_result,
    max(result_num)             AS max_result
FROM lab_results
GROUP BY 1, 2
ORDER BY 1, 2;
