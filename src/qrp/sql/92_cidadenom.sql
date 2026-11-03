-- =====================================================================
-- 92_cidadenom.sql — enrolled member-days (ms_cidadenom.sas).
--
-- The BACKGROUND denominator: how much eligible enrolled time existed,
-- whether or not anyone was exposed. Distinct from the exposed
-- person-time in `denominators`, and it is what SAS merges onto the
-- CIDA numerators to produce rates against the enrolled population.
--
-- The window (ms_cidadenom.sas:126-138, 700-720):
--
--   DenomEnrStartDt = Enr_Start + ENRDAYS
--       an index date needs ENRDAYS of prior enrollment, so eligible
--       time cannot begin until that lookback is satisfied.
--
--   AdjustedEnrEndDt = min(Enr_End, censordate)
--                    - max(0, MinEpisDur-1, MinDaySupp-1, BlackoutPer,
--                             ReqDaysAftInd,
--                             ReqDaysAftEpi + max(MinEpisDur-1,
--                                                 MinDaySupp-1,
--                                                 BlackoutPer))
--       symmetrically, eligible time must end early enough that an index
--       date there could still satisfy every forward-looking
--       requirement.
--
--   MemberDays = DenomEnrEndDt - DenomEnrStartDt + 1, kept when > 0.
--
-- Only spans with `Enr_End >= startdate` are considered, and the
-- enrollment spans already carry death censoring where the study asks
-- for it.
--
-- For studies with INCLUSIONCODES (SAS sets excl_incl=Y), eligible time
-- is further restricted below: each INCLUSION subcondition's windows
-- clip the enrollment (ms_cidadenom.sas:212-261), %ms_dose_denom's
-- qualifying dose spans clip it again (:265-280), exclusion windows are
-- removed further down, and exclusion dose spans remove time too
-- (:366-376). Studies without inclusion codes keep the exact
-- enrollment-minus-exclusions path. See docs/SAS_PARITY.md.
-- =====================================================================

-- Split into three passes rather than one nine-way join. Written as a
-- single statement this tripped `test_no_stage_joins_more_than_six_
-- relations`, which exists because POV1 in exactly this shape set the
-- memory floor: concurrent hash tables mean peak memory is the SUM of
-- the build sides, not the largest.
CREATE OR REPLACE TEMP TABLE _denom_windows AS
WITH windows AS (
    SELECT
        -- Keyed on the denominator CONFIG. Cohorts agreeing on every
        -- parameter this stage reads produce identical denominators;
        -- one pass per config and a fan-out at the end turns 14 cohorts
        -- into 5 passes on the study seen.
        c.denom_cfg_id,
        e.patid,
        e.episode                       AS eligepisode,
        e.enr_start,
        -- Enrolment ends at DEATH. SAS truncates the span before
        -- building the window and deletes it if that leaves the end
        -- before the start (ms_cidanum.sas:2184-2188):
        --
        --   if enrend_death in ('B','C') and DeathDt <= Enr_End
        --       then Enr_End = DeathDt;
        --   if Enr_end < Enr_Start then delete;
        --
        -- Extracts carry enrolment spans that run past a member's
        -- death. Counting them admitted one dead member per
        -- denominator configuration, each contributing their whole
        -- post-death window.
        -- unconditional: SAS gates this on `enrend_death`, a DATA
        -- PARTNER setting rather than a cohort option, and enrolment
        -- outliving a member is an extract artefact either way
        CASE WHEN d.deathdt IS NOT NULL AND d.deathdt <= e.enr_end
             THEN d.deathdt ELSE e.enr_end END AS enr_end,
        -- start: enrollment start plus the required prior-enrollment
        -- start: enrollment start plus the required prior enrollment,
        -- clipped to the query period
        greatest(e.enr_start + c.enr_days, DATE '{start_date}')
                                        AS denom_start,
        -- end: censor date or enrollment end, pulled back by the
        -- longest forward-looking requirement an index date would need
        -- end: the earlier of enrollment end and the query period end,
        -- pulled back by the longest forward-looking requirement an
        -- index date on the last eligible day would still have to
        -- satisfy.
        --
        -- Both halves were established against real SAS output. The
        -- window used to be clipped to `censor_date` rather than the
        -- query period end, which put member-days 45% high; clipping
        -- without the pullback put MEMBERS 1.4% high. Only the two
        -- together agree: 2,503,074 members against SAS's 2,502,986
        -- (0.0035%) and member-days within 0.02%.
        least(CASE WHEN d.deathdt IS NOT NULL AND d.deathdt <= e.enr_end
                   THEN d.deathdt ELSE e.enr_end END,
              DATE '{end_date}')
          - greatest(
                0,
                c.min_epis_dur - 1,
                c.min_days_supp - 1,
                c.blackout_per,
                c.req_days_aft_ind,
                c.req_days_aft_epi + greatest(c.min_epis_dur - 1,
                                              c.min_days_supp - 1,
                                              c.blackout_per)
            )                           AS denom_end
    FROM cfg_denom_cohort c
    JOIN enrollment_spans e
      ON e.enr_cfg_id = c.enr_cfg_id
    -- the member's death, if any: enrolment cannot outlive it
    LEFT JOIN deaths d ON d.patid = e.patid
    WHERE e.enr_end >= DATE '{start_date}'
)
SELECT
    w.*,
    span_days(w.denom_start, w.denom_end) AS memberdays
FROM windows w
-- SAS keeps only positive member days: the end can be truncated past
-- the start.
WHERE w.denom_end >= w.denom_start;

-- ------------------------------------------------------------------
-- INCLUSION-restricted eligible time (excl_incl = Y).
--
-- SAS does not count all enrolled time for INC-condition studies. Per
-- INCLUSION subcondition it derives one window per claim,
--
--   EligStart = ADate - coalesce(CondTo, 99999)
--   EligEnd   = Expiredt - coalesce(CondFrom, -99999)   (dateonly = 'N')
--             = ADate     - coalesce(CondFrom, -99999)   (dateonly = 'Y')
--   if EligStart <= EligEnd;                  (ms_cidadenom.sas:212-219)
--
-- requires the windows of CODEDAYS DISTINCT dispensing dates to overlap
-- (:220-247), and clips the enrollment into the result with
-- %ms_shaveoutside(KeepPartBf=Y) (:253-261). Within a condition the
-- subconditions INTERSECT (successive clips of the same enrollment
-- copy); across conditions the merged STACK_INC windows UNION
-- (:400-449). Exclusion windows are removed afterwards. MINRXDAYS never
-- clips the denominator: it gates the NUMERATOR's index dates
-- (ms_createpov3.sas:374-393), and a study with minrxdays > 1 gets NO
-- denominator from SAS at all — "Outputdenom set to N ... because
-- minrxdays is used in inclusion/exclusion criteria"
-- (ms_setnumloopmacrovars.sas:898-900), enforced in the Python config
-- (StudyConfig.denominator_cohorts). Without this restriction,
-- member-days are overstated for every INC-condition study.
-- ------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE _denom_incl_claims AS
WITH rep AS (
    -- One representative cohort per denominator config. The config id
    -- hashes the INCLUSION rules, so cohorts sharing a config carry
    -- identical rule rows; reading one cohort's keeps a claim from
    -- matching once per sharing cohort and duplicating dose sums.
    SELECT denom_cfg_id, min(cohortgrp) AS cohortgrp
    FROM cfg_denom_map
    GROUP BY denom_cfg_id
)
SELECT
    rep.denom_cfg_id,
    r.criteria,
    r.cond,
    r.subcond,
    s2.codecat,
    s2.code,
    s2.patid,
    s2.adate,
    s2.expiredt,
    s2.rxsup,
    s2.rxamt,
    r.condfrom,
    r.condto,
    r.codedays,
    r.dateonly,
    s2.adate - r.condto                                 AS elig_start,
    CASE WHEN r.dateonly THEN s2.adate - r.condfrom
         ELSE s2.expiredt - r.condfrom END              AS elig_end
