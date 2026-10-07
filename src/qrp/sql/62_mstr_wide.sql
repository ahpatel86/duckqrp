-- ------------------------------------------------------------------
-- Widen the master list to SAS's `<runid>_mstr` shape.
--
-- SAS's mstr is ONE WIDE ROW PER EPISODE carrying everything: the
-- covariate flags, the utilization counts, the comorbidity index and
-- the censoring flags all live on it. This package computes each of
-- those and wrote them to `covariates`, `utilization` and
-- `risk_scores` instead, so a data partner opening `<runid>_mstr`
-- found 53 columns absent even though the values existed elsewhere in
-- the output.
--
-- Columns whose source this package does not model are NOT invented:
-- they are omitted, so their absence stays visible rather than being
-- papered over with a zero.
-- ------------------------------------------------------------------
-- ------------------------------------------------------------------
-- Index-code distribution: SAS's distindexexp / distindexhoi.
--
-- Follows ms_codedistribution.sas exactly. Per cohort and type (exp =
-- the exposure's DEF codes, hoi = the outcome's DEF codes), every index
-- "entity" is numbered by row position over:
--   1. drug stockgroups, sorted;
--   2. medical codes, distinct (codecat, codetype, enctype, pdx, code),
--      sorted, then EXPANDED in place — a care setting of '**' into
--      IP IS ED AV OA, then a DX principal flag of '*' into P S X '' and
--      a PX into X '' (the procedure table has no flag, so procedure
--      claims match the '' rows).
-- An episode's value is the ids of its claims on the index date (exp)
-- or first event date (hoi), joined with '_' in CHARACTER order ('12'
-- before '2'), as SAS sorts the character id.
-- Not covered: pregnancy (PO) and cause-of-death (CD) entities, and
-- SAS's skip for cohorts defined by labs, dates or death.
-- ------------------------------------------------------------------
-- The claims on the dates that matter — each episode's index date and
-- first event date — read ONCE and shared by both lists. Each list used
-- to scan every diagnosis and procedure claim through the enveloping
-- join to find a handful of dates: 2.6s + 2.3s of wp307's 35s, the hoi
-- scan keeping 3 rows. The claims come from the cdm_* views, so they
-- are already ENVELOPED (10_normalize.sql).
CREATE OR REPLACE TEMP TABLE _di_claims AS
WITH dates AS (
    SELECT DISTINCT patid, indexdt AS d FROM cohort_final
    UNION
    SELECT DISTINCT patid, feventdt FROM cohort_final WHERE feventdt IS NOT NULL
)
SELECT x.patid, x.adate, 'PX' AS codecat, x.codetype, x.code,
       coalesce(x.enctype, '') AS enctype, x.pdx
FROM cdm_procedure x SEMI JOIN dates d ON d.patid = x.patid AND d.d = x.adate
UNION ALL
SELECT x.patid, x.adate, 'DX', x.codetype, x.code, x.enctype, coalesce(x.pdx, '')
FROM cdm_diagnosis x SEMI JOIN dates d ON d.patid = x.patid AND d.d = x.adate;

