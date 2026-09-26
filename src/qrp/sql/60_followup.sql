-- =====================================================================
-- 60_followup.sql — %ms_createpov56 (follow-up washout, blackout) and
--                   first-event attribution.
--
-- The follow-up washout asks: was there an outcome event in the
-- [indexdt - fupwash, indexdt - 1] window? The PySpark port answered it
-- with three separate range self-joins feeding three left-anti joins.
--
-- Here it is one ASOF JOIN. DuckDB's ASOF is built for exactly this
-- "most recent row at or before a key" question and runs as a merge over
-- sorted inputs rather than a range join that fans out and then filters.
-- One pass gives us both the washout answer and the first on-treatment
-- event, which the port computed separately.
-- =====================================================================

-- Claims that disqualify an episode if they fall in the washout window:
-- the outcome codes themselves, plus any `fupcriteria='IOC'` codes
-- (ms_cidanum.sas:1664 -> _FUPWash, consumed by
-- _WashEventsInFupWash). IOC codes never define an index or an outcome;
-- they only disqualify.
CREATE OR REPLACE VIEW washout_claims AS
SELECT cohortgrp, patid, adate FROM event_claims
UNION ALL
-- IOC codes come from the code's own domain too. _FUPWash is set from
-- the same union as _FUPEvent (ms_cidanum.sas:1663-1672), so an IOC
-- code naming a dispensing or a procedure is legitimate and was
-- silently never matching.
SELECT k.cohortgrp, x.patid, x.adate
FROM (
    SELECT patid, adate, code, 'DX' AS codecat FROM cdm_diagnosis
    UNION ALL
    SELECT patid, adate, code, 'PX' AS codecat FROM cdm_procedure
    UNION ALL
    SELECT patid, adate, code, 'RX' AS codecat FROM cdm_dispensing
) x
JOIN cfg_codes k
  ON k.code    = x.code
 AND k.role    = 'IOC'
 AND k.codecat = x.codecat;

-- Most recent disqualifying claim strictly before each index date.
-- A view: consumed once, by cohort_final.
CREATE OR REPLACE VIEW prior_event AS
SELECT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    e.adate AS prior_event_dt
FROM ptsmasterlist m
ASOF LEFT JOIN washout_claims e
       ON e.cohortgrp = m.cohortgrp
      AND e.patid     = m.patid
      AND e.adate     < m.indexdt;

-- First event inside the at-risk window. `eventcount` controls how
-- same-day events are collapsed, which is a config decision resolved
-- in Python — no data probe decides it.
-- Candidate events inside the at-risk window.
--
-- SAS searches from atriskindexdt (or indexdt), NOT shifted by the
-- blackout: ms_createpov56.sas:148 says "Not possible to have event in
-- blackoutper" because those episodes were already excluded above.
-- Shifting here as well was redundant once the exclusion existed, and
-- redundant guards are how the two copies later drift apart.
CREATE OR REPLACE TEMP TABLE _event_candidates AS
SELECT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    e.adate AS eventdt
FROM ptsmasterlist m
JOIN event_claims e
  ON e.cohortgrp = m.cohortgrp
 AND e.patid     = m.patid
 AND e.adate BETWEEN m.atriskindexdt AND m.episodeenddt;

