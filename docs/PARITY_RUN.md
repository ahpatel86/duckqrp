# Running a parity comparison

What I need from the SAS side, and exactly how to produce it.

---

## The short version

One SAS run and one DuckDB run of **the same study on the same data**,
then one command.

```sas
/* after a normal SAS QRP run, in the same session */
%include "tools/ms_parity_export.sas";
%ms_parity_export(outdir=/path/to/sas_dbg, runid=&RUNID.);
```

```bash
qrp run --study same_study.json --indata same_data/ \
        --out results/ --parity-dump /path/to/duck_dbg

python tools/parity_compare.py /path/to/sas_dbg /path/to/duck_dbg
```

Exit 0 means no differences. Exit 1 lists them, per table and column,
with an example key for each.

---

## What has to match

**The same study file.** Not an equivalent one — the same JSON, so the
cohort definitions, code lists, windows and USERSTRATA levels are
identical.

**The same input data.** The same SCDM extract, same date range. If SAS
ran against a refresh and DuckDB against a different one, every row
count will differ and the comparison tells you nothing.

**The same `runid`**, so the output dataset names line up.

That is the whole requirement. No special SAS build, no debug flags, no
re-run with different options.

---

## What to send, in priority order

### 1. The deliverables (required)

These are where a difference actually matters:

| SAS dataset | why |
|---|---|
| `DPLocal.&RUNID._mstr` | the cohort itself — one row per episode |
| `msoc.&RUNID._attrition` | where members and episodes were lost |
| `msoc.&RUNID._t2_cida` | the study's headline numbers |
| `DPLocal.&RUNID._numcounts` | CIDA numerators |
| `DPLocal.&RUNID._DenomCounts` | CIDA denominators |
| `msoc.&RUNID._censor_cida` | time to censoring |
| `msoc.&RUNID._distindex` / `_distindexmap` | code distribution |
| `msoc.&RUNID._followuptime_cida` | only if the study requests it |

`ms_parity_export.sas` writes all of them and skips any that a
particular study does not produce.

### 2. The intermediates (optional, but worth a lot)

`_Stockpiled`, `_PotentialIndexDates`, `_POV1`, `_PtsMasterList`.

These only exist in the SAS session's WORK library, so they have to be
exported during the run rather than afterwards.

**Why they are worth the trouble:** without them, a difference in the
final cohort could have originated in any of nine stages, and finding
which one means bisecting by hand. With them, the comparison reports the
FIRST stage that diverges, which usually identifies the defect directly.

---

## If a full export is difficult

A comparison of **`_mstr` alone** is still worth running. It is one row
per episode with every derived date and flag, so it exercises
enrollment, exposure, stockpiling, episode construction, inclusion
rules, censoring and event attribution all at once. A clean `_mstr`
comparison is strong evidence; a divergent one localises to a stage
quickly even without the intermediates.

If even that is not possible, **row counts per cohort** from `_mstr` and
`_attrition` would catch the largest class of error.

---

## Disclosure

The exported CSVs contain the same patient-level data as the SAS
datasets they come from — `_mstr` has one row per patient per index
date. **They are dplocal-equivalent and must not leave the site.**

Run `parity_compare.py` at the site. Its output is counts and column
names, plus up to twenty example KEY VALUES per table. If the key
includes `patid`, redact that column before sending the report onward:

```bash
python tools/parity_compare.py sas_dbg/ duck_dbg/ > report.txt
sed -E "s/'[0-9]{6,}'/'<patid>'/g" report.txt > report_safe.txt
```

For the aggregate tables — attrition, t2_cida, censoring, the code
distribution — the keys are cohort names and stratum labels, and the
report can be shared as-is.

---

## What the report looks like

```
tables: 14 SAS, 14 DuckDB, 14 compared

MISSING COLUMN  .../attrition.csv: ['claim_level']
ROW COUNT       .../cohort_final.csv: sas=64,701 duck=64,663 (diff -38)
KEY MISMATCH    .../cohort_final.csv: 38 keys only in SAS,
                e.g. [('drug_a','10241','2012-03-04')]
VALUE           .../cohort_final.csv.episodeenddt: 1,204 rows differ,
                e.g. key=('drug_a','10077','2011-08-02')
                     sas='2011-09-15' duck='2011-09-16'

4 difference(s).
```

Value differences are counted per column with one example, so a single
systematic error reports as one line rather than sixty thousand.

Numeric and missing-value formatting is tolerated: SAS writes `.` for
missing and formats floats differently, and flagging those would bury
the real differences.