FROM (
    -- Windowed like every other claim source. SAS shaves from
    -- `_IT<inclusioncodes>`, the EXTRACTED claim set, so a code outside
    -- the extraction window never restricts anyone. A DX/PX Expiredt is
    -- `ADate + CodeSupply - 1` with CodeSupply defaulting to 1; only
    -- dispensings carry supply.
    SELECT patid, adate, code, 'DX' AS codecat,
           adate AS expiredt, NULL::INTEGER AS rxsup, NULL::DOUBLE AS rxamt
    FROM cdm_diagnosis
     WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
       AND code IN (SELECT code FROM cfg_inclusion_codes)
    UNION ALL
    SELECT patid, adate, code, 'PX',
           adate, NULL::INTEGER, NULL::DOUBLE
    FROM cdm_procedure
     WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
       AND code IN (SELECT code FROM cfg_inclusion_codes)
    UNION ALL
    SELECT patid, adate, code, 'RX',
           adate + CAST(rxsup - 1 AS INTEGER), rxsup, rxamt
    FROM cdm_dispensing
     WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
       AND code IN (SELECT code FROM cfg_inclusion_codes)
) s2
JOIN cfg_inclusion_codes k
  ON k.code = s2.code AND k.codecat = s2.codecat
JOIN rep
  ON rep.cohortgrp = k.cohortgrp
JOIN cfg_inclusion r
  ON r.cohortgrp = k.cohortgrp AND r.criteria = k.criteria
 AND r.cond = k.cond AND r.subcond = k.subcond AND r.codecat = k.codecat
-- only plain index-anchored windows; an episode-end anchor has no
-- meaning without an episode (same skip the EXC shave applies)
WHERE coalesce(r.condfromanchor, '') <> 'EPISODEENDDT'
  AND coalesce(r.condtoanchor, '')   <> 'EPISODEENDDT';

