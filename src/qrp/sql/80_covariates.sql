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
-- HOW THE TEST IS DONE: one lookup per window, not one pair per claim.
--
-- The question is only "does ANY qualifying claim overlap the window",
-- never "how many". The previous form joined every matching claim to
-- every episode the same patient has — across every cohort — and only
-- then applied the window and a DISTINCT. On a study with 600k covariate
-- codes over 23 covariates and 40 cohorts that pairing produced 38.7M
-- rows to yield 443k, at a 0.1% sample of the data. The work grows with
-- (claims x episodes) per patient, and on a full extract it exhausted
-- memory and then the spill disk.
--
-- Now:
--   1. each patient's matching claims are deduplicated per covariate and
--      carry a running maximum of their end dates, ordered by start;
--   2. windows are deduplicated across cohorts — families of cohorts
--      (rupture/splenectomy, incident/prevalent) share episodes, so
--      724k (episode, covariate) windows collapse to 186k;
--   3. an ASOF join finds, for each window, the latest claim starting on
--      or before the window's end. The window is hit iff the running
--      maximum end at that claim reaches the window's start — exactly
--      `periods_overlap`, with ONE row per window and no fan-out.
--
-- Measured on that 600k-code study, isolated, 300MB spill allowance:
--
--                       memory needed      time at that limit
--   previous join        2 GB (fails 1.5)        9.2s
--   ASOF lookup        <= 256 MB                 4.0-5.2s
--
-- Output identical (443k rows, no difference either way), and identical
-- on wp307's ordinary 8.5k-code list (17,890 rows).
--
-- THE COST, stated so nobody is surprised: on an ordinary study with
-- ample memory the previous join was faster, 0.25s against 0.80s — about
-- half a second on a ~25s run. That is accepted in exchange for a
-- footprint ~8x smaller that does not fail on large code lists.
--
-- Two earlier rewrites (an EXISTS semi-join, and pre-filtering the
-- claim source) were measured 2.7-4.1x SLOWER at 700k patients and
-- rejected; they kept the claim-by-episode pairing and only changed how
-- it was expressed. This one removes the pairing. Measure before
-- changing it — and measure MEMORY, not only time.
-- ------------------------------------------------------------------
-- Covariate DISPENSINGS are stockpiled, as SAS does.
--
-- SAS does not use fill dates for drug covariates. It clips covariate
-- dispensings to enrolment spans, chains them per patient by covariate
-- and stockgroup, then clips the chained periods to enrolment again
-- (ms_cidacov_codeextraction.sas:590-672). With raw fill dates the
-- post-index drug covariates (days 1-30) disagreed with SAS in BOTH
-- directions — claims sat on different days.
--
-- Keyed by enrolment configuration because the clipping uses each
-- cohort's own enrolment spans; only configurations of cohorts that get
-- covariates (createbaseline = 'Y') are built.
-- ------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE _covar_rx_chain AS
WITH defs AS (
    SELECT DISTINCT covarnum, coalesce(dateonly, FALSE) AS dateonly,
           enr_cfg_id, enr_days
    FROM cfg_covariates
),
clipped AS (
    SELECT DISTINCT d.enr_cfg_id, d.enr_days, s.patid, k.covarnum, k.stockgroup,
           greatest(s.adate, en.enr_start)                  AS adate,
           -- the CLIPPED supply. A fill straddling an enrolment gap becomes
           -- one piece per span; keeping the full supply on each piece
           -- counted it twice (wp307 patient 124844251: a 180-day fill from
           -- 2022-07-19 across an Oct-Dec gap pushed 2023's fills into the
           -- pre-index window).
           date_diff('day', greatest(s.adate, en.enr_start),
                            least(s.expiredt, en.enr_end)) + 1 AS rxsup,
           s.code, s.expiredt AS raw_end
    FROM cohort_claims s
    JOIN cfg_covariate_codes k ON k.code = s.code AND k.codecat = s.codecat
    JOIN defs d ON d.covarnum = k.covarnum
    JOIN enrollment_spans en
      ON en.enr_cfg_id = d.enr_cfg_id AND en.patid = s.patid
     AND s.adate <= en.enr_end AND s.expiredt >= en.enr_start
    WHERE s.codecat = 'RX'
      -- The exposure chain's entry rule applies here too: a dispensing
      -- whose supply ends before the enrolment window opens never enters
      -- the chain. Without it, old fills pushed later ones forward and
      -- the pre-index drug covariates flagged episodes SAS does not
      -- (wp307: mine-only 99 -> 1, SAS-only 14 -> 0).
      AND s.expiredt >= DATE '{start_date}' - d.enr_days
),
sameday AS (
    SELECT enr_cfg_id, enr_days, patid, covarnum, stockgroup, adate,
           sum(rxsup)::INTEGER AS rxsup
    FROM clipped GROUP BY 1, 2, 3, 4, 5, 6
),
running AS (
    SELECT *, sum(rxsup) OVER w AS cum_sup,
           day_num(adate) - (sum(rxsup) OVER w - rxsup) AS anchor
    FROM sameday
    WINDOW w AS (PARTITION BY enr_cfg_id, enr_days, patid, covarnum, stockgroup
                 ORDER BY adate ROWS UNBOUNDED PRECEDING)
),
solved AS (
    SELECT *, max(anchor) OVER (PARTITION BY enr_cfg_id, enr_days, patid, covarnum, stockgroup
                                ORDER BY adate ROWS UNBOUNDED PRECEDING) AS ra
    FROM running
),
chained AS (
    SELECT enr_cfg_id, enr_days, patid, covarnum,
           from_day_num(cum_sup - 1 + ra - rxsup + 1) AS adate,
           from_day_num(cum_sup - 1 + ra)             AS expiredt
    FROM solved
),
reclipped AS (
    -- and clipped to enrolment again, as SAS's second shave does
    SELECT c.enr_cfg_id, c.enr_days, c.patid, c.covarnum,
           greatest(c.adate, en.enr_start) AS adate,
           least(c.expiredt, en.enr_end)   AS expiredt
    FROM chained c
    JOIN enrollment_spans en
      ON en.enr_cfg_id = c.enr_cfg_id AND en.patid = c.patid
     AND c.adate <= en.enr_end AND c.expiredt >= en.enr_start
),
dated AS (
    SELECT DISTINCT r.enr_cfg_id, r.enr_days, r.patid, r.covarnum, f.dateonly, r.adate,
           CASE WHEN f.dateonly THEN r.adate ELSE r.expiredt END AS cend
    FROM reclipped r
    JOIN (SELECT DISTINCT covarnum, dateonly FROM defs) f ON f.covarnum = r.covarnum
),
byday AS (
    SELECT enr_cfg_id, enr_days, patid, covarnum, dateonly, adate, max(cend) AS cend
    FROM dated GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT *, max(cend) OVER (PARTITION BY enr_cfg_id, enr_days, patid, covarnum, dateonly
                          ORDER BY adate ROWS UNBOUNDED PRECEDING) AS cend_max
FROM byday;

CREATE OR REPLACE TABLE covariates_long AS
WITH defs AS (
    SELECT DISTINCT covarnum, coalesce(dateonly, FALSE) AS dateonly
    FROM cfg_covariates
),
cov_claims AS (
    SELECT s.patid, k.covarnum, f.dateonly, s.adate,
           max(CASE WHEN f.dateonly THEN s.adate ELSE s.expiredt END) AS cend
    FROM cohort_claims s
    -- the claim's category must match the CODE's category; a covariate
    -- can mix several (wp307's pegfilgrastim covariate lists NDCs as
    -- dispensings and as procedure claims, plus J-codes). Matching on
    -- one category per covariate found 15 of SAS's 224 episodes.
    JOIN cfg_covariate_codes k ON k.code = s.code AND k.codecat = s.codecat
    JOIN defs f ON f.covarnum = k.covarnum
    -- dispensings come from the stockpiled chain above
    WHERE s.codecat <> 'RX'
    GROUP BY ALL
),
runmax AS (
    SELECT *, max(cend) OVER (PARTITION BY patid, covarnum, dateonly
                              ORDER BY adate ROWS UNBOUNDED PRECEDING) AS cend_max
    FROM cov_claims
),
windows AS (
    SELECT m.cohortgrp, m.patid, m.indexdt, d.covarnum, d.covarname, d.enr_cfg_id, d.enr_days,
           coalesce(d.dateonly, FALSE) AS dateonly,
           CASE WHEN d.covfromanchor = 'EPISODEENDDT'
                THEN m.episodeenddt ELSE m.indexdt END + d.covfrom AS ws,
           CASE WHEN d.covtoanchor = 'EPISODEENDDT'
                THEN m.episodeenddt ELSE m.indexdt END + d.covto AS we
    FROM ptsmasterlist m JOIN cfg_covariates d ON d.cohortgrp = m.cohortgrp
),
uniq AS (SELECT DISTINCT enr_cfg_id, enr_days, patid, covarnum, dateonly, ws, we FROM windows),
hits_other AS (
    SELECT u.* FROM uniq u
    ASOF JOIN runmax r
      ON r.patid = u.patid AND r.covarnum = u.covarnum
     AND r.dateonly = u.dateonly
     AND r.adate <= u.we
    WHERE r.cend_max >= u.ws
),
hits_rx AS (
    SELECT u.* FROM uniq u
    ASOF JOIN _covar_rx_chain r
      ON r.enr_cfg_id = u.enr_cfg_id AND r.enr_days = u.enr_days
     AND r.patid = u.patid
     AND r.covarnum = u.covarnum AND r.dateonly = u.dateonly
     AND r.adate <= u.we
    WHERE r.cend_max >= u.ws
),
hits AS (SELECT * FROM hits_other UNION SELECT * FROM hits_rx)
SELECT DISTINCT w.cohortgrp, w.patid, w.indexdt, w.covarnum, w.covarname
FROM windows w
JOIN hits h USING (enr_cfg_id, enr_days, patid, covarnum, dateonly, ws, we);
