-- Extracted so it can be rebuilt AFTER combo covariates are
-- inserted. Built inside 80_covariates.sql only, it counted the
-- base covariates and silently omitted every combo — 17 of 49 in
-- the real study seen. Reported in review.
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
