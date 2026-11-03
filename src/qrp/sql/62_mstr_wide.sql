-- ------------------------------------------------------------------
-- Widen the master list to SAS's `<runid>_mstr` shape.
--
-- SAS's mstr is ONE WIDE ROW PER EPISODE carrying everything: the
-- covariate flags, the utilization counts, the comorbidity index and
-- the censoring flags all live on it. This package computes each of
-- those and wrote them to `covariates`, `utilization` and
-- `risk_scores` instead, so a data partner opening `<runid>_mstr`
-- found 53 columns absent even though the values existed elsewhere in
-- the output.
--
-- Columns whose source this package does not model are NOT invented:
-- they are omitted, so their absence stays visible rather than being
-- papered over with a zero.
-- ------------------------------------------------------------------
-- ------------------------------------------------------------------
-- The index-code distribution (distindexexp / distindexhoi) is defined
-- ONCE, in 72_codedistribution.sql; {di_rel} is its per-episode table,
-- or an empty relation when that stage did not run.

CREATE OR REPLACE TABLE cohort_final AS
SELECT
    -- SAS leaves numevents blank, not 0, on an episode with no event;
    -- sums over it (t2_cida's all_events) default to 0 explicitly
    f.* REPLACE (nullif(f.numevents, 0) AS numevents),
    -- Calendar parts of the index date. SAS carries all three.
    year(f.indexdt)::SMALLINT                      AS "year",
    month(f.indexdt)::SMALLINT                     AS "month",
    -- quarter(), not ((month - 1) / 3 + 1)::SMALLINT: `/` is float
    -- division in DuckDB and the cast ROUNDS, so March gave 1.67 -> 2 and
    -- every Mar/Jun/Sep/Dec index date was reported a quarter late
    quarter(f.indexdt)::SMALLINT                   AS quarter,
    -- One surveillance period is modelled, so PeriodID is always 1 and
    -- the index look-end is the query period end.
    1::SMALLINT                                    AS "PeriodID",
    DATE '{end_date}'                              AS "IndexLookEndDt",
    -- Dispensing counts and totals for the episode.
    -- counted once, with supply and amount, in 60_followup.sql
    f.episode_rawdisp::INTEGER                     AS "RawDisp",
    f.episode_adjusteddisp::INTEGER                AS "AdjustedDisp",
    -- supply and amount clipped to the episode window, as SAS
    f.episode_totrxsup::INTEGER                    AS "TotRxSup",
    f.episode_totrxamt                             AS "TotRxAmt",
    coalesce(di.distindexexp, '')                 AS "distindexexp",
    coalesce(di.distindexhoi, '')                 AS "distindexhoi",
    -- SAS carries death fields into the master list only for Type 5
    -- (ms_createpov1.sas keeps them when type = 5); for Type 2 they are
    -- empty strings on every row, though the deaths themselves are used
    -- for censoring
    ''::VARCHAR                                    AS "death_source",
    ''::VARCHAR                                    AS "death_enctype",
    -- follow-up length category, from followuptime (matches SAS on
    -- every wp307 episode); Censorcat_sort is its position
    CASE WHEN f.followuptime <= 90 THEN '0-90'
         WHEN f.followuptime <= 180 THEN '91-180'
         ELSE '181+' END                           AS "fupdays_value_cat",
    CASE WHEN f.followuptime <= 90 THEN 1
         WHEN f.followuptime <= 180 THEN 2
         ELSE 3 END::DOUBLE                        AS "Censorcat_sort",
    -- follow-up and censoring flags: defined once, in 60_followup.sql
    ef.* EXCLUDE (cohortgrp, patid, indexdt),
    -- ttc is computed only for TreeScan analyses (analysis = "tree",
    -- ms_finalizeptsmasterlist.sas:360-369); otherwise SAS leaves it
    -- missing. Not the same as timetocensor.
    NULL::INTEGER                                  AS ttc{mstr_extra}
FROM cohort_final f
LEFT JOIN episode_flags ef
  ON ef.cohortgrp = f.cohortgrp AND ef.patid = f.patid AND ef.indexdt = f.indexdt{mstr_joins}
LEFT JOIN {di_rel} di
  ON di.cohortgrp = f.cohortgrp AND di.patid = f.patid AND di.indexdt = f.indexdt;