CREATE OR REPLACE TEMP TABLE _di_ids AS
WITH defs AS (
    SELECT k.cohortgrp,
           CASE k.role WHEN 'DEF' THEN 'exp' ELSE 'hoi' END AS typ,
           k.codecat, coalesce(k.codetype, '') AS codetype, k.code, k.stockgroup,
           coalesce(cs.enctype, '**') AS enctype, coalesce(cs.pdx, '*') AS pdx
    FROM cfg_codes k
    LEFT JOIN cfg_care_setting cs ON cs.cohortgrp = k.cohortgrp AND cs.code = k.code
    WHERE k.role IN ('DEF', 'EVENT')
),
rx AS (
    SELECT DISTINCT cohortgrp, typ, stockgroup FROM defs WHERE codecat = 'RX'
),
rx_n AS (
    SELECT cohortgrp, typ, stockgroup,
           row_number() OVER (PARTITION BY cohortgrp, typ ORDER BY stockgroup) AS id
    FROM rx
),
med AS (
    SELECT DISTINCT cohortgrp, typ, codecat, codetype, enctype, pdx, code
    FROM defs WHERE codecat IN ('PX', 'DX')
),
med_base AS (
    SELECT *, row_number() OVER (PARTITION BY cohortgrp, typ
               ORDER BY codecat, codetype, enctype, pdx, code) AS base_n
    FROM med
),
enc_x AS (
    SELECT cohortgrp, typ, codecat, codetype, pdx, code, base_n,
           unnest(CASE WHEN enctype = '**' THEN ['IP', 'IS', 'ED', 'AV', 'OA']
                       ELSE [enctype] END) AS enctype2,
           generate_subscripts(CASE WHEN enctype = '**' THEN ['IP', 'IS', 'ED', 'AV', 'OA']
                       ELSE [enctype] END, 1) AS enc_i
    FROM med_base
),
pdx_x AS (
    SELECT cohortgrp, typ, codecat, codetype, code, base_n, enctype2, enc_i,
           unnest(CASE WHEN codecat = 'DX' AND pdx = '*' THEN ['P', 'S', 'X', '']
                       WHEN codecat = 'PX' THEN ['X', '']
                       ELSE [pdx] END) AS pdx2,
           generate_subscripts(CASE WHEN codecat = 'DX' AND pdx = '*' THEN ['P', 'S', 'X', '']
                       WHEN codecat = 'PX' THEN ['X', '']
                       ELSE [pdx] END, 1) AS pdx_i
    FROM enc_x
)
SELECT cohortgrp, typ, 'RX' AS codecat, NULL::VARCHAR AS codetype,
       NULL::VARCHAR AS enctype, NULL::VARCHAR AS pdx, NULL::VARCHAR AS code,
       stockgroup, id
FROM rx_n
UNION ALL
SELECT p.cohortgrp, p.typ, p.codecat, p.codetype, p.enctype2, p.pdx2, p.code,
       NULL::VARCHAR,
       coalesce((SELECT count(*) FROM rx_n r
                 WHERE r.cohortgrp = p.cohortgrp AND r.typ = p.typ), 0)
       + row_number() OVER (PARTITION BY p.cohortgrp, p.typ
                            ORDER BY p.base_n, p.enc_i, p.pdx_i)
FROM pdx_x p;

CREATE OR REPLACE TEMP TABLE _di_exp AS
WITH hits AS (
    -- dispensings: stockpiled rows of an exposure DRUG stockgroup on the
    -- index date (SAS reads its stockpiled _groupindex)
    SELECT f.cohortgrp, f.patid, f.indexdt, i.id
    FROM cohort_final f
    JOIN stockpiled s ON s.cohortgrp = f.cohortgrp AND s.patid = f.patid
                     AND s.adate = f.indexdt
    JOIN _di_ids i ON i.cohortgrp = f.cohortgrp AND i.typ = 'exp'
                  AND i.codecat = 'RX' AND i.stockgroup = s.stockgroup
    UNION
    SELECT f.cohortgrp, f.patid, f.indexdt, i.id
    FROM cohort_final f
    JOIN _di_claims m ON m.patid = f.patid AND m.adate = f.indexdt
    JOIN _di_ids i ON i.cohortgrp = f.cohortgrp AND i.typ = 'exp'
                  AND i.codecat = m.codecat AND i.codetype = m.codetype
                  AND i.code = m.code AND i.enctype = m.enctype AND i.pdx = m.pdx
)
SELECT cohortgrp, patid, indexdt,
       string_agg(CAST(id AS VARCHAR), '_' ORDER BY CAST(id AS VARCHAR)) AS lst
FROM (SELECT DISTINCT * FROM hits)
GROUP BY 1, 2, 3;

