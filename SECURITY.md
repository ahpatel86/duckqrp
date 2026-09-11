# Security review notes

Written for whoever has to sign off on running this at a Data Partner
site. Findings are from actual scans (`bandit`, `pip-audit`), not from
recollection.

## The short version

| Concern | Status |
|---|---|
| Compiled `.exe` / installer | **None.** Pure Python + SQL text files. |
| Bundled JS / webpack bundle | **None.** No web assets in the core package. |
| Native binary | **One:** `_duckdb…so`, 58 MB, from the official PyPI wheel. |
| Runtime code download | **Disabled.** See below — this was ON by default. |
| Network listener | **Only** if you run `qrp serve` (opt-in extra). |
| `eval` / `exec` / `pickle` | **None.** |
| Known CVEs in declared deps | **None** (`pip-audit`, `duckdb>=1.1`). |
| Bandit HIGH severity | **Zero.** |

The core runtime dependency list is one line:

```toml
dependencies = ["duckdb>=1.1"]
```

`textual` and `textual-serve` are optional extras. `pytest`/`pandas` are
dev-only. A site that wants the minimum attack surface can install the
base package and use the CLI.

## The one thing that would have failed a review

DuckDB ships with **`autoinstall_known_extensions` and
`autoload_known_extensions` set to `true`**. A query touching an
unsupported path type (an `s3://` URL, say) makes DuckDB fetch a binary
extension from `extensions.duckdb.org` and load it — at runtime, on a
production run.

That is a runtime code download from the internet. At most DP sites it
is a hard stop, and where egress is blocked it produces a confusing
failure rather than a clear one.

Both are now **off by default** in `Engine.__post_init__`. The
extensions this pipeline needs — `parquet`, `json`, `icu`,
`core_functions` — are `STATICALLY_LINKED` into the wheel, so nothing is
lost. Verified: parquet reads still work with autoload disabled.

If a site genuinely needs an extension, `Engine(allow_extension_download=True)`
re-enables it as an explicit, reviewable decision.

## Scan results

### Bandit (`bandit -r src tools`)

3,313 lines scanned. **0 HIGH**, 21 MEDIUM, 12 LOW.

| ID | n | What it is |
|---|---|---|
| B608 | 18 | "Possible SQL injection through string construction" |
| B110/B112 | 9 | `try/except/pass` and `try/except/continue` |
| B108 | 3 | Hardcoded `/tmp` paths |
| B404/B603 | 2 | `subprocess` import and call |
| B101 | 1 | `assert` |

**B608 is the one a reviewer will ask about, and the answer needs to be
specific rather than "it's fine".**

The SQL is built with `str.format()` on `.sql` files. What reaches those
placeholders is only:

* dates from `StudyConfig`, already parsed into `datetime.date` and
  re-serialised with `.isoformat()` — a `date` object cannot carry SQL, and
* the `indata` path, supplied on the command line by the operator.

Everything that comes from the study JSON — cohort names, codes, age
strata, demographic values — reaches SQL as **rows in DuckDB config
tables** (`cfg_cohort`, `cfg_codes`, `cfg_age_strata`, `cfg_demog`),
inserted through parameterised `executemany()`, and joined against. That
was a performance decision (it is what lets all cohorts run in one pass)
but it is also the reason no untrusted string is ever concatenated into
SQL.

The remaining `?`-free interpolations are table names in helpers like
`Engine.count()` and `write_parquet()`, where the name comes from a
literal in this codebase's own source.

Worth stating plainly: **the threat model is not a hostile study file.**
Anyone who can supply a study JSON can already run arbitrary SQL by
other means. The reason the config-as-tables design matters is that it
keeps the SQL static and reviewable, not that it defends against a
supply-chain attack.

### B404/B603 (subprocess)

`tools/find_memory_floor.py` runs each memory-limit attempt in a
subprocess so an OOM cannot poison the search, and it writes a temporary
`.py` to do so. That is dynamic code execution and a scanner will flag
it.

It is a **development tool, not part of the runtime**. If a review is
strict, `tools/` can be excluded from what gets deployed — nothing in
`src/qrp/` imports it. All B108 (`/tmp` defaults) and both subprocess
findings are in `tools/`.

### B110/B112 (`try/except/pass`)

Eight sites, all deliberate and all in telemetry paths: a memory poll or
a progress read that fails must not take down a five-hour run. Same
reasoning as `multi_sink` swallowing sink exceptions — there is a test
(`test_broken_sink_does_not_fail_the_run`) asserting exactly that.
Worth annotating with `# nosec` plus a reason if your scanner gates on
LOW findings.

### pip-audit

No known vulnerabilities in the declared dependencies. Note that running
`pip-audit` in a shared environment reports CVEs for everything
installed (`setuptools`, `urllib3`, `wheel` …); scope it to the project
venv or the findings are meaningless.

## The native binary

`_duckdb.cpython-3xx-*.so`, about 58 MB, from the official `duckdb` wheel
on PyPI. This is unavoidable — it is the database engine.

For a site that requires provenance:

* Wheels are published by DuckDB Labs and are reproducible from the
  tagged source at `github.com/duckdb/duckdb`.
* Pin an exact version and hash in your lockfile
  (`pip install duckdb==1.5.5 --require-hashes`).
* Mirror the wheel into an internal artifact repository if outbound PyPI
  is not permitted.

Compare with the PySpark package this replaces, which requires a JVM,
the Spark distribution, and Hadoop libraries. One 58 MB wheel is a
smaller surface than that by a wide margin.

## Network behaviour

Out of the box, **the pipeline opens no sockets**:

* No telemetry, no update checks, no phone-home.
* Extension download now disabled (above).
* Everything reads local parquet and writes local parquet.

Two opt-in exceptions:

1. `python -m qrp serve` binds an HTTP port (default `127.0.0.1:8000`)
   to stream the TUI to a browser. It has **no authentication** — bind
   to loopback only, or put it behind whatever your site already uses.
   Do not expose it on `0.0.0.0` without a proxy.
2. `Engine(allow_extension_download=True)`, off by default.

## What it writes, and where

Everything is explicit and operator-supplied:

| Path | Set by |
|---|---|
| Output parquet | `--out` |
| Database file | `--db` (default: memory only) |
| Spill directory | `--temp-dir` (default: system temp) |
| Logs | `--log-dir` |
| Parity dumps | `--parity-dump` |

No writes to home directories, no config files created outside these,
no environment mutation. Note that **spill files contain patient data**
in intermediate form — point `--temp-dir` at storage covered by the same
controls as the SCDM data itself, and confirm it is cleaned up.

## Suggested hardening for a locked-down site

```bash
pip install --require-hashes -r requirements.lock   # pinned, from mirror
python -m qrp run --study s.json --indata /data/scdm \
  --out /secure/out --temp-dir /secure/scratch --log-dir /secure/logs
```

* Install the base package only — skip the `ui` and `serve` extras.
* Exclude `tools/` from the deployed artefact.
* Put `--temp-dir` on encrypted storage under the same controls as the
  source data.
* Leave `allow_extension_download` at its default.
