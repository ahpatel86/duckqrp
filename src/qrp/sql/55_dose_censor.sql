-- ---------------------------------------------------------------------
-- MAXCUMDOSE censoring (ms_createpov4.sas:178-201).
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
-- Only built when a cohort sets both maxcumdose and cumdoseper, which is
-- SAS's own gate (`&maxcumdose. ne . and &cumdoseper. ne .`).
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

-- Apply, then re-check the criteria that depend on episode length.
-- Truncating can push an episode below min_epis_dur or past the
-- required observable days, exactly as it can in SAS.
UPDATE episode_criteria AS ec SET
    episodeenddt = least(ec.episodeenddt, x.censordate_maxdose),
    episode_days = span_days(ec.indexdt,
                             least(ec.episodeenddt, x.censordate_maxdose)),
    min_enr_aft_met = (
        ec.dataavail_dt >= ec.indexdt + greatest(0, c.req_days_aft_ind)
        AND ec.dataavail_dt >= least(ec.episodeenddt, x.censordate_maxdose)
                              + greatest(0, c.req_days_aft_epi)
        AND (c.min_epis_dur <= 0
             OR (ec.enr_start <= ec.indexdt
                 AND ec.indexdt + (c.min_epis_dur - 1) <= ec.enr_end))
    ),
    min_epi_dur_met = span_days(ec.indexdt,
                                least(ec.episodeenddt, x.censordate_maxdose))
                      >= c.min_epis_dur,
    post_blackout_met = (c.blackout_per <= 0
                         OR date_diff('day', ec.indexdt,
                              least(ec.episodeenddt, x.censordate_maxdose))
                             - c.blackout_per >= 0)
FROM dose_censor x
JOIN cfg_cohort c ON c.cohortgrp = x.cohortgrp
WHERE x.cohortgrp = ec.cohortgrp
  AND x.patid     = ec.patid
  AND x.indexdt   = ec.indexdt;

CREATE OR REPLACE TABLE ptsmasterlist AS
SELECT * EXCLUDE (episodeenddt, episode_days,
                  min_enr_days_met, min_enr_aft_met, min_day_sup_met,
                  min_epi_dur_met, post_blackout_met),
       episodeenddt,
       episode_days
FROM episode_criteria ec
WHERE min_enr_days_met AND min_enr_aft_met AND min_day_sup_met
  AND min_epi_dur_met AND post_blackout_met
  AND indexdt <= episodeenddt
  AND NOT EXISTS (
      SELECT 1
      FROM {incl_rel} x
      WHERE x.cohortgrp = ec.cohortgrp
        AND x.patid     = ec.patid
        AND x.indexdt   = ec.indexdt
  );
