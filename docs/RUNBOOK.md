# Running a query — plain-language guide

Written for an analyst at a Data Partner site. No Python knowledge
assumed. If you can open a terminal and type a command, you can run this.

---

## What this tool actually does

You have two things:

1. **A study definition** — the JSON file the study designer sent you.
   It says which drug, which outcome, which patients, over what dates.
2. **Your SCDM data** — your site's enrollment, demographic, dispensing,
   diagnosis and death tables.

The tool reads both, works out which patients qualify and for how long,
and writes summary tables. It never sends anything anywhere. Everything
stays on your machine.

Nothing is modified in your source data. It is opened read-only.

---

## One-time setup

You need Python 3.10 or newer:

```bash
python3 --version
```

### Yes, you need a virtual environment

On most modern systems a plain `pip install` will **refuse to run**:

```
error: externally-managed-environment
```

That is not a problem with this tool. Recent Linux distributions (and
Homebrew on macOS) protect the system Python so that installing packages
into it cannot break the operating system. It is a good thing, and every
Python tool hits it.

A virtual environment is just a private folder holding its own copy of
Python and its packages. Nothing outside it is touched, and deleting the
folder uninstalls everything cleanly.

**Create it once:**

```bash
python3 -m venv ~/qrp-env
```

**Install into it:**

```bash
~/qrp-env/bin/pip install qrp-duckdb
```

**Then run the tool by its full path:**

```bash
~/qrp-env/bin/qrp --help
```

If you would rather type `qrp` than the full path, "activate" the
environment first — it lasts until you close the terminal:

```bash
source ~/qrp-env/bin/activate     # macOS / Linux
qrp --help
```

```powershell
~\qrp-env\Scripts\Activate.ps1    # Windows PowerShell
qrp --help
```

This guide uses the plain `qrp` form from here on. If you have not
activated, put `~/qrp-env/bin/` in front of it.

### The simpler alternative

If your site has `pipx`, it handles the environment for you:

```bash
pipx install qrp-duckdb
qrp --help
```

### No internet access

Ask IT to mirror the package internally, then:

```bash
~/qrp-env/bin/pip install --require-hashes -r requirements.lock
```

There is no separate database to set up, no server to start, no Java —
one package and its engine.

**Optional:** for the point-and-click screen,
`~/qrp-env/bin/pip install "qrp-duckdb[ui]"`.

---

## The five steps

### Step 1 — Point it at your data

Put the five tables in one folder. **The arrangement inside is up to
you** — all of these work:

**One file per table** (simplest):

```
my_scdm_data/
    enrollment.parquet
    demographic.parquet
    dispensing.parquet
    diagnosis.parquet
    death.parquet
```

**One folder per table** (if your export is partitioned):

```
my_scdm_data/
    enrollment/     part-0.parquet, part-1.parquet, …
    demographic/
    …
```

**Several files per table:**

```
my_scdm_data/
    enrollment_001.parquet
    enrollment_002.parquet
    …
```

You can also **mix** these, use **CSV** instead of parquet, and name
files in **any case** (`ENROLLMENT.parquet` is fine). Column
capitalisation doesn't matter either — `PatID`, `patid` and `PATID` are
all the same. Extra columns are ignored.

If your data is in SAS (`.sas7bdat`), it needs converting once. Your data
manager will know how; there is nothing study-specific about it.

**The file names don't have to match either.** Real exports are called
things like `dp042_msoc_elig_2024q1.parquet`. The tool first looks for
familiar names, and if that fails it identifies each table **by its
columns** — every SCDM table has a distinctive set, so this usually just
works with nothing configured.

If it can't tell (for example you have two versions of the same table),
it stops and asks rather than guessing, and you point it at the right one:

```bash
qrp run ... --table-map demographic=DP042_PT_MASTER_V3.parquet
```

**`death` is optional.** If your site has no death file, the run
proceeds and nobody is censored at death — you'll get slightly more
episodes as a result. The other four are required.

Not sure what it will pick up? Step 2 tells you exactly which file it
matched to each table.

### Step 2 — Check before you run

This is the step people skip and then regret. It takes seconds and tells
you whether anything is wrong **before** you wait for a long run:

```bash
qrp inspect --study study.json --indata my_scdm_data/
```

It prints which files it matched for each table, so you can confirm it
found what you expected:

```
table         status              rows  matched / notes
enrollment    ok               200,000  /data/enrollment*.parquet
demographic   ok               102,000  /data/demographic*.parquet
dispensing    ok             1,381,682  /data/dispensing*.parquet
diagnosis     ok             1,899,826  /data/diagnosis*.parquet
death         absent                    optional — death censoring
```

You want to see two lines at the end:

```
RESULT: usable — 2 cohort row(s), 80 code row(s), 12 covariate row(s)
RESULT: schema is compatible.
```

If instead you see `REQUIRED, MISSING` or `BAD SCHEMA`, stop and read
what it names. Common causes:

* A folder is missing, or is a file rather than a folder.
* A column has a genuinely different name (not just different case).
* The study JSON names a table that isn't in the file.

