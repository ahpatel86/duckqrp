-- =====================================================================
-- 45_pov1.sql — POV1: demographics, age strata and enrollment.
--
-- Deliberately AFTER 42_dose.sql. The dose stage rewrites
-- `index_candidates` in place, so POV1 must be built from the filtered
-- set. Having POV1 in the same file as index detection was a real bug:
-- dose exclusions were computed and then silently ignored, because POV1
-- had already been materialised from the pre-filter table. It surfaced
-- only when a study with an actual dose restriction was run end to end.
--
-- Stage ordering is the kind of thing that is invisible until it isn't,
-- which is the argument for stage boundaries being explicit objects
-- rather than incidental to where a CREATE TABLE happens to sit.
-- =====================================================================

-- ---------------------------------------------------------------------
-- POV1, in three passes rather than one 10-way join.
--
-- Written as a single statement this was the stage that set the memory
-- floor: ten joins, two of them range joins (enrollment containment and
-- the age-stratum band), means DuckDB builds many hash tables
-- concurrently and the peak is the SUM of them. Splitting it means the
-- peak is the LARGEST of them, and each intermediate is narrower than
-- the one before because filtering happens earlier.
--
-- Same materialisation argument as everywhere else in this package, but
-- for memory rather than plan size: a boundary is where you choose to
-- stop holding several things at once.
-- ---------------------------------------------------------------------

-- Pass 1: equi-join to demographics and apply demographic eligibility.
-- The cfg_demog tables are tiny, so these are cheap hash probes, and
-- they cut the row count before the expensive range joins run.
CREATE OR REPLACE TEMP TABLE _pov1_demog AS
SELECT
    i.cohortgrp, i.patid, i.adate AS indexdt, i.expiredt,
    i.rxsup, i.rxamt, i.numdispensing,
    dm.birth_date, dm.sex, dm.race, dm.hispanic, dm.zip, dm.zip_date
FROM index_candidates i
JOIN demographics dm ON dm.patid = i.patid
LEFT JOIN demographics_multi   mx ON mx.patid = i.patid
LEFT JOIN demographics_missing ms ON ms.patid = i.patid
JOIN cfg_demog ds ON ds.cohortgrp = i.cohortgrp AND ds.dimension = 'sex'
                 AND ds.value = dm.sex_raw
JOIN cfg_demog dr ON dr.cohortgrp = i.cohortgrp AND dr.dimension = 'race'
                 AND dr.value = dm.race
JOIN cfg_demog dh ON dh.cohortgrp = i.cohortgrp AND dh.dimension = 'hispanic'
                 AND dh.value = dm.hispanic
WHERE mx.patid IS NULL AND ms.patid IS NULL;

-- Pass 2: the enrollment containment range join, on the reduced set.
CREATE OR REPLACE TEMP TABLE _pov1_enrolled AS
SELECT d.*, e.enr_start, e.enr_end
FROM _pov1_demog d
JOIN cfg_cohort c ON c.cohortgrp = d.cohortgrp
JOIN enrollment_spans e
  ON e.enr_cfg_id = c.enr_cfg_id
 AND e.patid      = d.patid
 AND d.indexdt BETWEEN e.enr_start AND e.enr_end;

DROP TABLE _pov1_demog;

-- Pass 3: age stratum band join, plus death lookup.
CREATE OR REPLACE TABLE pov1 AS
SELECT
    d.cohortgrp, d.patid, d.indexdt, d.expiredt,
    d.rxsup, d.rxamt, d.numdispensing,
    d.enr_start, d.enr_end,
    d.birth_date, d.sex, d.race, d.hispanic, d.zip, d.zip_date,
    age_years(d.birth_date, d.indexdt) AS age,
    a.label   AS agegroup,
    a.ordinal AS agegroupnum,
    dth.deathdt
FROM _pov1_enrolled d
JOIN cfg_age_strata a
  ON a.cohortgrp = d.cohortgrp
 AND age_in_unit(d.birth_date, d.indexdt, a.unit) BETWEEN a.lo AND a.hi
LEFT JOIN deaths dth ON dth.patid = d.patid;

DROP TABLE _pov1_enrolled;
