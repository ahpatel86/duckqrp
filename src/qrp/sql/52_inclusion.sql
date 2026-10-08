-- =====================================================================
-- 52_inclusion.sql — INCLUSIONCODES: additional cohort criteria beyond
--                    the exposure definition.
--
-- Semantics, from ms_createpov3.sas (the macro that builds the
-- inclusion/exclusion sets; its exclusion rule is at 592-598):
--
--   * A row carries a code, a lookback window (condfrom/condto relative
--     to the index date), and a direction (INC = must have,
--     EXC = must not have).
--   * `condlevel` groups codes WITHIN a condition: they are
--     alternatives, so any one of them satisfies that level.
--   * `cond` numbers the conditions, and EVERY condition must pass.
--   * `codedays` requires the code on at least that many DISTINCT days,
--     which is how "two separate visits" is expressed.
--
-- So the evaluation is: satisfied-per-(cond, condlevel), then combined
-- with AND across conditions. That is a grouped aggregate followed by a
-- HAVING, not a chain of filters.
--
-- Runs AFTER the master list is built, matching SAS: ms_createpov3 is
-- called with _PtsMasterList, not with the raw index candidates.
--
-- This stage previously ran before episodes, filtering pov1. For
-- INDEXDT-anchored rules the two are equivalent — the predicate depends
-- only on (patid, indexdt), which both carry — but the earlier position
-- made EPISODEENDDT anchoring impossible, because the episode end does
-- not exist yet. Moving it here is also the more faithful placement:
-- SAS's own attrition counts excluded patients at this step.
-- =====================================================================

