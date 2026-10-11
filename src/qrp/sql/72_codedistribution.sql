-- =====================================================================
-- 72_codedistribution.sql — the index-code distribution
-- (ms_codedistribution.sas): which codes, and which COMBINATIONS of codes,
-- defined each episode's index (exp) and first event (hoi).
--
-- The ONE definition. It produces:
--   distindexmap        SAS's map: every numbered entity per cohort and
--                       type (drug stockgroups, then medical codes);
--   distindex           SAS's distribution: episodes per id list;
--   distindex_episodes  each episode's two lists, which the master list
--                       joins (62_mstr_wide.sql).
-- This file used to hold a second, older implementation that numbered
-- every NDC separately and wrote the type as EXP/HOI: on wp307 it gave
-- 2,030 distribution rows and 12,488 map rows where SAS has 266 and 3,997.
-- =====================================================================
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
                  AND i.codecat = m.codecat
                  -- a code given without a code type matches any type, as
                  -- everywhere else (e.g. trunc_claims); requiring equality
                  -- matched none of the demo's 1,778 events
                  AND (i.codetype = '' OR i.codetype = m.codetype)
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
                  AND i.codecat = m.codecat
                  -- a code given without a code type matches any type, as
                  -- everywhere else (e.g. trunc_claims); requiring equality
                  -- matched none of the demo's 1,778 events
                  AND (i.codetype = '' OR i.codetype = m.codetype)
                  AND i.code = m.code AND i.enctype = m.enctype AND i.pdx = m.pdx
)
SELECT cohortgrp, patid, indexdt,
       string_agg(CAST(id AS VARCHAR), '_' ORDER BY CAST(id AS VARCHAR)) AS lst
FROM (SELECT DISTINCT * FROM hits)
GROUP BY 1, 2, 3;

-- SAS's map, in its columns. The hoi entities are listed only for a
-- cohort with at least one first event; any other cohort gets ONE blank
-- hoi row. (Read from SAS's wp307 output: 37 of 40 cohorts have no
-- event, each with a single blank hoi row; the 3 with events match this
-- numbering row for row.)
CREATE OR REPLACE TABLE distindexmap AS
WITH with_event AS (SELECT DISTINCT cohortgrp FROM cohort_final WHERE feventdt IS NOT NULL)
SELECT cohortgrp AS "group", typ AS distindextype, stockgroup, codecat,
       codetype, enctype, pdx, code, id AS distindexid
FROM _di_ids
WHERE typ = 'exp' OR cohortgrp IN (SELECT cohortgrp FROM with_event)
UNION ALL
SELECT DISTINCT c.cohortgrp, 'hoi', NULL, NULL, NULL, NULL, NULL, NULL, NULL::BIGINT
FROM cohort_final c
WHERE c.cohortgrp NOT IN (SELECT cohortgrp FROM with_event)
ORDER BY 1, 2, 9 NULLS FIRST;

-- each episode's two lists, for the master list
CREATE OR REPLACE TABLE distindex_episodes AS
SELECT f.cohortgrp, f.patid, f.indexdt,
       e.lst AS distindexexp, h.lst AS distindexhoi
FROM cohort_final f
LEFT JOIN _di_exp e ON e.cohortgrp = f.cohortgrp AND e.patid = f.patid AND e.indexdt = f.indexdt
LEFT JOIN _di_hoi h ON h.cohortgrp = f.cohortgrp AND h.patid = f.patid AND h.indexdt = f.indexdt;

-- SAS's distribution: episodes per non-empty id list. A cohort with no
-- list for a type gets ONE placeholder row instead: blank list, episodes
-- missing, mirroring the map's blank row. (Read from SAS's wp307 output:
-- the 37 cohorts without an event each have one blank hoi row with no
-- count; the 3 with events have no blank row at all.)
CREATE OR REPLACE TABLE distindex AS
WITH lists AS (
    SELECT cohortgrp, 'exp' AS typ, distindexexp AS lst FROM distindex_episodes
    WHERE coalesce(distindexexp, '') <> ''
    UNION ALL
    SELECT cohortgrp, 'hoi', distindexhoi FROM distindex_episodes
    WHERE coalesce(distindexhoi, '') <> ''
)
SELECT cohortgrp AS "group", typ AS distindextype, lst AS distindexlist,
       count(*) AS episodes
FROM lists
GROUP BY 1, 2, 3
UNION ALL
SELECT c.cohortgrp, t.typ, '', NULL::BIGINT
FROM (SELECT DISTINCT cohortgrp FROM distindex_episodes) c
CROSS JOIN (VALUES ('exp'), ('hoi')) t(typ)
WHERE NOT EXISTS (SELECT 1 FROM lists l WHERE l.cohortgrp = c.cohortgrp AND l.typ = t.typ)
ORDER BY 1, 2, 3;