-- %ms_dose_denom: per-subcondition dose thresholds restrict TIME, not
-- members. SAS enumerates one row per member-day inside each claim's
-- window ∩ enrollment (ms_dose_denom.sas:77-92), prorates every
-- overlapping dispensing into the day's [date+CondFrom, date+CondTo]
-- lookback (:154-170), keeps days meeting round(cumdose,1) >= subcumdose
-- and the afdd bounds (:173-182), collapses the qualifying days into
-- eligcumdosestart/end spans (:185-204), and ms_cidadenom shaves those
-- into the denominator — INSIDE for inclusions (:272-280), OUT for
-- exclusions (:366-376). The macro is invoked only when at least one
-- threshold is set: `%if %sysevalf(&subcumdose. > 0) |
-- %sysevalf(&subminafdd. > 0) | %sysevalf(&submaxafdd. > 0)`
-- (ms_cidadenom.sas:265-270).
CREATE OR REPLACE TEMP TABLE _denom_dose_spans AS
WITH gated AS (
    SELECT DISTINCT
        m.denom_cfg_id, r.criteria, r.cond, r.subcond,
        r.condfrom, r.condto, r.mincumdose, r.minafdd, r.maxafdd
    FROM cfg_inclusion r
    JOIN cfg_denom_map m ON m.cohortgrp = r.cohortgrp
    WHERE (coalesce(r.mincumdose, 0) > 0 OR coalesce(r.minafdd, 0) > 0
           OR coalesce(r.maxafdd, 0) > 0)
      -- same anchor skip as the window shaves
      AND coalesce(r.condfromanchor, '') <> 'EPISODEENDDT'
      AND coalesce(r.condtoanchor, '')   <> 'EPISODEENDDT'
),
clipped AS (
    -- SAS enumerates days inside [eligstartdt, eligenddt] ∩ enrollment
    -- (ms_dose_denom.sas:77-92). Enumerating against the member's own
    -- denominator windows and intersecting with them again at the shave
    -- yields the same spans at a fraction of the rows.
    SELECT c.denom_cfg_id, c.criteria, c.cond, c.subcond, c.patid,
           g.condfrom, g.condto, g.mincumdose, g.minafdd, g.maxafdd,
           greatest(c.elig_start, w.denom_start)        AS gs,
           least(c.elig_end, w.denom_end)               AS ge
    FROM _denom_incl_claims c
    JOIN _denom_windows w
      ON w.denom_cfg_id = c.denom_cfg_id AND w.patid = c.patid
     AND periods_overlap(c.elig_start, c.elig_end, w.denom_start, w.denom_end)
    JOIN gated g
      ON g.denom_cfg_id = c.denom_cfg_id AND g.criteria = c.criteria
     AND g.cond = c.cond AND g.subcond = c.subcond
    WHERE c.elig_start <= c.elig_end
),
days AS (
    SELECT DISTINCT denom_cfg_id, criteria, cond, subcond, patid,
           condfrom, condto, mincumdose, minafdd, maxafdd,
           unnest(generate_series(gs, ge, INTERVAL 1 DAY))::DATE AS day
    FROM clipped
    WHERE gs <= ge
),
-- Every dispensing overlapping the day's lookback window is credited,
-- prorated by the share of its supply inside it:
--   cumdose = cumdose * (rxsup - ToDeductBf - ToDeductAf) / rxsup
-- (ms_dose_denom.sas:158-170), where the two deductions are the parts
-- of the supply before [date+CondFrom] and after [date+CondTo] — i.e.
-- the overlap in days. aFDD divides the summed cfdd by the number of
-- dispensings (:173-182); SAS does not prorate it.
rx AS (
    SELECT d.denom_cfg_id, d.criteria, d.cond, d.subcond, d.patid, d.day,
           overlap_days(c.adate,
                        CASE WHEN c.dateonly THEN c.adate ELSE c.expiredt END,
                        d.day + c.condfrom, d.day + c.condto) AS od,
           c.rxsup, c.rxamt, s.strength,
           d.condfrom, d.condto, d.mincumdose, d.minafdd, d.maxafdd
    FROM days d
    JOIN _denom_incl_claims c
      ON c.denom_cfg_id = d.denom_cfg_id AND c.criteria = d.criteria
     AND c.cond = d.cond AND c.subcond = d.subcond AND c.patid = d.patid
     AND periods_overlap(c.adate,
                         CASE WHEN c.dateonly THEN c.adate ELSE c.expiredt END,
                         d.day + c.condfrom, d.day + c.condto)
    JOIN cfg_code_strength s ON s.code = c.code
    WHERE c.codecat = 'RX' AND c.rxsup > 0
),
summed AS (
    SELECT denom_cfg_id, criteria, cond, subcond, patid, day,
           any_value(condfrom) AS condfrom, any_value(condto) AS condto,
           any_value(mincumdose) AS mincumdose,
           any_value(minafdd) AS minafdd, any_value(maxafdd) AS maxafdd,
           sum(strength * rxamt * od / rxsup)           AS cumdose,
           count(*)                                     AS numdispensing,
           sum(strength * rxamt / rxsup)                AS cfdd
    FROM rx
    GROUP BY 1, 2, 3, 4, 5, 6
),
qualified AS (
    -- `where=(round(cumdose,1)>=&subcumdose. and
    --   round((cfdd/numdispensing),1) >= &subminafdd. and
    --   round((cfdd/numdispensing),1) <= &submaxafdd.)`
    -- (ms_dose_denom.sas:173-182). An unset bound is a no-op, so NULL
    -- thresholds fall back to 0 (or no bound); a day with no credited
    -- dispensing has NULL cfdd and fails any set bound, as SAS's
    -- missing-value comparisons do.
    SELECT denom_cfg_id, criteria, cond, subcond, patid, day
    FROM summed
    WHERE round(cumdose, 1) >= coalesce(mincumdose, 0)
      AND round(cfdd / numdispensing, 1) >= coalesce(minafdd, 0)
      AND (maxafdd IS NULL
           OR round(cfdd / numdispensing, 1) <= maxafdd)
),
-- Contiguous qualifying days collapse to eligcumdosestart/end spans
-- (ms_dose_denom.sas:185-204): a new block starts wherever a day is not
-- the successor of the previous one.
marked AS (
    SELECT *,
           CASE WHEN day <= max(day) OVER (
                    PARTITION BY denom_cfg_id, criteria, cond, subcond, patid
                    ORDER BY day
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 1
                THEN 0 ELSE 1 END AS new_block
    FROM qualified
),
blocks AS (
    SELECT *, sum(new_block) OVER (
                 PARTITION BY denom_cfg_id, criteria, cond, subcond, patid
                 ORDER BY day) AS blk
    FROM marked
)
SELECT denom_cfg_id, criteria, cond, subcond, patid,
       min(day) AS span_start, max(day) AS span_end
FROM blocks
GROUP BY 1, 2, 3, 4, 5, blk;

-- Clip the enrolled windows into the INCLUSION-restricted time, the
-- mirror of `_denom_windows_shaved`'s subtraction: there ineligible
-- periods are cut out, here everything OUTSIDE the allowed spans is.
CREATE OR REPLACE TEMP TABLE _denom_windows_incl AS
WITH daywin AS (
    -- One window per (member, DISTINCT dispensing date): SAS assigns an
    -- id that increments only when ADate changes
    -- (ms_cidadenom.sas:220-247), so same-day dispensings are one window
    -- reaching as far as their combined supply does.
    SELECT denom_cfg_id, cond, subcond, patid,
           min(elig_start)                                  AS ws,
           CASE WHEN bool_or(dateonly) THEN min(elig_end)
                ELSE max(elig_end) END                      AS we,
           any_value(codedays)::INTEGER                     AS codedays
    FROM _denom_incl_claims
    WHERE criteria = 'INC' AND elig_start <= elig_end
    GROUP BY denom_cfg_id, cond, subcond, patid, adate
),
-- The common overlap of CODEDAYS windows from DISTINCT dates is the set
-- of days covered by at least CODEDAYS of them: a day inside some
-- combination's common overlap is inside codedays windows, and any
-- codedays covering a day have that day in their overlap. A sweep over
-- starts (+1) and end+1 (-1) finds those days for any codedays where
-- SAS needs one self-join per codedays way (:220-247). At a date tie
-- the -1 events sort first, so a window ending on a day and one
-- starting on it cancel before the count is read for that day.
cd_events AS (
    SELECT denom_cfg_id, cond, subcond, patid, codedays, ws AS d, 1 AS delta
    FROM daywin
    UNION ALL
    SELECT denom_cfg_id, cond, subcond, patid, codedays, we + 1, -1
    FROM daywin
),
cd_run AS (
    SELECT *,
           sum(delta) OVER (PARTITION BY denom_cfg_id, cond, subcond, patid
                            ORDER BY d, delta
                            ROWS BETWEEN UNBOUNDED PRECEDING
                                     AND CURRENT ROW) AS cov
    FROM cd_events
),
cd_span AS (
    SELECT denom_cfg_id, cond, subcond, patid, d, delta,
           CASE WHEN cov >= codedays
                 AND coalesce(lag(cov) OVER w, 0) < codedays
                THEN d END AS s,
           CASE WHEN cov < codedays
                 AND coalesce(lag(cov) OVER w, 0) >= codedays
                THEN d - 1 END AS e
    FROM cd_run
    WINDOW w AS (PARTITION BY denom_cfg_id, cond, subcond, patid
                 ORDER BY d, delta)
),
cd_open AS (
    SELECT *,
           count(s) OVER (PARTITION BY denom_cfg_id, cond, subcond, patid
                          ORDER BY d, delta
                          ROWS UNBOUNDED PRECEDING) AS span_no
    FROM cd_span
),
cd_cov AS (
    SELECT denom_cfg_id, cond, subcond, patid,
           min(s) AS span_start, max(e) AS span_end
    FROM cd_open
    GROUP BY denom_cfg_id, cond, subcond, patid, span_no
),
keys AS (
    -- how many subconditions each INC condition has, and how many of
    -- them carry dose thresholds: a day counts toward the condition
    -- only when EVERY subcondition covers the member then, and when
    -- dose-gated subconditions exist, every one of those covers it too
    SELECT m.denom_cfg_id, r.cond,
           count(DISTINCT r.subcond) AS win_need,
           count(DISTINCT CASE WHEN coalesce(r.mincumdose, 0) > 0
                                    OR coalesce(r.minafdd, 0) > 0
                                    OR coalesce(r.maxafdd, 0) > 0
                               THEN r.subcond END) AS dose_need
    FROM cfg_inclusion r
    JOIN cfg_denom_map m ON m.cohortgrp = r.cohortgrp
    WHERE r.criteria = 'INC'
      AND coalesce(r.condfromanchor, '') <> 'EPISODEENDDT'
      AND coalesce(r.condtoanchor, '')   <> 'EPISODEENDDT'
    GROUP BY 1, 2
),
inc_union AS (
    SELECT c.denom_cfg_id, c.cond, c.patid, k.win_need AS need,
           c.span_start, c.span_end
    FROM cd_cov c
    JOIN keys k
      ON k.denom_cfg_id = c.denom_cfg_id AND k.cond = c.cond
),
inc_events AS (
    SELECT denom_cfg_id, cond, patid, need, span_start AS d, 1 AS delta
    FROM inc_union
    UNION ALL
    SELECT denom_cfg_id, cond, patid, need, span_end + 1, -1
    FROM inc_union
),
inc_run AS (
    SELECT *,
           sum(delta) OVER (PARTITION BY denom_cfg_id, cond, patid
                            ORDER BY d, delta
                            ROWS BETWEEN UNBOUNDED PRECEDING
                                     AND CURRENT ROW) AS cov
    FROM inc_events
),
inc_span AS (
    SELECT denom_cfg_id, cond, patid, d, delta,
           CASE WHEN cov >= need
                 AND coalesce(lag(cov) OVER w, 0) < need THEN d END AS s,
           CASE WHEN cov < need
                 AND coalesce(lag(cov) OVER w, 0) >= need THEN d - 1 END AS e
    FROM inc_run
    WINDOW w AS (PARTITION BY denom_cfg_id, cond, patid ORDER BY d, delta)
),
inc_open AS (
    SELECT *,
           count(s) OVER (PARTITION BY denom_cfg_id, cond, patid
                          ORDER BY d, delta
                          ROWS UNBOUNDED PRECEDING) AS span_no
    FROM inc_span
),
inc_cond AS (
    SELECT denom_cfg_id, cond, patid, min(s) AS span_start, max(e) AS span_end
    FROM inc_open
    GROUP BY denom_cfg_id, cond, patid, span_no
),
dose_union AS (
    -- the dose-gated subconditions of each condition intersect the same
    -- way; conditions with no dose-gated subcondition constrain nothing
    SELECT s.denom_cfg_id, s.cond, s.patid, k.dose_need AS need,
           s.span_start, s.span_end
    FROM _denom_dose_spans s
    JOIN keys k
      ON k.denom_cfg_id = s.denom_cfg_id AND k.cond = s.cond
     AND k.dose_need > 0
    WHERE s.criteria = 'INC'
),
dose_events AS (
    SELECT denom_cfg_id, cond, patid, need, span_start AS d, 1 AS delta
    FROM dose_union
    UNION ALL
    SELECT denom_cfg_id, cond, patid, need, span_end + 1, -1
    FROM dose_union
),
dose_run AS (
    SELECT *,
           sum(delta) OVER (PARTITION BY denom_cfg_id, cond, patid
                            ORDER BY d, delta
                            ROWS BETWEEN UNBOUNDED PRECEDING
                                     AND CURRENT ROW) AS cov
    FROM dose_events
),
dose_span AS (
    SELECT denom_cfg_id, cond, patid, d, delta,
           CASE WHEN cov >= need
                 AND coalesce(lag(cov) OVER w, 0) < need THEN d END AS s,
           CASE WHEN cov < need
                 AND coalesce(lag(cov) OVER w, 0) >= need THEN d - 1 END AS e
    FROM dose_run
    WINDOW w AS (PARTITION BY denom_cfg_id, cond, patid ORDER BY d, delta)
),
dose_open AS (
    SELECT *,
           count(s) OVER (PARTITION BY denom_cfg_id, cond, patid
                          ORDER BY d, delta
                          ROWS UNBOUNDED PRECEDING) AS span_no
    FROM dose_span
),
dose_cond AS (
    SELECT denom_cfg_id, cond, patid, min(s) AS span_start, max(e) AS span_end
    FROM dose_open
    GROUP BY denom_cfg_id, cond, patid, span_no
),
cond_keys AS (
    -- Per subcondition the eligibility is codedays-window ∩ dose-span
    -- (ms_cidadenom.sas:253-261 then :265-280), and intersecting across
    -- subconditions distributes over the pairwise intersections — so
    -- the condition's sweep needs every window-covering subcondition
    -- AND every dose-covering one.
    SELECT denom_cfg_id, cond,
           1 + CASE WHEN dose_need > 0 THEN 1 ELSE 0 END AS need
    FROM keys
),
-- Intersect the condition's window time with its dose time: two span
-- sets that are each already disjoint, counted by the same sweep —
-- covered exactly when both cover.
cond_events AS (
    SELECT e.denom_cfg_id, e.cond, e.patid, k.need,
           e.d, e.delta
    FROM (
        SELECT denom_cfg_id, cond, patid, span_start AS d, 1 AS delta
        FROM inc_cond
        UNION ALL
        SELECT denom_cfg_id, cond, patid, span_end + 1, -1 FROM inc_cond
        UNION ALL
        SELECT denom_cfg_id, cond, patid, span_start, 1 FROM dose_cond
        UNION ALL
        SELECT denom_cfg_id, cond, patid, span_end + 1, -1 FROM dose_cond
    ) e
    JOIN cond_keys k
      ON k.denom_cfg_id = e.denom_cfg_id AND k.cond = e.cond
),
cond_run AS (
    SELECT *,
           sum(delta) OVER (PARTITION BY denom_cfg_id, cond, patid
                            ORDER BY d, delta
                            ROWS BETWEEN UNBOUNDED PRECEDING
                                     AND CURRENT ROW) AS cov
    FROM cond_events
),
cond_span AS (
    SELECT denom_cfg_id, cond, patid, d, delta,
           CASE WHEN cov >= need
                 AND coalesce(lag(cov) OVER w, 0) < need THEN d END AS s,
           CASE WHEN cov < need
                 AND coalesce(lag(cov) OVER w, 0) >= need THEN d - 1 END AS e
    FROM cond_run
    WINDOW w AS (PARTITION BY denom_cfg_id, cond, patid ORDER BY d, delta)
),
cond_open AS (
    SELECT *,
           count(s) OVER (PARTITION BY denom_cfg_id, cond, patid
                          ORDER BY d, delta
                          ROWS UNBOUNDED PRECEDING) AS span_no
    FROM cond_span
),
cond_cov AS (
    SELECT denom_cfg_id, cond, patid, min(s) AS span_start, max(e) AS span_end
    FROM cond_open
    GROUP BY denom_cfg_id, cond, patid, span_no
),
-- ACROSS conditions the windows UNION: "_Denom&Group._STACK_INC includes
-- all time periods where the potential index dates will meet at least
-- one of the cond inclusions" (ms_cidadenom.sas:406). Same merge as
-- `_denom_unelig`.
raw AS (
    SELECT denom_cfg_id, patid, span_start, span_end FROM cond_cov
),
marked AS (
    SELECT *,
           CASE WHEN span_start <= max(span_end) OVER (
                    PARTITION BY denom_cfg_id, patid ORDER BY span_start
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                THEN 0 ELSE 1 END AS new_block
    FROM raw
),
blocks AS (
    SELECT *, sum(new_block) OVER (
                 PARTITION BY denom_cfg_id, patid
                 ORDER BY span_start) AS blk
    FROM marked
),
allow AS (
    SELECT denom_cfg_id, patid,
           min(span_start) AS span_start, max(span_end) AS span_end
    FROM blocks
    GROUP BY denom_cfg_id, patid, blk
)
-- DuckDB's greatest/least ignore NULLs, so with no matching allowed
-- span the window keeps its own bounds — and the WHERE drops it once
-- the denominator config HAS inclusion rules. A config with none must
-- pass through exactly: SAS scopes excl_incl per group (built from
-- that group's rules only) and never clips a group without them.
SELECT w.denom_cfg_id, w.patid, w.eligepisode, w.enr_start, w.enr_end,
       greatest(w.denom_start, a.span_start) AS denom_start,
       least(w.denom_end, a.span_end)        AS denom_end,
       span_days(greatest(w.denom_start, a.span_start),
                 least(w.denom_end, a.span_end)) AS memberdays
FROM _denom_windows w
LEFT JOIN allow a
  ON a.denom_cfg_id = w.denom_cfg_id AND a.patid = w.patid
 AND a.span_start <= w.denom_end AND a.span_end >= w.denom_start
WHERE a.span_start IS NOT NULL
   OR NOT EXISTS (
        SELECT 1
        FROM cfg_inclusion i
        JOIN cfg_denom_map m ON m.cohortgrp = i.cohortgrp
        WHERE i.criteria = 'INC' AND m.denom_cfg_id = w.denom_cfg_id);

DROP TABLE _denom_windows;
ALTER TABLE _denom_windows_incl RENAME TO _denom_windows;

-- Demographics for stratification, on the same eligibility rules the
-- numerator uses. Age is taken at the START of the eligible window,
-- which is the earliest date an index could occur in it.
-- Pass 2a: demographic eligibility, same rules the numerator applies.

-- ---------------------------------------------------------------
-- Remove exposed-plus-washout time from the denominator.
--
-- SAS makes a member INELIGIBLE from the day after an exposure claim
-- until its supply expires plus the washout
-- (ms_cidadenom.sas:461-478), then shaves those periods out of the
-- enrolled window with %ms_shaveinside. A window split by an exposure
-- becomes two windows.
--
-- Missing this made every cohort's denominator identical — 62,730 for
-- all 40 in the study compared — where SAS gives 62,462 for the
-- incident cohorts (washout 183) and 62,729 for the prevalent ones
-- (washout 0, where SAS skips the block entirely).
--
-- The `washout <> 0` gate is SAS's, not an optimisation: with no
-- washout the block does not run at all.
-- ---------------------------------------------------------------
-- Outcome-event periods, built separately so the ineligible-period
-- union below stays within the join budget the memory-floor test
-- enforces.
-- Periods around each follow-up EVENT (the outcome). SAS makes a
-- member ineligible from `ADate - BLACKOUTPER + 1` to
-- `ExpireDt + FUPWASHPER` for every outcome claim, across the whole
-- eligible population (ms_cidadenom.sas:486-515). A medical claim's
-- ExpireDt is `ADate + CodeSupply - 1`, with CodeSupply defaulting
-- to 1, so with the usual parameters each claim removes its own day.
--
-- This was missing entirely. It is small in member-days but it is
-- the ONLY thing that separates two cohorts differing only in their
-- outcome, so without it those denominators were identical.
CREATE OR REPLACE TEMP TABLE _denom_event_periods AS
SELECT DISTINCT
    m.denom_cfg_id,
    'a:' || ev.cohortgrp                                     AS scope,
    ev.patid,
    ev.adate - CAST(c.blackout_per AS INTEGER) + 1           AS unelig_start,
    ev.adate + CAST(coalesce(sup.code_supply, 1) AS INTEGER) - 1
             + CAST(coalesce(c.fup_wash_per, 0) AS INTEGER)  AS unelig_end
FROM event_claims ev
JOIN cfg_denom_map m ON m.cohortgrp = ev.cohortgrp
JOIN cfg_cohort    c ON c.cohortgrp = ev.cohortgrp
LEFT JOIN (
    SELECT cohortgrp, code, max(code_supply) AS code_supply
    FROM cfg_codes WHERE role = 'EVENT' GROUP BY 1, 2
) sup ON sup.cohortgrp = ev.cohortgrp AND sup.code = ev.code;

-- The denominator's OWN washout chain — reset at every enrolment span.
--
-- SAS shaves washout from `_groupindex`: stockpiled exposure joined to
-- enrolment. Its stockpile chain RESTARTS in each enrolment span: a fill
-- in a new span is never pushed by supply carried over from the
-- previous one. Patient 98573092 shows it directly — enrolled to
-- 2019-07-31 and again from 2019-10-01, their 2019-10-21 fill stays on
-- 2019-10-21 in SAS, where a single continuous chain pushes it to
-- 2019-11-09 with supply left over from July.
--
-- Two earlier versions got this partly right and are worth recording:
--   * chaining every claim, then joining to enrolment: -34 member-days;
--   * chaining claims within each patient's overall enrolment range:
--     -24 — it fixed a warfarin patient (64384723, enrolled 2014-21 and
--     2023) only because dropping out-of-range claims happened to stop
--     drift crossing the gap. Resetting per span is the actual rule.
-- Per-span chaining: denominator members AND member-days exact on all
-- 40 cohorts of the study compared.
--
-- The episode path keeps `stockpiled`, which already matches SAS
-- exactly, and is not touched.
CREATE OR REPLACE TEMP TABLE _denom_chain AS
WITH kept AS (
    SELECT x.cohortgrp, x.stockgroup, x.patid, x.adate, x.rxsup, x.codecat,
           en.enr_start AS span
    FROM exposure_claims x
    JOIN cfg_cohort c ON c.cohortgrp = x.cohortgrp
    JOIN enrollment_spans en ON en.enr_cfg_id = c.enr_cfg_id AND en.patid = x.patid
                            AND x.adate BETWEEN en.enr_start AND en.enr_end
    WHERE c.wash_per <> 0
      AND (x.codecat <> 'RX'
           OR x.adate + CAST(x.rxsup - 1 AS INTEGER) >= DATE '{start_date}' - c.enr_days)
),
sameday AS (
    SELECT cohortgrp, stockgroup, patid, span, adate, sum(rxsup)::INTEGER AS rxsup
    FROM kept WHERE codecat = 'RX' GROUP BY 1, 2, 3, 4, 5
),
running AS (
    SELECT *, sum(rxsup) OVER w AS cum_sup,
           day_num(adate) - (sum(rxsup) OVER w - rxsup) AS anchor
    FROM sameday
    WINDOW w AS (PARTITION BY cohortgrp, stockgroup, patid, span
                 ORDER BY adate ROWS UNBOUNDED PRECEDING)
),
solved AS (
    SELECT *, max(anchor) OVER (PARTITION BY cohortgrp, stockgroup, patid, span
                                ORDER BY adate ROWS UNBOUNDED PRECEDING) AS ra
    FROM running
)
SELECT cohortgrp, patid,
       from_day_num(cum_sup - 1 + ra - rxsup + 1) AS adate,
       from_day_num(cum_sup - 1 + ra)             AS expiredt
FROM solved
UNION ALL
SELECT cohortgrp, patid, adate, adate + CAST(max(rxsup) - 1 AS INTEGER)
FROM kept WHERE codecat <> 'RX'
GROUP BY cohortgrp, stockgroup, patid, adate;

CREATE OR REPLACE TEMP TABLE _denom_unelig AS
WITH raw AS (
    SELECT DISTINCT
        m.denom_cfg_id,
        x.patid,
        -- STOCKPILED dates. SAS shaves from `ADate + 1` to
        -- `ExpireDt + washper` where both come from the stockpiled
        -- exposure (ms_cidadenom.sas:461-478), not from the raw claim.
        -- Using raw dates made the ineligible window start and end too
        -- early, so members re-entered the denominator sooner than SAS
        -- allows — visible as the incident cohorts carrying every large
        -- difference, since only they have a washout at all.
        x.adate + 1                                   AS unelig_start,
        -- The supply is CLIPPED AT THE ENROLMENT END before the
        -- washout is added. SAS builds `_groupindex` by joining the
        -- exposure to enrolment, and its ExpireDt never runs past
        -- `Enr_End` — 46 of the 47 rows where its supply differed from
        -- this package's ended exactly there. Carrying the full supply
        -- made the ineligible window run days past where SAS ends it.
        least(x.expiredt, en.enr_end)
            + CAST(c.wash_per AS INTEGER)             AS unelig_end
    FROM _denom_chain x
    JOIN cfg_denom_map m ON m.cohortgrp = x.cohortgrp
    JOIN cfg_cohort    c ON c.cohortgrp = x.cohortgrp
    -- the enrolment span the claim falls in: SAS shaves from
    -- `_groupindex`, which is the exposure JOINED to enrolment
    JOIN enrollment_spans en
      ON en.enr_cfg_id = c.enr_cfg_id AND en.patid = x.patid
     AND x.adate BETWEEN en.enr_start AND en.enr_end
    WHERE c.wash_per <> 0
      -- SAS shaves from `_groupindex`, which is the stockpiled
      -- exposure JOINED TO ENROLMENT (ms_cidadenom.sas:459) — not
      -- every exposure claim. A dispensing filled outside any
      -- enrolment span never makes a member ineligible, and counting
      -- it removed eligible time SAS keeps.


    UNION ALL

    -- Periods disqualified by an EXCLUSION condition. SAS shaves these
    -- out of the denominator too (ms_cidadenom.sas:145, `excl_incl`),
    -- so eligibility depends on the exclusion criteria, not only on
    -- enrolment and exposure.
    --
    -- The mapping is the inverse of the rule: an index at T is excluded
    -- when a matching code falls in [T + condfrom, T + condto], so a
    -- code at D disqualifies indices in [D - condto, D - condfrom].
    SELECT DISTINCT
        m.denom_cfg_id,
        s2.patid,
        s2.adate - r.condto                           AS unelig_start,
        s2.adate - r.condfrom                         AS unelig_end
    -- NOT cohort_claims: that is materialised from the master list
    -- when the numerator is a small share of the extract, so it holds
    -- only patients who reached an episode. Denominator eligibility
    -- covers every enrolled member, and shaving it with a
    -- numerator-filtered claim source leaves excluded time in the
    -- denominator for everyone outside the cohort.
    -- Each branch pre-filtered to inclusion codes (a semi-join applied at
    -- the scan): the join below then sees candidates, not every claim.
    FROM (
        -- Windowed like every other claim source. SAS shaves from
        -- `_IT<inclusioncodes>`, the EXTRACTED claim set, so a code
        -- outside the extraction window never disqualifies anyone.
        -- Reading the raw tables unbounded created shave periods SAS
        -- does not have.
        SELECT patid, adate, code, 'DX' AS codecat FROM cdm_diagnosis
         WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
           AND code IN (SELECT code FROM cfg_inclusion_codes)
        UNION ALL
        SELECT patid, adate, code, 'PX' FROM cdm_procedure
         WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
           AND code IN (SELECT code FROM cfg_inclusion_codes)
        UNION ALL
        SELECT patid, adate, code, 'RX' FROM cdm_dispensing
         WHERE adate BETWEEN DATE '{claims_from}' AND DATE '{claims_to}'
           AND code IN (SELECT code FROM cfg_inclusion_codes)
    ) s2
    JOIN cfg_inclusion_codes k
      ON k.code = s2.code AND k.codecat = s2.codecat
    JOIN cfg_inclusion r
      ON r.cohortgrp = k.cohortgrp AND r.criteria = k.criteria
     AND r.cond = k.cond AND r.subcond = k.subcond
     AND r.codecat = s2.codecat
    JOIN cfg_denom_map m ON m.cohortgrp = r.cohortgrp
    WHERE r.criteria = 'EXC'
      -- only plain index-anchored windows; an episode-end anchor has no
      -- meaning without an episode
      AND coalesce(r.condfromanchor, '') <> 'EPISODEENDDT'
      AND coalesce(r.condtoanchor, '')   <> 'EPISODEENDDT'

    UNION ALL

    -- Periods disqualified by an EXCLUSION subcondition's DOSE
    -- thresholds: %ms_dose_denom's qualifying spans are shaved OUT of
    -- the denominator on the exclusion side (ms_cidadenom.sas:366-376),
    -- the mirror of the inclusion shave at :272-280 — there meeting the
    -- dose profile keeps time, here meeting it removes it.
    SELECT denom_cfg_id, patid, span_start AS unelig_start,
           span_end AS unelig_end
    FROM _denom_dose_spans
    WHERE criteria = 'EXC'
),
-- SAS drops degenerate periods BEFORE merging
-- (`if UneligStart<=UneligEnd;`, ms_cidadenom.sas:467). One whose
-- start is past its end shaves nothing on its own, but carried into
-- the running-max merge below it can still extend a block.
guarded AS (
    SELECT * FROM raw WHERE unelig_start <= unelig_end
),
-- merge overlapping periods per member: a running max of the end seen
-- so far starts a new block whenever a gap appears
marked AS (
    SELECT *,
           CASE WHEN unelig_start <= max(unelig_end) OVER (
                    PARTITION BY denom_cfg_id, patid ORDER BY unelig_start
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                THEN 0 ELSE 1 END AS new_block
    FROM guarded
),
blocks AS (
    SELECT *, sum(new_block) OVER (
                 PARTITION BY denom_cfg_id, patid ORDER BY unelig_start) AS blk
    FROM marked
)
SELECT denom_cfg_id, patid, min(unelig_start) AS unelig_start,
       max(unelig_end) AS unelig_end
FROM blocks GROUP BY 1, 2, blk;

-- ------------------------------------------------------------------
-- SCOPES: share the expensive work across cohorts that differ only in
-- their outcome.
--
-- The outcome-event shave touches very few members (38 in wp307), but
-- keying the whole denominator by an outcome-aware config doubled every
-- per-member table: 2.7M rows where 1.35M carry the same information.
-- Instead the config stays outcome-free, and each cohort's event
-- patients are computed again in two small scopes of their own:
--
--   '*'        every member, shared ineligible periods
--   'b:<grp>'  that cohort's event patients, shared periods   (subtract)
--   'a:<grp>'  the same patients, shared + event periods      (add back)
--
-- so  counts(cohort) = '*' + 'a:<grp>' - 'b:<grp>'.  Member-days and
-- distinct members are both additive over disjoint sets of patients,
-- so the result is exact, not approximate.
-- ------------------------------------------------------------------
CREATE OR REPLACE VIEW _denom_ev_pat AS
SELECT DISTINCT denom_cfg_id, scope, patid FROM _denom_event_periods;

-- shared periods plus event periods, merged again for the 'a:' scope
CREATE OR REPLACE VIEW _denom_unelig_a AS
WITH raw AS (
    SELECT p.denom_cfg_id, p.scope, u.patid, u.unelig_start, u.unelig_end
    FROM _denom_unelig u
    JOIN _denom_ev_pat p ON p.denom_cfg_id = u.denom_cfg_id AND p.patid = u.patid
    UNION ALL
    SELECT denom_cfg_id, scope, patid, unelig_start, unelig_end
    FROM _denom_event_periods
    WHERE unelig_start <= unelig_end
),
marked AS (
    SELECT *,
           CASE WHEN unelig_start <= max(unelig_end) OVER (
                    PARTITION BY denom_cfg_id, scope, patid ORDER BY unelig_start
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                THEN 0 ELSE 1 END AS new_block
    FROM raw
),
blocks AS (
    SELECT *, sum(new_block) OVER (
                 PARTITION BY denom_cfg_id, scope, patid ORDER BY unelig_start
                 ROWS UNBOUNDED PRECEDING) AS blk
    FROM marked
)
SELECT denom_cfg_id, scope, patid,
       min(unelig_start) AS unelig_start, max(unelig_end) AS unelig_end
FROM blocks GROUP BY denom_cfg_id, scope, patid, blk;

CREATE OR REPLACE VIEW _denom_unelig_x AS
SELECT denom_cfg_id, '*' AS scope, patid, unelig_start, unelig_end
FROM _denom_unelig
UNION ALL
SELECT u.denom_cfg_id, 'b' || substr(p.scope, 2), u.patid, u.unelig_start, u.unelig_end
FROM _denom_unelig u
JOIN _denom_ev_pat p ON p.denom_cfg_id = u.denom_cfg_id AND p.patid = u.patid
UNION ALL
SELECT denom_cfg_id, scope, patid, unelig_start, unelig_end FROM _denom_unelig_a;

CREATE OR REPLACE VIEW _denom_windows_x AS
SELECT '*' AS scope, w.* FROM _denom_windows w
UNION ALL
SELECT p.scope, w.* FROM _denom_windows w
JOIN _denom_ev_pat p ON p.denom_cfg_id = w.denom_cfg_id AND p.patid = w.patid
UNION ALL
SELECT 'b' || substr(p.scope, 2), w.* FROM _denom_windows w
JOIN _denom_ev_pat p ON p.denom_cfg_id = w.denom_cfg_id AND p.patid = w.patid;

-- Split each enrolled window around the ineligible periods inside it.
CREATE OR REPLACE TEMP TABLE _denom_windows_shaved AS
WITH overlapping AS (
    SELECT w.denom_cfg_id, w.scope, w.patid, w.eligepisode, w.enr_start, w.enr_end,
           w.denom_start, w.denom_end,
           greatest(u.unelig_start, w.denom_start) AS us,
           least(u.unelig_end, w.denom_end)        AS ue
    FROM _denom_windows_x w
    JOIN _denom_unelig_x u
      ON u.denom_cfg_id = w.denom_cfg_id AND u.scope = w.scope AND u.patid = w.patid
     AND u.unelig_start <= w.denom_end AND u.unelig_end >= w.denom_start
),
-- the gap BEFORE each ineligible period ...
gaps AS (
    SELECT denom_cfg_id, scope, patid, eligepisode, enr_start, enr_end,
           coalesce(lag(ue) OVER (PARTITION BY denom_cfg_id, scope, patid,
                                  eligepisode ORDER BY us) + 1,
                    denom_start)                    AS seg_start,
           us - 1                                   AS seg_end
    FROM overlapping
    UNION ALL
    -- ... and the tail after the last one
    SELECT denom_cfg_id, scope, patid, eligepisode, enr_start, enr_end,
           max(ue) + 1, max(denom_end)
    FROM overlapping
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT denom_cfg_id, scope, patid, eligepisode, enr_start, enr_end,
       seg_start AS denom_start, seg_end AS denom_end,
       span_days(seg_start, seg_end) AS memberdays
FROM gaps
WHERE seg_end >= seg_start
UNION ALL
-- windows with no ineligible period inside them pass through untouched.
-- They are exactly the windows NOT in `overlapping`, so this is an anti-join
-- on the window's identity, not a correlated NOT EXISTS with a range
-- condition: that made DuckDB decorrelate over every window (wp307: 1.36M
-- of them, ~1s) when only 14k overlap anything.
SELECT w.denom_cfg_id, w.scope, w.patid, w.eligepisode, w.enr_start, w.enr_end,
       w.denom_start, w.denom_end, w.memberdays
FROM _denom_windows_x w
ANTI JOIN (SELECT DISTINCT denom_cfg_id, scope, patid, eligepisode,
                           denom_start, denom_end
           FROM overlapping) o
       ON o.denom_cfg_id IS NOT DISTINCT FROM w.denom_cfg_id
      AND o.scope        IS NOT DISTINCT FROM w.scope
      AND o.patid        IS NOT DISTINCT FROM w.patid
      AND o.eligepisode  IS NOT DISTINCT FROM w.eligepisode
      AND o.denom_start  IS NOT DISTINCT FROM w.denom_start
      AND o.denom_end    IS NOT DISTINCT FROM w.denom_end;

DROP TABLE _denom_windows;
DROP VIEW _denom_windows_x;
DROP VIEW _denom_unelig_x;
DROP VIEW _denom_unelig_a;
DROP VIEW _denom_ev_pat;
ALTER TABLE _denom_windows_shaved RENAME TO _denom_windows;

-- A view, like _denom_strat which reads it: the chain from windows to
-- denomcounts streams, with no 1.36M-row intermediate written (wp307:
-- ~0.5s and the memory). Its source, _denom_windows, is therefore
-- dropped after denomcounts, not here.
CREATE OR REPLACE TEMP VIEW _denom_demog AS
SELECT
    el.denom_cfg_id,
    el.scope,
    el.patid,
    el.eligepisode,
    el.denom_start,
    el.denom_end,
    el.memberdays,
    dm.birth_date,
    dm.sex,
    dm.race,
    dm.hispanic
FROM _denom_windows el
JOIN demographics dm ON dm.patid = el.patid
LEFT JOIN demographics_multi   mx ON mx.patid = el.patid
LEFT JOIN demographics_missing ms ON ms.patid = el.patid
JOIN cfg_denom_demog ds ON ds.denom_cfg_id = el.denom_cfg_id
                 AND ds.dimension = 'sex' AND ds.value = dm.sex_raw
JOIN cfg_denom_demog dr ON dr.denom_cfg_id = el.denom_cfg_id
                 AND dr.dimension = 'race' AND dr.value = dm.race
JOIN cfg_denom_demog dh ON dh.denom_cfg_id = el.denom_cfg_id
                 AND dh.dimension = 'hispanic' AND dh.value = dm.hispanic
WHERE mx.patid IS NULL AND ms.patid IS NULL;


-- Pass 2b: age band. Age is taken at the START of the eligible window,
-- the earliest date an index could occur in it.
-- A view: its one reader (_denom_agg) aggregates it straight to a few
-- rows, so materialising 1.36M rows first (wp307: 0.47s) bought nothing.
CREATE OR REPLACE TEMP VIEW _denom_strat AS
SELECT
    d.denom_cfg_id,
    d.scope,
    d.patid,
    d.eligepisode,
    d.denom_start,
    d.denom_end,
    d.memberdays,
    d.sex,
    d.race,
    d.hispanic,
    a.label                      AS agegroup,
    a.ordinal                    AS agegroupnum,
    year(d.denom_start)::VARCHAR AS index_year
FROM _denom_demog d
JOIN cfg_denom_strata a
  ON a.denom_cfg_id = d.denom_cfg_id
 AND age_in_unit(d.birth_date, d.denom_start, a.unit) BETWEEN a.lo AND a.hi;


-- Two phases, for speed; the counts are identical.
--   1. _denom_agg: each DISTINCT count once per denominator config, SCOPE,
--      level and stratum. The shared scope '*' is the same for every
--      cohort on a config, and an event scope ('a:<cohort>' / 'b:<cohort>')
--      belongs to one cohort, so nothing needs the cohort fan-out here.
--   2. denomcounts: map to cohorts and combine '*' + after - before by
--      plain addition — exactly the FILTERed distinct counts of the
--      one-step form, which spent 3.9s of wp307's run on 40 rows
--      re-counting the shared rows once per cohort.
-- a view: one reader. The planner cannot move the join below the
-- aggregation, so the counting still happens before the cohort fan-out.
CREATE OR REPLACE TEMP VIEW _denom_agg AS
SELECT
    lv.level_id,
    s.denom_cfg_id,
    s.scope,
    CASE WHEN lv.has_agegroup THEN s.agegroup    END   AS agegroup,
    CASE WHEN lv.has_agegroup THEN s.agegroupnum END   AS agegroupnum,
    CASE WHEN lv.has_sex      THEN s.sex         END   AS sex,
    CASE WHEN lv.has_race     THEN s.race        END   AS race,
    CASE WHEN lv.has_hispanic THEN s.hispanic    END   AS hispanic,
    CASE WHEN lv.has_year     THEN s.index_year  END   AS index_year,
    count(DISTINCT s.patid)                            AS n_pts,
    sum(s.memberdays)                                  AS memberdays
FROM _denom_strat s
CROSS JOIN (SELECT * FROM cfg_strata
            WHERE tableid = 't2cida') lv
GROUP BY ALL;

CREATE OR REPLACE TABLE denomcounts AS
SELECT
    a.level_id                                         AS level,
    -- Fan out: one row per COHORT, from the config that produced it.
    m.cohortgrp                                        AS "group",
    a.agegroup,
    a.agegroupnum,
    a.sex,
    a.race,
    a.hispanic,
    -- SAS column names (ms_cidadenom.sas:1386). `year`, not
    -- `index_year`; `DenNumPts`/`DenNumMemDays`, not
    -- `eligible_members`/`memberdays`. These are the metrics a
    -- downstream merge references by name, so they are a contract, not
    -- a description.
    a.index_year                                       AS "year",
    -- Geography and finer time strata appear in SAS's shell and are
    -- stratified on only when a level asks for them. This package does
    -- not offer them as CIDA strata, so they are emitted NULL — the
    -- same convention already used for a level that does not stratify
    -- on agegroup or sex, and it keeps the column set matching.
    NULL::VARCHAR                                      AS zip_uncertain,
    NULL::VARCHAR                                      AS zip3,
    NULL::VARCHAR                                      AS state,
    NULL::VARCHAR                                      AS hhs_reg,
    NULL::VARCHAR                                      AS cb_reg,
    NULL::SMALLINT                                     AS "month",
    NULL::SMALLINT                                     AS quarter,
    -- '*' + after - before; see SCOPES above
    (coalesce(sum(a.n_pts) FILTER (WHERE a.scope = '*'), 0)
      + coalesce(sum(a.n_pts) FILTER (WHERE a.scope LIKE 'a:%'), 0)
      - coalesce(sum(a.n_pts) FILTER (WHERE a.scope LIKE 'b:%'), 0))::BIGINT
                                                       AS dennumpts,
    -- OUTPUTDENOM='M' reports members only: SAS sets DenNumMemDays to
    -- MISSING for those cohorts, and 0 for the rest
    -- (ms_cidadenom.sas:1346-1347). NULL, not 0 — "we did not count
    -- this" and "we counted zero days" are different statements.
    CASE WHEN m.output_denom = 'M' THEN NULL
         ELSE coalesce(sum(a.memberdays) FILTER (WHERE a.scope = '*'), 0)
            + coalesce(sum(a.memberdays) FILTER (WHERE a.scope LIKE 'a:%'), 0)
            - coalesce(sum(a.memberdays) FILTER (WHERE a.scope LIKE 'b:%'), 0)
         END                                           AS dennummemdays
FROM _denom_agg a
-- one row per cohort sharing this config
JOIN cfg_denom_map m ON m.denom_cfg_id = a.denom_cfg_id
 -- each cohort sees the shared scope and only its OWN event scopes
 AND (a.scope = '*' OR substr(a.scope, 3) = m.cohortgrp)
GROUP BY
    a.level_id, m.cohortgrp, m.output_denom,
    a.agegroup, a.agegroupnum, a.sex, a.race, a.hispanic, a.index_year
-- a stratum emptied by the event shave is not emitted, as before
HAVING coalesce(sum(a.n_pts) FILTER (WHERE a.scope = '*'), 0)
     + coalesce(sum(a.n_pts) FILTER (WHERE a.scope LIKE 'a:%'), 0)
     - coalesce(sum(a.n_pts) FILTER (WHERE a.scope LIKE 'b:%'), 0) > 0;
DROP VIEW _denom_strat;
-- after the views that read them, not before: the chain is lazy
DROP VIEW _denom_demog;
DROP TABLE _denom_windows;