CREATE OR REPLACE TEMP TABLE _di_hoi AS
WITH hits AS (
    SELECT f.cohortgrp, f.patid, f.indexdt, i.id
    FROM cohort_final f
    JOIN cdm_dispensing d ON d.patid = f.patid AND d.adate = f.feventdt
    JOIN cfg_codes k ON k.cohortgrp = f.cohortgrp AND k.role = 'EVENT'
                    AND k.codecat = 'RX' AND k.code = d.code
    JOIN _di_ids i ON i.cohortgrp = f.cohortgrp AND i.typ = 'hoi'
                  AND i.codecat = 'RX' AND i.stockgroup = k.stockgroup
    UNION
    SELECT f.cohortgrp, f.patid, f.indexdt, i.id
    FROM cohort_final f
    JOIN _di_claims m ON m.patid = f.patid AND m.adate = f.feventdt
    JOIN _di_ids i ON i.cohortgrp = f.cohortgrp AND i.typ = 'hoi'
                  AND i.codecat = m.codecat AND i.codetype = m.codetype
                  AND i.code = m.code AND i.enctype = m.enctype AND i.pdx = m.pdx
)
SELECT cohortgrp, patid, indexdt,
       string_agg(CAST(id AS VARCHAR), '_' ORDER BY CAST(id AS VARCHAR)) AS lst
FROM (SELECT DISTINCT * FROM hits)
GROUP BY 1, 2, 3;

CREATE OR REPLACE TABLE cohort_final AS
SELECT
    f.*,
    -- Calendar parts of the index date. SAS carries all three.
    year(f.indexdt)::SMALLINT                      AS "year",
    month(f.indexdt)::SMALLINT                     AS "month",
    ((month(f.indexdt) - 1) / 3 + 1)::SMALLINT     AS quarter,
    -- One surveillance period is modelled, so PeriodID is always 1 and
    -- the index look-end is the query period end.
    1::SMALLINT                                    AS "PeriodID",
    DATE '{end_date}'                              AS "IndexLookEndDt",
    -- Dispensing counts and totals for the episode.
    f.numdispensing::INTEGER                       AS "RawDisp",
    f.numdispensing::INTEGER                       AS "AdjustedDisp",
    -- supply and amount clipped to the episode window, as SAS
    f.episode_totrxsup::INTEGER                    AS "TotRxSup",
    f.episode_totrxamt                             AS "TotRxAmt",
    coalesce(de.lst, '')                          AS "distindexexp",
    coalesce(dh.lst, '')                          AS "distindexhoi",
    -- follow-up length category, from followuptime (matches SAS on
    -- every wp307 episode); Censorcat_sort is its position
    CASE WHEN f.followuptime <= 90 THEN '0-90'
         WHEN f.followuptime <= 180 THEN '91-180'
         ELSE '181+' END                           AS "fupdays_value_cat",
    CASE WHEN f.followuptime <= 90 THEN 1
         WHEN f.followuptime <= 180 THEN 2
         ELSE 3 END::DOUBLE                        AS "Censorcat_sort",
    -- Censoring flags. SAS writes one per reason, and `fup_*` and
    -- `cens_*` are the same flags under two names
    -- (ms_finalizeptsmasterlist.sas:394).
    CASE WHEN f.exit_reason = 'disenrollment' THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_elig,
    CASE WHEN f.exit_reason = 'death'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_dth,
    CASE WHEN f.exit_reason = 'query_end'     THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_qryend,
    CASE WHEN f.exit_reason = 'data_end'      THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_dpend,
    CASE WHEN f.exit_reason = 'exposure_end'  THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_episend,
    CASE WHEN f.episodeenddt_censor IS NOT NULL
              AND f.episodeenddt = f.episodeenddt_censor
         THEN 1 ELSE 0 END::SMALLINT              AS fup_spec,
    CASE WHEN f.exit_reason = 'event'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_event,
    -- `cens_*` are the SAME flags under a second name: SAS renames
    -- them wholesale (ms_finalizeptsmasterlist.sas:394), and both sets
    -- appear on the master list.
    CASE WHEN f.exit_reason = 'disenrollment' THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_elig,
    CASE WHEN f.exit_reason = 'death'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_dth,
    CASE WHEN f.exit_reason = 'query_end'     THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_qryend,
    CASE WHEN f.exit_reason = 'data_end'      THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_dpend,
    -- SAS's short name for the same quantity.
    f.timetocensor                                 AS ttc{mstr_extra}
FROM cohort_final f{mstr_joins}
LEFT JOIN _di_exp de ON de.cohortgrp = f.cohortgrp AND de.patid = f.patid AND de.indexdt = f.indexdt
LEFT JOIN _di_hoi dh ON dh.cohortgrp = f.cohortgrp AND dh.patid = f.patid AND dh.indexdt = f.indexdt;
