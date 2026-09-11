-- =====================================================================
-- 50_episodes.sql — %ms_createclaimepi + %ms_createpov4 +
--                   %ms_createptsmasterlist.
--
-- Exposure episodes are chains of dispensings separated by no more than
-- `episode_gap` days; the episode then gets extended, truncated and
-- censored. The PySpark port split this across three modules and four
-- localCheckpoint calls. It is one gap-and-islands pass followed by one
-- projection, so it is one stage here.
-- =====================================================================

CREATE OR REPLACE TABLE episodes AS
WITH located AS (
    -- The enrollment span covering each claim. SAS carries Enr_Start on
    -- the claim and breaks the episode when it changes
    -- (ms_createclaimepi.sas:75, `or LEnrStartDt ne Enr_Start`), so a
    -- patient who disenrols and re-enrols gets TWO episodes, not one.
    -- Without this, chaining ran straight through a coverage gap.
    --
    -- LEFT JOIN, not JOIN: a claim outside any enrollment span must
    -- still take part in chaining (it is the master list that enforces
    -- enrollment), and NULL is a distinct span for break purposes.
    SELECT
        s.*,
        e.enr_start AS claim_enr_start
    FROM stockpiled s
    JOIN cfg_cohort c2
      ON c2.cohortgrp = s.cohortgrp
    LEFT JOIN enrollment_spans e
      ON e.enr_cfg_id = c2.enr_cfg_id
     AND e.patid      = s.patid
     AND s.adate BETWEEN e.enr_start AND e.enr_end
),
claims AS (
    SELECT
        s.cohortgrp,
        s.patid,
        s.adate,
        s.expiredt,
        s.rxsup,
        s.claim_enr_start,
        c.episode_gap,
        c.episode_gap_type,
        c.exp_ext_per,
        -- runout: the latest supply end among all previous claims
        max(s.expiredt) OVER (
            PARTITION BY s.cohortgrp, s.patid
            ORDER BY s.adate, s.expiredt, s.rxsup
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prev_runout,
        lag(s.rxsup)    OVER w AS prev_rxsup,
        lag(s.claim_enr_start) OVER w AS prev_enr_start
    FROM located s
    JOIN cfg_cohort c USING (cohortgrp)
    WHERE NOT c.point
    WINDOW w AS (
        PARTITION BY s.cohortgrp, s.patid
        ORDER BY s.adate, s.expiredt, s.rxsup
    )
),
flagged AS (
    SELECT
        *,
        date_diff('day', prev_runout, adate) - 1 AS gap_days,
        CASE
            WHEN prev_runout IS NULL THEN 1
            WHEN episode_gap IS NULL THEN 1
            -- gap type 'P': threshold is a percentage of the prior supply
            WHEN episode_gap_type = 'P'
                 THEN CASE WHEN date_diff('day', prev_runout, adate) - 1
                              > (episode_gap * prev_rxsup / 100.0)
                           THEN 1 ELSE 0 END
            ELSE CASE WHEN date_diff('day', prev_runout, adate) - 1 > episode_gap
                      THEN 1 ELSE 0 END
        END
        -- ... OR the enrollment span changed (SAS: LEnrStartDt ne
        -- Enr_Start). IS DISTINCT FROM so a NULL span on either side
        -- counts as a change rather than swallowing the comparison.
        | CASE WHEN prev_runout IS NOT NULL
                AND claim_enr_start IS DISTINCT FROM prev_enr_start
               THEN 1 ELSE 0 END AS is_break
    FROM claims
),
numbered AS (
    SELECT
        *,
        sum(is_break) OVER (
            PARTITION BY cohortgrp, patid
            ORDER BY adate, expiredt, rxsup
            ROWS UNBOUNDED PRECEDING
        ) AS episode
    FROM flagged
)
SELECT
    cohortgrp,
    patid,
    episode,
    min(adate)    AS episodestartdt,
    -- expextper extends the observed supply end
    max(expiredt) + any_value(exp_ext_per) AS episodeenddt,
    sum(rxsup)    AS episode_rxsup
FROM numbered
GROUP BY 1, 2, 3;

-- Point-exposure cohorts: one episode per index date, no chaining.
INSERT INTO episodes
SELECT
    p.cohortgrp,
    p.patid,
    row_number() OVER (PARTITION BY p.cohortgrp, p.patid
                       ORDER BY p.indexdt, p.expiredt, p.rxsup),
    p.indexdt,
    CASE WHEN c.max_epis_dur >= 1
         THEN p.indexdt + (c.max_epis_dur - 1)
         ELSE p.enr_end END,
    p.rxsup
FROM pov1 p
JOIN cfg_cohort c USING (cohortgrp)
WHERE c.point;

-- ---------------------------------------------------------------------
-- Patient master list: one row per qualifying (cohort, patient, index).
--
-- Censoring is a chain of `least()` calls rather than the seven
-- sequential conditional withColumn overwrites the PySpark port used.
-- Written as a single projection, the precedence is visible in one place
-- instead of being spread over 60 lines of imperative reassignment.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TABLE ptsmasterlist AS
WITH joined AS (
    SELECT
        p.* EXCLUDE (cohortgrp),
        p.cohortgrp,
        e.episode,
        e.episodeenddt AS raw_episodeenddt,
        e.episode_rxsup,
        c.enr_days,
        c.min_epis_dur,
        c.max_epis_dur,
        c.min_days_supp,
        c.at_risk_start,
        c.blackout_per,
        c.req_days_aft_ind,
        c.req_days_aft_epi,
        c.censor_death
    FROM pov1 p
    JOIN episodes e
      ON e.cohortgrp = p.cohortgrp
     AND e.patid     = p.patid
     AND e.episodestartdt = p.indexdt
    JOIN cfg_cohort c
      ON c.cohortgrp = p.cohortgrp
),
censored AS (
    SELECT
        *,
        raw_episodeenddt AS origepisenddt,
        -- Censoring precedence, innermost first:
        --   study censor date, death (when censoring on death),
        --   end of enrollment, max episode duration.
        least(
            raw_episodeenddt,
            DATE '{censor_date}',
            enr_end,
            CASE WHEN censor_death THEN COALESCE(deathdt, DATE '9999-12-31')
                 ELSE DATE '9999-12-31' END,
            CASE WHEN max_epis_dur > 0 THEN indexdt + (max_epis_dur - 1)
                 ELSE DATE '9999-12-31' END
        ) AS episodeenddt,
        -- the last date on which data is available for this patient
        least(
            enr_end,
            DATE '{censor_date}',
            CASE WHEN censor_death THEN COALESCE(deathdt, DATE '9999-12-31')
                 ELSE DATE '9999-12-31' END
        ) AS dataavail_dt
    FROM joined
)
SELECT
    cohortgrp, patid, indexdt, episode,
    indexdt AS episodestartdt,
    episodeenddt,
    origepisenddt,
    CASE WHEN at_risk_start > 0 THEN indexdt + at_risk_start ELSE indexdt END
        AS atriskindexdt,
    dataavail_dt,
    span_days(indexdt, episodeenddt) AS episode_days,
    age, agegroup, agegroupnum, sex, race, hispanic, zip, zip_date,
    birth_date, deathdt, enr_start, enr_end,
    rxsup, rxamt, numdispensing, episode_rxsup
FROM censored
WHERE
    -- minimum prior enrollment
        enr_start <= indexdt - enr_days
    -- required observable days after index and after episode end
    AND dataavail_dt >= indexdt      + greatest(0, req_days_aft_ind)
    AND dataavail_dt >= episodeenddt + greatest(0, req_days_aft_epi)
    -- minimum episode duration, and enrollment covering it
    AND span_days(indexdt, episodeenddt) >= min_epis_dur
    AND (min_epis_dur <= 0
         OR (enr_start <= indexdt
             AND indexdt + (min_epis_dur - 1) <= enr_end))
    -- blackout must fit inside the episode
    AND (blackout_per <= 0
         OR date_diff('day', indexdt, episodeenddt) - blackout_per >= 0)
    -- minimum days of supply within the episode
    AND (min_days_supp <= 0 OR episode_rxsup >= min_days_supp)
    AND indexdt <= episodeenddt;