Also read any lines marked **`[present but NOT YET IMPLEMENTED]`**. That
means the study asks for something this tool doesn't apply yet, so your
results would include *more* patients than the SAS version. Tell the
study team before running.

### Step 3 — Decide how much memory to give it

The tool works within whatever limit you give it. If it needs more, it
uses disk instead of failing — but disk is slower, so more memory means
a faster run.

A reasonable rule: **give it about two-thirds of your machine's RAM**,
and point its scratch space at a disk with plenty of free room.

If you're not sure what your machine can handle, or the data is large:

```bash
qrp inspect --indata my_scdm_data/     # tells you how big the data is
```

For very large data, ask your technical contact to run
`tools/find_memory_floor.py` once against your biggest table. It reports
the minimum memory that works and how much scratch disk you'll need.

### Step 4 — Run it

```bash
qrp run --study study.json \
        --indata my_scdm_data/ \
        --out results/ \
        --memory-limit 8GB \
        --temp-dir /scratch/qrp \
        --log-dir logs/
```

Reading that:

| Part | What it means |
|---|---|
| `--study` | the JSON the study designer sent |
| `--indata` | the folder holding your five data folders |
| `--out` | where to write the results |
| `--memory-limit` | how much RAM it may use |
| `--temp-dir` | scratch space when RAM runs out |
| `--log-dir` | where to save the record of the run |

You'll see each step tick past with timings. A small study finishes in
seconds; a large one can take hours.

**Important:** the scratch folder will hold patient data while the run is
going. Put it somewhere covered by the same rules as your SCDM data, not
in a shared temp folder.

### Step 5 — Look at the results

Yes, this is something you actively do. The results are saved as parquet
files, which are efficient but **do not open in Excel**. The tool prints
them for you.

Results are written into **two folders**:

```
results/
    dplocal/     ← stays here, behind your firewall
        <runid>_mstr/               patient-level, one folder per cohort
        <runid>_mstr_final/
        <runid>_denomcounts.parquet
    msoc/        ← what goes to the Operations Center
        <runid>_attrition.parquet
        <runid>_censoring.parquet
        <runid>_signature.parquet    run provenance
        <runid>_runtimes.parquet     per-stage timings
    manifest.json
```

**That split is a disclosure boundary.** Anything in `dplocal/` is
patient-level and stays local. Anything in `msoc/` is aggregate and is
what you return. If you are unsure whether a file may leave the site,
the folder is the answer — and anything unclassified defaults to
`dplocal`, so a new output can never become shareable by accident.



File names mirror the SAS QRP datasets (`<runid>_mstr`,
`<runid>_denomcounts`) so they drop into existing SOPs and review habits
unchanged. `--names logical` uses this tool's own names instead. Either
way `qrp show` finds tables by their logical name, so you never have to
remember which was used.

**See what was produced:**

```bash
qrp show --out results/
```

```
Result tables in results/:

  attrition                           12 rows
  censoring                           10 rows
  cohort_final                    68,845 rows
  denominators                       138 rows
  ptsmasterlist                   75,960 rows
```

**Read `attrition` first — this is the sanity check:**

```bash
qrp show --out results/ attrition
```

```
cohortgrp     step_no  step                            records  patients  records_dropped
lisinopril          1  Exposure dispensings            145,008    76,639
lisinopril          2  After stockpiling               138,091    76,639            6,917
lisinopril          3  Met washout (potential index)   119,779    76,639           18,312
lisinopril          4  With enrollment and demographi   53,213    41,652           66,566
lisinopril          5  Met episode and enrollment cri   38,003    31,999           15,210
lisinopril          6  Met follow-up washout (final c   34,449    29,495            3,554
```

Each row is a filter. Read down the `records` column and ask whether
each drop is plausible. **If 99% of patients disappear at one step,
something is wrong** — with the data or with the study definition — and
this table tells you exactly which step to investigate. It is far
cheaper to catch that here than after the numbers have been reported.

**Look at the rates:**

```bash
qrp show --out results/ denominators --limit 20
```

**Filter to what you care about:**

```bash
qrp show --out results/ denominators --where "sex='F' and agegroup='65-74'"
```

**Get it into Excel:**

```bash
qrp show --out results/ --csv excel_copies/
```

or add `--csv` to the original run and CSV copies are written alongside
the parquet automatically.

### What each table means

| Table | What's in it |
|---|---|
| `attrition` | how many patients were dropped at each step — **read this first** |
| `denominators` | counts and event rates, by age, sex and year |
| `censoring` | why follow-up ended (event, death, disenrollment, study end) |
| `cohort_final` | one row per qualifying patient episode — the detailed data |
| `ptsmasterlist` | the patient list before the follow-up washout was applied |

The first three are summaries small enough to read on screen. The last
two are per-episode and will have hundreds of thousands of rows —
`qrp show` prints the first 50 and tells you the total.

---

## If something goes wrong

The tool tries to tell you what to do rather than just what broke.

**"Out of memory"** — the limit was too low. Raise `--memory-limit`, and
make sure `--temp-dir` points somewhere with free space. It is not a
sign your machine is too small; it means the number you gave was.

