-- =====================================================================
-- 80_covariates.sql — %ms_cidacov: baseline covariate detection.
--
-- This is the stage that produced the documented Spark perf regression
-- (docs/ms_cidacov_perf_regression_session.md). Worth being deliberate
-- about, because the failure modes there were structural rather than
-- incidental.
--
-- Three decisions differ from the port:
--
-- 1. LONG IS THE OUTPUT; WIDE IS A VIEW.
--    The port squared covariates into covar1..covarN columns as the
--    primary result, adding one column per covarnum. With a few hundred
--    covariates that is a very wide table, and every downstream stage
--    carries all of it. Here detection produces one row per
--    (cohort, patient, index, covarnum) and the wide form is a PIVOT
--    materialised only if asked for. Long joins and filters better,
--    and adding a covariate stops being a schema change.
--
-- 2. WINDOW BOUNDS COME FROM THE CONFIG TABLE.
--    covfrom/covto are offsets in days from the anchor. Joining the
--    covariate definition table means one range join covers every
--    covariate, rather than a per-covarnum branch.
--
-- 3. NULL BOUNDS ARE COALESCED IN THE CONFIG, NOT THE PREDICATE.
--    A NULL covfrom means unbounded-left. Resolving that in Python at
--    load time keeps the join predicate simple enough for DuckDB to
--    push down, which the port's COALESCE-inside-the-predicate did not.
-- =====================================================================

-- Every claim that could contribute to any covariate, in one shape.
-- Unioning the source domains once avoids the port's pattern of
-- re-extracting per code category.
--
-- A VIEW, deliberately, and this one matters. As a TABLE it materialised
-- the whole of diagnosis + dispensing (~5.3M rows at 2m patients) as a
-- staging copy that is read exactly once. As a view, DuckDB pushes the
-- downstream join's code filter down into the parquet scans, so only
-- rows matching cfg_covariate_codes are ever read off disk.
--
-- Measured at 2m patients: the covariates stage went 65.1s -> 8.8s
-- (7.4x), total runtime 153.7s -> 94.7s, database file 1103MB -> 423MB.
-- Output verified byte-identical.
-- covar_source is defined ONCE, in pipeline.py, immediately after
-- normalize. This file used to redefine it here — a stale copy that
-- silently overrode the real one and dropped both the PX arm and the
-- rxsup/rxamt columns, so PX covariates matched nothing at all.
-- Reported in review; the duplicate is the bug, not the definition.

-- Detection: one row per (cohort, patient, index, covarnum) where at
-- least one qualifying claim falls in the covariate's anchor window.
--
-- `dateonly='Y'` means the claim is a point event (use adate for both
-- ends); otherwise the supply interval [adate, expiredt] is used, and
-- the test is an interval overlap rather than containment.
-- Two "obvious" optimisations were tried here and BOTH were slower.
-- Measured at 4x real SCDM (700k patients, 60M claims):
--
--   as written (join + DISTINCT)                     5.6s
--   WHERE EXISTS semi-join instead of DISTINCT      23.1s  (4.1x worse)
--   pre-filtering covar_source to covariate codes   21.2s  (3.8x worse)
--
-- Both look like they should help — the question is "did any claim
-- match", not "how many", and restricting the source before a range
-- join is textbook. DuckDB plans the flat join with a hash aggregate
-- better than either, and an intervening semi-join blocks the pushdown
-- the direct join already gets.
--
-- Left as-is deliberately. Measure before changing this.
--
-- A join plus DISTINCT, NOT a correlated EXISTS semi-join.
--
-- EXISTS looks like the better shape — the question is "did any
-- qualifying claim occur", never "how many" — so it was tried. It was
-- 2.7x SLOWER at 2m patients (8.5s -> 23.1s): DuckDB plans the flat
-- join with a hash aggregate well, and does not turn a correlated
-- subquery containing a range predicate into anything as good.
--
-- Left as a join deliberately. Do not "fix" this without measuring.
CREATE OR REPLACE TABLE covariates_long AS
SELECT DISTINCT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    d.covarnum,
    d.covarname
FROM ptsmasterlist m
JOIN cfg_covariates d
  ON d.cohortgrp = m.cohortgrp
JOIN cfg_covariate_codes k
  ON k.covarnum = d.covarnum
JOIN covar_source s
  ON s.patid   = m.patid
 AND s.code    = k.code
 AND s.codecat = d.codecat
 AND periods_overlap(
        -- Each end anchors independently (ms_cidacov.sas:47-54), the
        -- same mechanism the inclusion rules use. Hardcoding indexdt
        -- silently converted an EPISODEENDDT-anchored covariate — a
        -- FORWARD-looking window — into a lookback.
        CASE WHEN d.covfromanchor = 'EPISODEENDDT'
             THEN m.episodeenddt ELSE m.indexdt END + d.covfrom,
        CASE WHEN d.covtoanchor = 'EPISODEENDDT'
             THEN m.episodeenddt ELSE m.indexdt END + d.covto,
        s.adate,
        CASE WHEN d.dateonly THEN s.adate ELSE s.expiredt END
     );

-- Covariate counts, which the baseline table needs and which the port
-- computed by re-scanning the wide table.
CREATE OR REPLACE TABLE covariate_prevalence AS
SELECT
    b.cohortgrp,
    b.covarnum,
    b.covarname,
    count(c.patid)                       AS n_with_covariate,
    b.n_episodes,
    round(100.0 * count(c.patid) / nullif(b.n_episodes, 0), 2) AS pct
FROM (
    SELECT d.cohortgrp, d.covarnum, d.covarname, t.n_episodes
    FROM cfg_covariates d
    JOIN (SELECT cohortgrp, count(*) AS n_episodes
          FROM ptsmasterlist GROUP BY 1) t
      ON t.cohortgrp = d.cohortgrp
) b
LEFT JOIN covariates_long c
       ON c.cohortgrp = b.cohortgrp
      AND c.covarnum  = b.covarnum
GROUP BY 1, 2, 3, b.n_episodes
ORDER BY 1, 2;