-- IEV / EEV: inclusion and exclusion criteria anchored on the EVENT
-- date rather than the index date (ms_cidanum.sas:1683 routes
-- these to _InclExclHOI; ms_createpov56.sas:186-196 evaluates them
-- against unique (patid, eventdt) pairs and keeps only passing events).
--
-- The distinction from INC/EXC matters: a failing INC/EXC rule removes
-- the EPISODE, a failing IEV/EEV removes only that EVENT. The episode
-- survives and can still be scored on a later qualifying event, or
-- counted as event-free.
--
-- Same evaluation shape as 52_inclusion.sql — satisfied per
-- (cond, condlevel), ORed within a condition, ANDed across conditions —
-- but anchored on eventdt.
CREATE OR REPLACE TABLE event_excluded AS
WITH hits AS (
    SELECT
        ec.cohortgrp, ec.patid, ec.indexdt, ec.eventdt,
        r.criteria, r.cond, r.subcond,
        count(DISTINCT s.adate) AS n_days,
        any_value(r.codedays)   AS need_days,
        -- MINRXDAYS is total DAYS OF SUPPLY in the window, not a claim
        -- count. RX only; SAS resets it to 1 elsewhere.
        sum(CASE WHEN r.codecat = 'RX'
                 THEN overlap_days(s.adate, s.expiredt,
                                   ec.eventdt + r.condfrom,
                                   ec.eventdt + r.condto)
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
                                              ec.eventdt + r.condfrom,
                                              ec.eventdt + r.condto)::DOUBLE / s.rxsup
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
    -- DISTINCT (patid, eventdt): SAS says so explicitly —
    -- "Keep only one event per patid/eventdt for inclusions/exclusions
    -- processing" (ms_createpov56.sas:186).
    --
    -- Without it, an episode with two qualifying claims on one day
    -- enters this join twice and every sum() aggregate below is
    -- doubled — supply_days and cumdose in particular, so a dose
    -- threshold passes on half the real evidence. count(DISTINCT adate)
    -- hides it, which is why it survived. Same shape as the condition
    -- -key duplication fixed on the index-anchored side. Reported in
    -- review.
    FROM (SELECT DISTINCT cohortgrp, patid, indexdt, eventdt
          FROM _event_candidates) ec
    JOIN (SELECT DISTINCT cohortgrp, cond, subcond, subcond_inclusion,
                 criteria, codecat, condfrom, condto, codedays, minrxdays,
                 mincumdose, minafdd, maxafdd
          FROM cfg_inclusion WHERE criteria IN ('IEV', 'EEV')) r
      ON r.cohortgrp = ec.cohortgrp
    JOIN cfg_inclusion_codes k
      ON k.cohortgrp = r.cohortgrp
     AND k.criteria  = r.criteria
     AND k.cond      = r.cond
     AND k.subcond   = r.subcond
    JOIN cohort_claims s
      ON s.patid   = ec.patid
     AND s.code    = k.code
     AND s.codecat = r.codecat
     AND periods_overlap(
            ec.eventdt + r.condfrom,
            ec.eventdt + r.condto,
            s.adate,
            CASE WHEN r.codecat = 'RX' THEN s.expiredt ELSE s.adate END
         )
    -- Strength for the dose thresholds. LEFT and placed AFTER the
    -- covar_source predicate: inserted before it, the periods_overlap
    -- clause binds to THIS join instead, silently disabling the window
    -- filter on the claims.
    LEFT JOIN cfg_code_strength cs
           ON cs.code = s.code
    GROUP BY 1, 2, 3, 4, 5, 6, 7
),
per_subcond AS (
    -- Codes within a subcondition are alternatives; a sub-EXCLUSION
    -- inverts the sense (ms_createpov3.sas:37).
    SELECT
        ec.cohortgrp, ec.patid, ec.indexdt, ec.eventdt,
        r.criteria, r.cond, r.subcond,
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
    FROM (SELECT DISTINCT cohortgrp, patid, indexdt, eventdt
          FROM _event_candidates) ec
    JOIN (SELECT DISTINCT cohortgrp, criteria, cond, subcond,
                 subcond_inclusion
          FROM cfg_inclusion WHERE criteria IN ('IEV', 'EEV')) r
      ON r.cohortgrp = ec.cohortgrp
    LEFT JOIN hits h
           ON h.cohortgrp = ec.cohortgrp AND h.patid = ec.patid
          AND h.indexdt = ec.indexdt AND h.eventdt = ec.eventdt
          AND h.criteria = r.criteria
          AND h.cond    = r.cond AND h.subcond = r.subcond
    GROUP BY 1, 2, 3, 4, 5, 6, 7
),
per_cond AS (
    -- All subconditions must be satisfied for the condition to be.
    SELECT
        sc.cohortgrp, sc.patid, sc.indexdt, sc.eventdt,
        sc.criteria, sc.cond,
        CASE WHEN bool_and(sc.satisfied) THEN 1 ELSE 0 END AS cond_met
    FROM per_subcond sc
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT DISTINCT cohortgrp, patid, indexdt, eventdt
FROM per_cond
WHERE (criteria = 'IEV' AND cond_met = 0)    -- required, not satisfied
   OR (criteria = 'EEV' AND cond_met = 1);   -- forbidden, satisfied

-- First SURVIVING event per episode.
CREATE OR REPLACE TABLE first_event AS
SELECT
    ec.cohortgrp,
    ec.patid,
    ec.indexdt,
    min(ec.eventdt) AS eventdt,
    count(*)        AS numevents
FROM _event_candidates ec
LEFT JOIN event_excluded x
       ON x.cohortgrp = ec.cohortgrp AND x.patid = ec.patid
      AND x.indexdt   = ec.indexdt   AND x.eventdt = ec.eventdt
WHERE x.patid IS NULL
GROUP BY 1, 2, 3;

DROP TABLE _event_candidates;

-- Final cohort: episodes surviving the follow-up washout, with outcome
-- and person-time attached.
CREATE OR REPLACE TABLE cohort_final AS
SELECT
    m.*,
    -- SAS column names, added alongside this package's own. These are
    -- the names downstream SAS steps read off mstr
    -- (ms_finalizeptsmasterlist.sas), and mstr is the primary
    -- patient-level deliverable — a step selecting FEventDt or
    -- followuptime by name would have found nothing.
    --
    -- Added rather than renamed: the internal names are used across
    -- every other stage in this package, and a rename would be churn
    -- for no gain. The contract is that the SAS names EXIST and are
    -- correct, not that they are the only ones.
    m.cohortgrp                       AS "group",
    fe.eventdt                        AS feventdt,
    (fe.eventdt IS NOT NULL)::INTEGER AS "event",
    CASE WHEN fe.eventdt IS NOT NULL THEN 'Y' ELSE 'N' END AS event_flag,
    -- episodelength = Min(EpisodeEndDt, Enr_End) - IndexDt + 1
    -- (ms_finalizeptsmasterlist.sas:305)
    date_diff('day', m.indexdt, least(m.episodeenddt, m.enr_end)) + 1
                                      AS episodelength,
    -- EpisodeEndDt_Censor for the CENSOR table ignores the event and
    -- uses the query/DP end (line 308). The followuptime variant, which
    -- does count the event, is `followuptime` below.
    least(m.enr_end,
          coalesce(m.deathdt, DATE '9999-12-31'),
          DATE '{end_date}',
          DATE '{censor_date}')       AS episodeenddt_censor,
    date_diff('day', m.indexdt,
              least(m.enr_end,
                    coalesce(m.deathdt, DATE '9999-12-31'),
                    DATE '{end_date}',
                    DATE '{censor_date}')) + 1  AS timetocensor,
    -- followuptime = Max(0, Min(EpisodeEndDt, Enr_End, FEventDt)
    --                       - IndexDt - BLACKOUTPER - ATRISKSTART + 1)
    greatest(0,
        date_diff('day', m.indexdt,
                  least(m.episodeenddt, m.enr_end,
                        coalesce(fe.eventdt, DATE '9999-12-31')))
        - c.blackout_per - c.at_risk_start + 1)  AS followuptime,
    fe.eventdt,
    (fe.eventdt IS NOT NULL)::INTEGER AS has_event,
    -- SAS reports BOTH sum(NumEvents)=All_Events and
    -- sum(HadEvent)=Eps_wEvents (ms_cidatables.sas:128-130). They are
    -- different measures: how many events occurred, versus how many
    -- episodes had at least one. Using has_event for both made
    -- all_events identical to eps_wevents by construction, so a cohort
    -- with recurrent events under-reported them. Reported in review.
    coalesce(fe.numevents, 0)         AS numevents,
    -- person-time runs to the event, or to the end of the episode
    span_days(m.atriskindexdt, least(COALESCE(fe.eventdt, m.episodeenddt),
                                     m.episodeenddt)) AS person_days,
    CASE
        WHEN fe.eventdt IS NOT NULL           THEN 'event'
        WHEN m.episodeenddt = m.deathdt       THEN 'death'
        WHEN m.episodeenddt = m.enr_end       THEN 'disenrollment'
        WHEN m.episodeenddt = DATE '{censor_date}' THEN 'study_end'
        ELSE 'exposure_end'
    END AS exit_reason
FROM ptsmasterlist m
JOIN cfg_cohort c USING (cohortgrp)
LEFT JOIN prior_event pe
       ON pe.cohortgrp = m.cohortgrp
      AND pe.patid     = m.patid
      AND pe.indexdt   = m.indexdt
LEFT JOIN first_event fe
       ON fe.cohortgrp = m.cohortgrp
      AND fe.patid     = m.patid
      AND fe.indexdt   = m.indexdt
WHERE
    -- Follow-up washout. A NULL fup_wash_per means "never had an event"
    -- (SAS uses 99999), so it is the strictest setting rather than the
    -- loosest — any prior event at all disqualifies the episode.
    CASE
        WHEN c.fup_wash_per IS NULL THEN pe.prior_event_dt IS NULL
        WHEN c.fup_wash_per <= 0    THEN TRUE
        ELSE pe.prior_event_dt IS NULL
             OR pe.prior_event_dt < m.indexdt - c.fup_wash_per
    END
    -- Blackout: SAS builds _EventsInBlackout and drops those episodes
    -- outright (`if a and not b and not c and not d`,
    -- ms_createpov56.sas:127). Shifting the at-risk start alone ignored
    -- the event but KEPT the episode, inflating the denominator.
    AND (c.blackout_per <= 0 OR NOT EXISTS (
            SELECT 1 FROM event_claims b
            WHERE b.cohortgrp = m.cohortgrp
              AND b.patid     = m.patid
              AND b.adate BETWEEN m.indexdt
                              AND m.indexdt + c.blackout_per - 1
        ));
