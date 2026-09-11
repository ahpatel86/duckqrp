-- =====================================================================
-- 47_geography.sql — geographic variables (ms_geographicvars.sas).
--
-- Adds zip3, state, HHS region, Census-Bureau region and a social
-- deprivation index to each episode, from a ZIP lookup file.
--
-- Two details worth stating, because both are easy to get subtly wrong:
--
-- 1. The 'Unknown' rules CASCADE. A missing zip OR an unmatched
--    statecode makes all four geography variables Unknown — not just
--    the one that failed to map. And cb_reg is additionally Unknown
--    when the lookup says "other", which is a value, not a NULL.
--
-- 2. `zip_uncertain` defaults to 'Y'. A missing zip_date means
--    uncertain, and so does an index date BEFORE the zip was recorded —
--    the address on file postdates the event, so it may not be where
--    the patient lived at the time. Only a zip_date on or before the
--    index date gives 'N'.
--
-- 3. "Missing zip" follows SAS's `missing()`, which on a character
--    variable is true for NULL *and* for blank. Real SCDM postalcode
--    uses '' rather than NULL, so `IS NULL` alone would treat an empty
--    zip as present. Here the two happen to coincide (an empty zip also
--    fails the lookup join, so statecode is NULL either way), but the
--    coincidence is not something to rely on.
--
-- The lookup is optional: with no zipfile supplied every episode gets
-- Unknown, which is what SAS's LEFT JOIN produces against an empty
-- lookup.
-- =====================================================================

CREATE OR REPLACE TABLE geography AS
SELECT
    m.cohortgrp,
    m.patid,
    m.indexdt,
    m.zip,
    -- All four cascade off the same two conditions.
    CASE WHEN is_missing(m.zip) OR z.statecode IS NULL THEN 'Unknown'
         ELSE substr(trim(m.zip), 1, 3) END                  AS zip3,
    CASE WHEN is_missing(m.zip) OR z.statecode IS NULL THEN 'Unknown'
         ELSE z.statecode END                                AS state,
    CASE WHEN is_missing(m.zip) OR z.statecode IS NULL
              OR z.hhs_region IS NULL THEN 'Unknown'
         ELSE z.hhs_region END                               AS hhs_reg,
    CASE WHEN is_missing(m.zip) OR z.statecode IS NULL
              OR z.cb_region IS NULL
              OR lower(z.cb_region) = 'other' THEN 'Unknown'
         ELSE z.cb_region END                                AS cb_reg,
    CASE WHEN is_missing(m.zip) THEN NULL ELSE z.sdi END         AS sdi,
    -- Quartiles of the social deprivation index; '5' is unknown.
    CASE WHEN is_missing(m.zip)          THEN '5'
         WHEN z.sdi >=  0 AND z.sdi < 25 THEN '1'
         WHEN z.sdi >= 25 AND z.sdi < 50 THEN '2'
         WHEN z.sdi >= 50 AND z.sdi < 75 THEN '3'
         WHEN z.sdi >= 75 AND z.sdi <= 100 THEN '4'
         ELSE '5' END                                        AS sdi_cat,
    -- Uncertain unless the address predates the index date.
    CASE WHEN m.zip_date IS NULL       THEN 'Y'
         WHEN m.indexdt < m.zip_date   THEN 'Y'
         ELSE 'N' END                                        AS zip_uncertain
FROM ptsmasterlist m
LEFT JOIN cfg_zipfile z
       ON z.zip = m.zip;
