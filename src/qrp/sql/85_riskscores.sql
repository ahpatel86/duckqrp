-- =====================================================================
-- 85_riskscores.sql — weighted comorbidity scores (ms_computeriskscores).
--
-- A risk score is a weighted sum over CONDITIONS, not over claims.
-- ms_computeriskscores.sas:369 takes `max(weight)` per
-- (group, PatId, IndexDt, condidnum) BEFORE summing, so a patient
-- meeting a condition ten times scores it once — and at its highest
-- weight, which matters when several codes map to one condition with
-- different weights.
--
-- Then (line 396):
--     score = sum(over distinct conditions) + intercept
--
-- and (line 407) a patient matching nothing still gets the intercept,
-- not NULL. That last rule is why the join is a LEFT one from the
-- master list.
--
-- Structure, following the two-stage aggregation SAS uses:
--   1. match claims to codes within the anchor window
--   2. max(weight) per (episode, condition)
--   3. sum across conditions, add the intercept
--
-- Demographic (`codecat = 'DM'`) rows contribute by sex or age group
-- rather than by a claim, and are unioned into the same condition set.
-- =====================================================================

-- Written as CTEs, not temp tables: none of these intermediates has
-- more than two joins, so there is no memory argument for materialising
-- them and the barrier would cost a write and a read for nothing. (The
-- denominator stage IS materialised, because its passes are wide.)
CREATE OR REPLACE TABLE risk_scores AS
WITH intercept AS (
    -- A constant per score, and the floor for anyone matching nothing.
    SELECT riskscore, coalesce(sum(weight), 0) AS intercept
    FROM cfg_risk_codes
    WHERE is_intercept
    GROUP BY 1
),
hits AS (
    -- Claim-based conditions: one row per (episode, score, condition)
    -- carrying the HIGHEST weight for that condition.
    SELECT
        m.cohortgrp, m.patid, m.indexdt,
        r.riskscore, r.condid,
        max(r.weight) AS weight
    FROM ptsmasterlist m
    JOIN cfg_risk_codes r
      ON NOT r.is_intercept
     AND r.codecat IN ('DX', 'PX', 'RX')
    JOIN covar_source s
      ON s.patid   = m.patid
     AND s.code    = r.code
     AND s.codecat = CASE WHEN r.codecat = 'RX' THEN 'RX' ELSE 'DX' END
     AND periods_overlap(
            -- Each end anchors independently
            -- (ms_computeriskscores.sas:107-117), the same mechanism the
            -- inclusion rules and covariate windows use. Found by
            -- grepping every *anchor column in the SAS after the
            -- covariate stage turned out to have the identical defect.
            CASE WHEN r.riskfromanchor = 'EPISODEENDDT'
                 THEN m.episodeenddt ELSE m.indexdt END + r.riskfrom,
            CASE WHEN r.risktoanchor = 'EPISODEENDDT'
                 THEN m.episodeenddt ELSE m.indexdt END + r.riskto,
            s.adate,
            CASE WHEN r.codecat = 'RX' THEN s.expiredt ELSE s.adate END
         )
    GROUP BY 1, 2, 3, 4, 5
),
demog AS (
    -- Demographic conditions match a sex or age group rather than a
    -- claim (ms_computeriskscores.sas:301-347).
    SELECT
        m.cohortgrp, m.patid, m.indexdt,
        r.riskscore, r.condid,
        max(r.weight) AS weight
    FROM ptsmasterlist m
    JOIN cfg_risk_codes r
      ON r.codecat = 'DM'
     AND NOT r.is_intercept
     AND (upper(r.code) = upper(m.sex) OR upper(r.code) = upper(m.agegroup))
    GROUP BY 1, 2, 3, 4, 5
),
summed AS (
    SELECT cohortgrp, patid, indexdt, riskscore, sum(weight) AS total
    FROM (SELECT * FROM hits UNION ALL SELECT * FROM demog)
    GROUP BY 1, 2, 3, 4
)
-- LEFT from the master list, so an episode matching no condition still
-- scores the intercept rather than vanishing.
SELECT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    i.riskscore,
    coalesce(s.total, 0) + i.intercept AS score
FROM ptsmasterlist m
CROSS JOIN intercept i
LEFT JOIN summed s
       ON s.cohortgrp = m.cohortgrp
      AND s.patid     = m.patid
      AND s.indexdt   = m.indexdt
      AND s.riskscore = i.riskscore;

-- Distribution per cohort and score, for the output tables.
CREATE OR REPLACE TABLE risk_score_summary AS
SELECT
    cohortgrp,
    riskscore,
    count(*)                AS episodes,
    round(avg(score), 3)    AS mean_score,
    min(score)              AS min_score,
    round(median(score), 3) AS median_score,
    max(score)              AS max_score
FROM risk_scores
GROUP BY 1, 2
ORDER BY 1, 2;
