-- Dispensing claims with dose attached. A VIEW, defined unconditionally
-- because three stages consume it: dose restrictions (42_dose.sql), dose
-- censoring (55_dose_censor.sql), and the per-subcondition dose
-- thresholds on inclusion rules (52_inclusion.sql, 60_followup.sql).
--
-- It was previously defined inside 42_dose.sql and so existed only when
-- a cohort set a dose limit. Adding the third consumer broke at runtime
-- — the same failure covar_source had for the same reason. A view
-- materialises nothing, so defining it always is free.
CREATE OR REPLACE VIEW claim_dose AS
SELECT
    e.cohortgrp,
    e.patid,
    e.adate,
    -- SAS orders the cumulative-dose accumulation by (adate, expiredt),
    -- so the supply end has to be carried even though the exclusion
    -- path does not use it.
    e.adate + CAST(e.rxsup - 1 AS INTEGER) AS expiredt,
    e.rxsup,
    e.rxamt,
    -- exposure_claims is pre-collapse, so one row IS one dispensing.
    -- SAS's aFDD divides by sum(numdispensing) over the window; at this
    -- grain that is the claim count.
    1                                          AS numdispensing,
    s.strength,
    s.strength * e.rxamt                       AS cumdose,
    CASE WHEN e.rxsup > 0
         THEN s.strength * e.rxamt / e.rxsup
         ELSE NULL END                         AS cfdd
FROM exposure_claims e
JOIN cfg_code_strength s
  ON s.code = e.code;
