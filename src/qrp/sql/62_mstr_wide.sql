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
CREATE OR REPLACE TABLE cohort_final AS
SELECT
    f.*,
    -- Calendar parts of the index date. SAS carries all three.
    year(f.indexdt)::SMALLINT                      AS "year",
    month(f.indexdt)::SMALLINT                     AS "month",
    ((month(f.indexdt) - 1) / 3 + 1)::SMALLINT     AS quarter,
    -- One surveillance period is modelled, so PeriodID is always 1 and
    -- the index look-end is the query period end.
    1::SMALLINT                                    AS "PeriodID",
    DATE '{end_date}'                              AS "IndexLookEndDt",
    -- Dispensing counts and totals for the episode.
    f.numdispensing::INTEGER                       AS "RawDisp",
    f.numdispensing::INTEGER                       AS "AdjustedDisp",
    f.episode_rxsup::INTEGER                       AS "TotRxSup",
    f.rxamt                                        AS "TotRxAmt",
    -- Censoring flags. SAS writes one per reason, and `fup_*` and
    -- `cens_*` are the same flags under two names
    -- (ms_finalizeptsmasterlist.sas:394).
    CASE WHEN f.exit_reason = 'disenrollment' THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_elig,
    CASE WHEN f.exit_reason = 'death'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_dth,
    CASE WHEN f.exit_reason = 'query_end'     THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_qryend,
    CASE WHEN f.exit_reason = 'data_end'      THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_dpend,
    CASE WHEN f.exit_reason = 'exposure_end'  THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_episend,
    CASE WHEN f.episodeenddt_censor IS NOT NULL
              AND f.episodeenddt = f.episodeenddt_censor
         THEN 1 ELSE 0 END::SMALLINT              AS fup_spec,
    CASE WHEN f.exit_reason = 'event'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS fup_event,
    -- `cens_*` are the SAME flags under a second name: SAS renames
    -- them wholesale (ms_finalizeptsmasterlist.sas:394), and both sets
    -- appear on the master list.
    CASE WHEN f.exit_reason = 'disenrollment' THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_elig,
    CASE WHEN f.exit_reason = 'death'         THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_dth,
    CASE WHEN f.exit_reason = 'query_end'     THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_qryend,
    CASE WHEN f.exit_reason = 'data_end'      THEN 1 ELSE 0 END::SMALLINT
                                                   AS cens_dpend,
    -- SAS's short name for the same quantity.
    f.timetocensor                                 AS ttc{mstr_extra}
FROM cohort_final f;
