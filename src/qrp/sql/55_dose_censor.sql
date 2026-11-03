-- ---------------------------------------------------------------------
-- MAXCUMDOSE censoring (ms_createpov4.sas:178-201) + the SAS-ordered
-- post-truncation length re-check (DB-5).
--
-- SAS does TWO different things with maxcumdose, and this implementation
-- previously did only the first:
--
--   1. ms_pov1dose: exclude the index date when prior cumulative dose is
--      outside [mincumdose, maxcumdose]. That is `dose_excluded`.
--   2. ms_createpov4: accumulate dose WITHIN the episode, from
--      (episodestart - cumdoseper) onward, and CENSOR the episode at the
--      expiry date of the claim that tips the running total over
--      maxcumdose.
--
-- The difference matters: excluding a patient and shortening their
-- follow-up give quite different denominators.
--
-- Order matters too, and SAS's is explicit. The truncation
--
--   if missing(censordate_maxdose)=0
--      and EpisodeEndDt > censordate_maxdose
--      then EpisodeEndDt = censordate_maxdose;
--                                    (ms_createptsmasterlist.sas:124)
--
-- PRECEDES the flag computations (:146-165), so MinEnrolMet3 (:156) and
-- MinEpiDurMet (:165) are evaluated on the POST-truncation end; the
-- subsetting IF at :185 then drops every episode failing any flag, and
-- `if IndexDt > EpisodeEndDt then delete` (:188) runs last. The
-- maximum-episode-duration truncation (:168) comes after the flags and
-- is NOT fed back into them.
--
-- Filtering on the pre-truncation end in 50_episodes.sql and truncating
-- here without re-checking got this exactly backwards in both
-- directions: episodes the censor pushed below minepisdur were kept
-- here but deleted by SAS, and episodes failing the required observable
-- days on the pre-truncation end were deleted here but kept by SAS,
-- because a shorter end needs fewer observable days after it. So this
-- stage applies the truncation to the flag inputs first, re-evaluates
-- the end-dependent flags on the truncated end, and only then rebuilds
-- the master list.
--
-- Only built when a cohort sets both maxcumdose and cumdoseper, which is
-- SAS's own gate (`&maxcumdose. ne . and &cumdoseper. ne .`). Cohorts
-- without the pair contribute no dose_censor rows, so the update and
-- rebuild below leave their flags and rows untouched.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TABLE dose_censor AS
WITH in_window AS (
    SELECT
        e.cohortgrp,
        e.patid,
        e.indexdt,
        d.adate,
        d.expiredt,
        d.cumdose,
        c.max_cum_dose
    FROM episode_criteria e
    JOIN cfg_cohort c
      ON c.cohortgrp = e.cohortgrp
     AND c.max_cum_dose IS NOT NULL
     AND c.cum_dose_per IS NOT NULL
    JOIN claim_dose d
      ON d.cohortgrp = e.cohortgrp
     AND d.patid     = e.patid
     -- SAS: (episodestartdt - cumdoseper) <= adate <= episodeenddt
     AND d.adate BETWEEN e.indexdt - c.cum_dose_per AND e.episodeenddt
),
running AS (
    SELECT
        *,
        sum(cumdose) OVER (
            PARTITION BY cohortgrp, patid, indexdt
            ORDER BY adate, expiredt
            ROWS UNBOUNDED PRECEDING
        ) AS total
    FROM in_window
)
SELECT
    cohortgrp,
    patid,
    indexdt,
    -- SAS takes min() of the expiry dates where the total is exceeded,
    -- i.e. the FIRST claim to tip it over.
    min(expiredt) AS censordate_maxdose
FROM running
WHERE round(total) > max_cum_dose
GROUP BY 1, 2, 3;

-- Apply the truncation (SAS :124) to episode_criteria in place, before
-- anything consumes its flags again. Rewriting episode_criteria rather
-- than just ptsmasterlist keeps the attrition funnel (70_outputs.sql)
-- counting the same flag evaluation the master list applies — the two
-- cannot disagree.
--
-- Everything in the SET list recomputes from the OLD episodeenddt, so
-- the UPDATE is order-independent and idempotent: least() with the
-- censor date no-ops when the episode already ends before it.
UPDATE episode_criteria AS ec SET
    episodeenddt = least(ec.episodeenddt, x.censordate_maxdose),
    episode_days = span_days(ec.indexdt,
                             least(ec.episodeenddt, x.censordate_maxdose)),
    -- MinEnrolMet (:146) and MinEnrolMet2 (:153) are index-anchored;
    -- FUEnrolMet (:171-174) is too. MinEnrolMet3 (:156) moves with the
    -- end, so the conjunction is re-evaluated as a whole.
    min_enr_aft_met = (
        ec.dataavail_dt >= ec.indexdt + greatest(0, c.req_days_aft_ind)
        AND ec.dataavail_dt >= least(ec.episodeenddt, x.censordate_maxdose)
                              + greatest(0, c.req_days_aft_epi)
        AND (c.min_epis_dur <= 0
             OR (ec.enr_start <= ec.indexdt
                 AND ec.indexdt + (c.min_epis_dur - 1) <= ec.enr_end))
    ),
    -- MinEpiDurMet (:165) on the post-truncation end
    min_epi_dur_met = span_days(ec.indexdt,
                                least(ec.episodeenddt, x.censordate_maxdose))
                      >= c.min_epis_dur,
    -- the blackout check is end-dependent in this port; re-evaluate it
    -- on the same truncated end so one end drives every flag after :124
    post_blackout_met = (c.blackout_per <= 0
                         OR date_diff('day', ec.indexdt,
                              least(ec.episodeenddt, x.censordate_maxdose))
                             - c.blackout_per >= 0)
FROM dose_censor x
JOIN cfg_cohort c ON c.cohortgrp = x.cohortgrp
WHERE x.cohortgrp = ec.cohortgrp
  AND x.patid     = ec.patid
  AND x.indexdt   = ec.indexdt;

-- The subsetting IF (:185) over the re-evaluated flags, plus the
-- defensive :188 delete on the truncated end. Rebuilt from
-- episode_criteria rather than filtered in place: an episode whose ONLY
-- stage-50 failure was an end-dependent flag that the truncation
-- satisfies must come back, exactly as SAS's order keeps it.
--
-- No maximum-episode-duration term appears here. In SAS the :168
-- truncation runs after the flags and is not fed back into them; in
-- this port the cap is applied upstream (inside 50_episodes.sql's
-- censor chain) and nothing re-truncates or re-checks after this
-- rebuild.
CREATE OR REPLACE TABLE ptsmasterlist AS
SELECT * EXCLUDE (episodeenddt, episode_days,
                  min_enr_days_met, min_enr_aft_met, min_day_sup_met,
                  min_epi_dur_met, post_blackout_met),
       -- re-added last to hold the column order every earlier stage of
       -- this package produced, so deliverable schemas do not move
       episodeenddt,
       episode_days
FROM episode_criteria ec
WHERE min_enr_days_met AND min_enr_aft_met AND min_day_sup_met
  AND min_epi_dur_met AND post_blackout_met
  -- if IndexDt > EpisodeEndDt then delete (:188)
  AND indexdt <= episodeenddt
  -- the inclusion filter (52_inclusion.sql) applied against the
  -- candidate set this rebuild starts from, not just the survivors
  AND NOT EXISTS (
      SELECT 1
      FROM {incl_rel} x
      WHERE x.cohortgrp = ec.cohortgrp
        AND x.patid     = ec.patid
        AND x.indexdt   = ec.indexdt
  );