-- Written as one statement with CTEs rather than two temp tables: each
-- intermediate has exactly one consumer, so materialising them would be
-- a barrier for nothing.
CREATE OR REPLACE TABLE inclusion_excluded AS
WITH hits AS (
    -- Per (episode, condition, level): how many distinct days carry a
    -- matching code? codedays counts DAYS, not claims, so two
    -- dispensings on one day is one day.
    SELECT
        p.cohortgrp, p.patid, p.indexdt,
        r.criteria, r.cond, r.subcond,
        count(DISTINCT s.adate) AS n_days,
        any_value(r.codedays)   AS need_days,
        -- MINRXDAYS is TOTAL DAYS OF SUPPLY in the window, not a claim
        -- count (ms_createpov3.sas:26). Only meaningful for RX; SAS
        -- resets it to 1 elsewhere, which any claim satisfies.
        sum(CASE WHEN r.codecat = 'RX'
                 THEN overlap_days(s.adate, s.expiredt,
                        CASE WHEN r.condfromanchor = 'EPISODEENDDT'
                             THEN p.episodeenddt ELSE p.indexdt END
                        + r.condfrom,
                        CASE WHEN r.condtoanchor = 'EPISODEENDDT'
                             THEN p.episodeenddt ELSE p.indexdt END
                        + r.condto)
                 ELSE 1 END)    AS supply_days,
        any_value(r.minrxdays)  AS need_supply,
        -- Per-subcondition DOSE thresholds (ms_createpov3.sas:333-353,
        -- 440-458). cumdose is strength x rxamt; aFDD is the AVERAGE
        -- filled daily dose over the window,
        --     round(sum(cfdd) / sum(numdispensing), 1)
        -- not a per-claim value — so it is an aggregate here, alongside
        -- the day counts, rather than a filter on individual claims.
        -- Dose is computed from the MATCHED claim, not from
        -- exposure_claims. claim_dose is built over the DEF codes only,
        -- so joining it here gave every inclusion code cumdose = 0 and
        -- any mincumdose threshold excluded the entire cohort.
        -- PRO-RATED by the fraction of supply inside the window
        -- (ms_createpov3.sas:369-375):
        --     ToDeductBf = (adate+condfrom) - incdate      if it starts early
        --     ToDeductAf = incexpiredt - (adate+condto)    if it ends late
        --     cumdose = cumdose * (rxsup - Bf - Af) / rxsup
        -- Counting the whole claim when it merely OVERLAPS the window
        -- over-credits a dispensing that mostly falls outside it. On the
        -- 100k fixture that admitted 205 episodes below the threshold.
        sum(coalesce(cs.strength * s.rxamt
                     * CASE WHEN s.rxsup > 0
                            THEN overlap_days(s.adate, s.expiredt,
                                              CASE WHEN r.condfromanchor = 'EPISODEENDDT'
                       THEN p.episodeenddt ELSE p.indexdt END + r.condfrom,
                                              CASE WHEN r.condtoanchor = 'EPISODEENDDT'
                       THEN p.episodeenddt ELSE p.indexdt END + r.condto)::DOUBLE / s.rxsup
                            ELSE 1 END, 0))                 AS cumdose,
        CASE WHEN count(s.adate) > 0
             THEN round(sum(CASE WHEN s.rxsup > 0
                                 THEN cs.strength * s.rxamt / s.rxsup END)
                        / count(s.adate), 1)
             END                                         AS afdd,
        -- SAS aggregates the thresholds across the rows of a
        -- subcondition: max(mincumdose), min(minafdd), max(maxafdd) —
        -- strictest lower bound, widest upper bound.
        max(r.mincumdose)       AS need_cumdose,
        min(r.minafdd)          AS need_afdd_lo,
        max(r.maxafdd)          AS need_afdd_hi
    FROM ptsmasterlist p
    -- DISTINCT on the condition key. Several INCLUSIONCODES rows share
    -- one (cond, condlevel) — one per code — so joining the raw table
    -- counts every claim once per row. count(DISTINCT adate) hid this
    -- for `codedays`; `minrxdays` uses sum() and exposed it, inflating
    -- supply threefold with three codes.
    JOIN (SELECT DISTINCT cohortgrp, cond, subcond, subcond_inclusion,
                 criteria, codecat, condfrom, condto, codedays, minrxdays,
                 condfromanchor, condtoanchor,
                 mincumdose, minafdd, maxafdd
          FROM cfg_inclusion WHERE criteria IN ('INC', 'EXC')) r
      ON r.cohortgrp = p.cohortgrp
    JOIN cfg_inclusion_codes k
      ON k.cohortgrp = r.cohortgrp
     AND k.criteria  = r.criteria
     AND k.cond      = r.cond
     AND k.subcond   = r.subcond
    JOIN cohort_claims s
      ON s.patid   = p.patid
     AND s.code    = k.code
     AND s.codecat = r.codecat
     -- Each END of the window anchors independently
     -- (ms_createpov3.sas:139-175). EPISODEENDDT gives a
     -- FORWARD-looking window; INDEXDT a lookback. A rule can mix them,
     -- e.g. from index date to episode end.
     AND periods_overlap(
            CASE WHEN r.condfromanchor = 'EPISODEENDDT'
                 THEN p.episodeenddt ELSE p.indexdt END + r.condfrom,
            CASE WHEN r.condtoanchor = 'EPISODEENDDT'
                 THEN p.episodeenddt ELSE p.indexdt END + r.condto,
            s.adate,
            CASE WHEN r.codecat = 'RX' THEN s.expiredt ELSE s.adate END
         )
    -- Strength for the dose thresholds. LEFT and placed AFTER the
    -- covar_source predicate: inserted before it, the periods_overlap
    -- clause binds to THIS join instead, silently disabling the window
    -- filter on the claims.
    LEFT JOIN cfg_code_strength cs
           ON cs.code = s.code
    GROUP BY 1, 2, 3, 4, 5, 6
),
per_subcond AS (
    -- A SUBCONDITION is satisfied when any of its codes meets both
    -- thresholds — codes within a subcondition are alternatives.
    -- `subcond_inclusion = false` marks a sub-EXCLUSION, where being
    -- met is what fails it: "If the subcondition is met but it is a
    -- subexclusion, then means that condition not satisfied"
    -- (ms_createpov3.sas:37).
    SELECT
        p.cohortgrp, p.patid, p.indexdt, r.criteria, r.cond, r.subcond,
        CASE WHEN coalesce(max(CASE WHEN h.n_days >= h.need_days
                                     AND h.supply_days >= h.need_supply
                                     AND (h.need_cumdose IS NULL
                                          OR h.cumdose >= h.need_cumdose)
                                     AND (h.need_afdd_lo IS NULL
                                          OR h.afdd >= h.need_afdd_lo)
                                     AND (h.need_afdd_hi IS NULL
                                          OR h.afdd <= h.need_afdd_hi)
                                    THEN 1 ELSE 0 END), 0) = 1
             THEN any_value(r.subcond_inclusion)
             ELSE NOT any_value(r.subcond_inclusion)
        END AS satisfied
    FROM ptsmasterlist p
    JOIN (SELECT DISTINCT cohortgrp, criteria, cond, subcond,
                 subcond_inclusion
          FROM cfg_inclusion WHERE criteria IN ('INC', 'EXC')) r
      ON r.cohortgrp = p.cohortgrp
    LEFT JOIN hits h
           ON h.cohortgrp = p.cohortgrp
          AND h.patid     = p.patid
          AND h.indexdt   = p.indexdt
          AND h.criteria  = r.criteria
          AND h.cond      = r.cond
          AND h.subcond   = r.subcond
    GROUP BY 1, 2, 3, 4, 5, 6
),
per_cond AS (
    -- "If ALL subconditions are satisfied, then condition is satisfied"
    -- (ms_createpov3.sas:38). Subconditions are ANDed within a
    -- condition; conditions are ANDed with each other. Treating the
    -- inner level as alternatives — which this did while `subcond` was
    -- unmodelled — ORs what SAS ANDs.
    -- Keyed on (cohortgrp, criteria, cond): SAS numbers cond within
    -- (group, conduse), so an INC and an EXC rule can both be cond 1.
    SELECT
        sc.cohortgrp, sc.patid, sc.indexdt, sc.criteria, sc.cond,
        CASE WHEN bool_and(sc.satisfied) THEN 1 ELSE 0 END AS cond_met
    FROM per_subcond sc
    GROUP BY 1, 2, 3, 4, 5
)
SELECT
    cohortgrp,
    patid,
    indexdt,
    min(cond)                          AS first_failed_cond,
    string_agg(DISTINCT criteria, ',') AS failed_criteria
FROM per_cond
-- INC/EXC only. IEV/EEV are anchored on the EVENT date and applied in
-- 60_followup.sql, where a failing rule drops the EVENT rather than the
-- episode.
--
-- They were lumped in here while the event-anchored stage did not
-- exist, which was the best approximation available. The CTEs above now
-- filter to INC/EXC, so these branches were unreachable — dead code
-- that still read as if IEV were handled at the index anchor.
WHERE (criteria = 'INC' AND cond_met = 0)    -- required, not satisfied
   OR (criteria = 'EXC' AND cond_met = 1)    -- forbidden, satisfied
GROUP BY 1, 2, 3;

-- Apply. Rebuilding the master list in place keeps every downstream
-- stage unaware of whether inclusion logic ran at all — the pattern the
-- dose stage also uses.
CREATE OR REPLACE TABLE ptsmasterlist AS
SELECT p.*
FROM ptsmasterlist p
LEFT JOIN inclusion_excluded x
  ON x.cohortgrp = p.cohortgrp
 AND x.patid     = p.patid
 AND x.indexdt   = p.indexdt
WHERE x.patid IS NULL;