**"No files found that match the pattern"** — check `--indata` points at
the *parent* folder that contains `enrollment/`, `dispensing/` etc., not
at one of those folders.

**A warning about `inclusioncodes` or similar** — the study uses a
feature not yet implemented. The run continues, but your results will be
broader than the SAS version. Report it before using the numbers.

**It's just slow** — check the log for lines saying `SPILLING`. That
means it ran out of memory and is using disk. Raising `--memory-limit`
will speed it up.

---

## Sending results back

Include the log file. It records exactly what was run — the study
settings, every cohort parameter, the software versions, the timings and
the row counts at each step. If two sites disagree, the logs usually
show why without anyone re-running anything.

```
logs/
    mystudy_20260829_143022.log      ← readable; send this one
    mystudy_20260829_143022.jsonl    ← for automated comparison
```

Check the log before sending: it contains counts and settings, not
patient-level data, but it is worth a look under your site's disclosure
rules.

---

## The point-and-click version

Each path field has a **Browse** button that opens a folder/file picker,
so you can navigate rather than paste. Typing still works and is faster
when you know the path.

If you'd rather not type commands:

```bash
qrp ui
```

You get a screen with boxes for the file paths, dropdowns for memory and
threads, an **Inspect** button, a progress bar, and Escape to stop.

Works in **Windows Terminal**, VS Code, macOS Terminal, and any Linux
terminal. It does **not** work properly in Git Bash or the old
`cmd.exe` — if that's what you have, use:

```bash
qrp serve
```

then open `http://127.0.0.1:8000` in a browser. Same screen, no terminal
needed.

---

## Quick reference

```bash
python3 -m venv ~/qrp-env                       # one-time setup
~/qrp-env/bin/pip install qrp-duckdb
source ~/qrp-env/bin/activate                   # each new terminal

qrp inspect --study s.json --indata data/       # check before running
qrp validate --study s.json                     # check the study only
qrp run --study s.json --indata data/ --out results/ --log-dir logs/
qrp show --out results/                         # list result tables
qrp show --out results/ attrition               # read one
qrp show --out results/ --csv excel/            # export for Excel
qrp ui                                          # point-and-click
qrp serve                                       # same, in a browser
```

Every command takes `--help`.


---

## How SCDM tables are found

You point `--indata` at a folder. For each table the pipeline needs, it
tries three strategies **in order of how much it trusts them**:

| # | strategy | example |
|---|---|---|
| 1 | **your `--table-map`** — always wins | `--table-map dispensing=/other/rx.parquet` |
| 2 | **the file name**, case-insensitively, incl. known aliases | `dispensing.parquet`, `DISPENSING/`, `dispensing/*.parquet` |
| 3 | **the columns** — identifies the table by its schema | a file called `dp042_rx_2024q1.parquet` |

Strategy 3 is what handles site-specific names. It is only used when
the name gave nothing, so a correctly named file is never second-guessed.

`qrp inspect --indata <folder>` shows what was found and where, before
you commit to a run.

### Reading one table from somewhere else

```bash
qrp run --study study.json --indata /data/scdm \
        --table-map dispensing=/data/other_team/rx_extract_2024.parquet
```

* An **absolute** path is used exactly as given — it does not have to be
  inside `--indata`.
* A **relative** path is resolved against `--indata`.
* A **folder** is read as every parquet file beneath it.
* Repeat `--table-map` for more than one table.

Verified: moving `dispensing` to a different directory under a
different name and pointing `--table-map` at it gives results identical
to the file sitting in the folder.

### When the override is wrong

Both mistakes stop the run with a message rather than guessing:

```
--table-map names 'dispensng', which is not an SCDM table this package
reads. Did you mean 'dispensing'?
```

```
error: --table-map dispensing=... points at '/data/WRONG.parquet',
which does not exist.
  Check the path. An absolute path is used as-is; a relative one is
  resolved against --indata (/data/scdm).
```

**The misspelt name used to be silently ignored.** The override was
dropped, the real table reported as missing, and nothing said the name
had not been recognised — so it looked like a data problem, not a typo.
The path error used to say the file was missing "under" the input
folder even for an absolute path, which it never was.


### In the terminal UI

The UI now has the same two controls as the command line:

| field | equivalent | notes |
|---|---|---|
| **Tables** | `--table-map` | `dispensing=/other/rx.parquet; diagnosis=/other/dx` |
| **Debug** | `--debug` | also write the dplocal diagnostics |

Separate several overrides with a **semicolon**, not a comma — commas
turn up in real folder names and semicolons essentially never do.

**Inspect honours the Tables field**, so what it checks is what Run will
read. Inspecting the folder without the overrides would report a table
as missing that the run will in fact find elsewhere.

A misspelt table name is caught by the same check as the CLI, with the
same "Did you mean ...?" suggestion, and stops before anything starts.

Before this, the UI had neither control — `RunHandle` did not accept a
table map at all — so a site keeping one table outside its SCDM folder
could only run from the command line.
