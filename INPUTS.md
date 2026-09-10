# What to send me

Two things: a **study input file** (JSON) and an **SCDM parquet root**.
Both can be checked before you send them:

```bash
python -m qrp inspect --study qrp_inputfiles_....json \
                      --lookup qrp_inputfiles_lookup.json \
                      --indata /path/to/scdm_parquet
```

That prints exactly what resolved, what is missing, and what is present
but not yet implemented. If it says `RESULT: usable` and
`RESULT: schema is compatible`, a run will at least execute.

---

## 1. The study input file

Produce it the way you already do — `tools/SAS2JSON/create_json.sas`.
No changes needed. The loader handles the real `PROC JSON` output:

* **Dataset-name keys.** `PROC JSON` writes each table under the SAS
  dataset name (`anmod_mpl1r_cohortfile_v3`), not a logical name.
  `QRP_PARAMETERS` carries the `parameter` → `run1` mapping, and
  `qrp/inputfile.py` resolves it. (My first version assumed literal keys
  like `"cohortfile"` and would have produced a silent zero-cohort study
  on a real file.)
* **SAS date integers.** `create_json.sas` strips date formats so dates
  export as days since 1960-01-01. Accepted, as are ISO strings.
* **SAS column casing.** `CohortGrp`, `EnrDays`, `T2WashPer` are
  lowercased on load.
* **Multi-run files.** Only `run1` is read today. Say so if you need
  `run2+`.

### Tables consumed today

| Table | Fields used |
|---|---|
| `QRP_PARAMETERS` | `type`, `runid`, `startdate`, `enddate`, `censordate` |
| `COHORTFILE` | `cohortgrp`, `enrollmentnum`, `coverage`, `enrolgap`, `chartres`, `enrdays`, `agestrat`, `sex`, `race`, `hispanic`, `reqdaysaftind` |
| `TYPE2FILE` | `group`, `t2washper`, `point`, `episodegap`, `episodegaptype`, `expextper`, `minepisdur`, `maxepisdur`, `mindaysupp`, `t2atriskstart`, `blackoutper`, `t2fupwashper`, `eventcount`, `reqdaysaftepi`, `censor_dth`, `mincumdose`, `maxcumdose`, `t2cumdoseper`, `mincfdd`, `maxcfdd` |
| `COHORTCODES` | `group`, `indexcriteria` (`DEF` / event), `code`, `codesupply` |
| `COVARIATECODES` | `covarnum`, `covarname`, `codecat` (`DX`/`RX`), `covfrom`, `covto`, `dateonly`, `codes` |

### Recognised but NOT implemented

`INCLUSIONCODES`, `USERSTRATA`, `STOCKPILINGFILE`, `RISKSCOREFILE`,
`RISKSCORECODES`, `UTILFILE`, `MFUFILE`.

If your study supplies any of these, `load_study` **warns** and the run
continues without them — so results will be *broader* than the SAS run
(fewer exclusions applied). That is a deliberate loud-failure choice: a
silent narrower answer would be worse. Tell me which ones your study
actually uses and I'll wire them.

### One gap worth naming

Dose restrictions need a **strength per dispensed code**. In SAS this
comes from the `drugclass` lookup keyed on NDC, which I have not wired
in. Until then, supply a `codestrength` array of `{code, strength}`, or
send `drugclass.sas7bdat` / the lookup JSON and I'll read it properly.
Studies with no dose restriction don't need it.

---

## 2. SCDM data

Put the five tables in one folder. The layout inside is **your choice** —
paths are resolved in Python (`qrp.scdm.resolve_table`), not hardcoded
in the SQL, so all of these are accepted and can be mixed:

| Layout | Example |
|---|---|
| One file per table | `enrollment.parquet` |
| One folder per table | `enrollment/part-0.parquet` (nesting and hive partitioning fine) |
| Several files per table | `enrollment_001.parquet`, `enrollment_002.parquet` |
| Any casing | `ENROLLMENT.parquet` |
| CSV instead of parquet | `enrollment.csv` |

### If your tables aren't named anything like that

