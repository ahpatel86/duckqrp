/*---------------------------------------------------------------------
  ms_parity_export.sas — export the SAS side of a parity comparison.

  Run this AFTER a normal SAS QRP run, in the same session, so the
  work datasets are still available. It writes one CSV per table into
  the directory layout tools/parity_compare.py expects.

  Usage:

      %include "ms_parity_export.sas";
      %ms_parity_export(outdir=/path/to/sas_dbg, runid=&RUNID.);

  Then, on the DuckDB side:

      qrp run --study s.json --indata data/ --out results/ \
              --parity-dump /path/to/duck_dbg
      python tools/parity_compare.py /path/to/sas_dbg /path/to/duck_dbg

  Only the msoc/dplocal datasets are required. The five intermediate
  work datasets are optional and make a divergence far easier to
  localise: without them a difference in the final cohort could have
  originated in any of nine stages.

  Nothing here reads or writes patient-level data outside the SAS
  session's own libraries.
---------------------------------------------------------------------*/

%macro ms_parity_export(outdir=, runid=&RUNID., cohortvar=group);

    %if %length(&outdir.) = 0 %then %do;
        %put ERROR: outdir is required;
        %return;
    %end;

    /* One CSV per (stage, cohort, table). The cohort level exists
       because the DuckDB side partitions by cohort; a table without a
       cohort column is written once under "all".                     */
    %macro _emit(stage=, ds=, table=, bycohort=Y);

        %if %sysfunc(exist(&ds.)) = 0 %then %do;
            %put NOTE: &ds. not present - skipping (optional table);
            %return;
        %end;

        %if &bycohort. = N %then %do;
            %let _dir = &outdir./_dbg_&stage./pt001/all/iter_00/&table.;
            options dlcreatedir;
            libname _pd "&outdir.";
            %sysexec mkdir -p "&_dir.";
            proc export data=&ds.
                        outfile="&_dir./&table..csv"
                        dbms=csv replace;
            run;
        %end;
        %else %do;
            /* split by cohort so the paths match the DuckDB dump */
            proc sql noprint;
                select distinct &cohortvar. into :_cohorts separated by '|'
                from &ds.;
            quit;
            %let _n = %sysfunc(countw(%bquote(&_cohorts.), |));
            %do _i = 1 %to &_n.;
                %let _c = %scan(%bquote(&_cohorts.), &_i., |);
                %let _dir = &outdir./_dbg_&stage./pt001/&_c./iter_00/&table.;
                %sysexec mkdir -p "&_dir.";
                proc export
                    data=&ds.(where=(&cohortvar. = "&_c."))
                    outfile="&_dir./&table..csv"
                    dbms=csv replace;
                run;
            %end;
        %end;
    %mend _emit;

    /* ---- deliverables: the tables that matter most --------------- */
    %_emit(stage=pov56,        ds=DPLocal.&runid._mstr,
           table=cohort_final,  bycohort=Y);
    %_emit(stage=attrition,    ds=msoc.&runid._attrition,
           table=attrition,     bycohort=N);
    %_emit(stage=censor,       ds=msoc.&runid._censor_cida,
           table=censoring,     bycohort=N);
    %_emit(stage=cida,         ds=msoc.&runid._t2_cida,
           table=t2_cida,       bycohort=N);
    %_emit(stage=cida,         ds=DPLocal.&runid._numcounts,
           table=numcounts,     bycohort=N);
    %_emit(stage=cida,         ds=DPLocal.&runid._DenomCounts,
           table=denomcounts,   bycohort=N);
    %_emit(stage=codedist,     ds=msoc.&runid._distindex,
           table=distindex,     bycohort=N);
    %_emit(stage=codedist,     ds=msoc.&runid._distindexmap,
           table=distindexmap,  bycohort=N);
    %_emit(stage=followuptime, ds=msoc.&runid._followuptime_cida,
           table=followuptime,  bycohort=N);

    /* ---- intermediates: optional, but they localise a divergence -- */
    %_emit(stage=stockpiling,   ds=_Stockpiled,
           table=stockpiled,        bycohort=Y);
    %_emit(stage=pov1,          ds=_PotentialIndexDates,
           table=index_candidates,  bycohort=Y);
    %_emit(stage=pov1,          ds=_POV1,
           table=pov1,              bycohort=Y);
    %_emit(stage=ptsmasterlist, ds=_PtsMasterList,
           table=ptsmasterlist,     bycohort=Y);

    %put NOTE: parity export complete - &outdir.;

%mend ms_parity_export;