They usually aren't. Real exports look like
`dp042_msoc_elig_2024q1.parquet` or `SENTINEL_RX_EXTRACT_20240115`, and
no alias list can anticipate that.

Tables are resolved in three steps, each used only when the previous one
found nothing:

1. **An explicit mapping you provide.** Always wins.
   ```bash
   qrp run ... --table-map enrollment=dp042_msoc_elig_2024q1.parquet \
                --table-map demographic=DP042_PT_MASTER_V3.parquet
   ```
2. **The canonical name or a known alias** (`enr`, `demog`, `rx`, `dx`,
   `dth`, …), in any layout, any casing.
3. **The columns.** Every SCDM table this pipeline reads has a
   required-column set that no other one has — they share only `patid` —
   so a table can be identified by what is *in* it rather than what it is
   called. Reading a parquet footer is a metadata operation, so this
   stays cheap even against a 300 GB table.

In practice step 3 means arbitrary names usually just work, with nothing
configured. `qrp inspect --indata <path>` shows exactly which file was
matched to each table, so you can confirm before running.

**Ambiguity is refused, not guessed.** If two files both have the right
columns — two versions of the demographic table, say — the run stops and
names both, telling you to disambiguate with `--table-map`. Silently
picking one, or worse unioning them, would produce a wrong answer rather
than an error.

`death` is **optional** — with no death file the run proceeds and death
censoring never triggers, which yields more episodes. The other four are
required.

`qrp inspect --indata <path>` prints exactly which files matched.

### Columns actually read

Verified against a real SCDM extract (174k patients, 15M claims). The
dispensed-code column is **`rx`**, not `ndc` — `ndc` is the code
vocabulary, not the column. Extracts using `ndc` are accepted too; the
column is resolved from the file's actual schema rather than assumed.

| Table | Required | Optional |
|---|---|---|
| `enrollment` | `patid`, `enr_start`, `enr_end`, `medcov`, `drugcov` | `chart` |
| `demographic` | `patid`, `birth_date`, `sex` | `race`, `hispanic`, `postalcode`, `postalcode_date` |
| `dispensing` | `patid`, `rxdate`, `rx`, `rxsup` | `rxamt` (dose only), `rx_codetype` |
| `diagnosis` | `patid`, `adate`, `dx` | `dx_codetype`, `pdx`, `enctype` |
| `death` | `patid`, `deathdt` | — (empty table is fine) |

`procedure`, `encounter` and `lab_result` are not read yet — they carry
PX covariates, care-setting logic and lab covariates.

### Things that are NOT a problem

* **Column casing.** DuckDB identifiers are case-insensitive, so
  `PatID`, `patid` and `PATID` all resolve. This is why the ~130
  lowercase-map rebuilds in the PySpark port are unnecessary here.
* **Date representation.** DATE, TIMESTAMP, ISO strings and SAS date
  integers are all cast in `10_normalize.sql`.
* **Extra columns.** Ignored, and never read off disk — DuckDB pushes
  projection into the parquet reader.
* **Row-group layout / file count.** Irrelevant.

### Things that ARE a problem

* **Genuine name differences**, e.g. `dxcodetype` vs `dx_codetype`, or
  `rx_days_supply` vs `rxsup`. `qrp inspect --indata` lists these; the
  fix is one alias in `10_normalize.sql`.
* **`.sas7bdat` instead of parquet.** Convert first, or send a small
  sample and I'll add a reader.
* **Encrypted or restricted-use data.** Please don't send real patient
  data. A synthetic or de-identified extract is enough — SynPUF works
  well, and `tools/gen_synthetic.py` shows the shape.

---

## What would make the comparison strongest

1. **The same input file the SAS run used**, so parity is like-for-like.
2. **SAS-side output for at least one stage** — `ptsmasterlist` and
   `attrition` are the highest-value two. Parity on attrition alone
   catches most cohort-definition drift.
3. **The SAS wall-clock time** and the machine it ran on. My benchmarks
   are single-core synthetic; without your baseline the comparison isn't
   meaningful.
4. **A note on which Type 2 options the study exercises** — especially
   `point=Y`, `episodegaptype=P`, `t2atriskstart`, or `blackoutper`,
   since those paths are implemented but untested.
