# Getting this running at a Data Partner site

Written for the person who has to install it, and for whoever has to
help them when it goes wrong.

---

## Three ways to get it running

| route | needs at the site | size | verified here |
|---|---|--:|---|
| **single executable** | nothing — not even Python | ~120 MB | **yes** |
| offline bundle | Python 3.11+ | ~22 MB | yes |
| Docker image | a container runtime | ~200 MB | **no** — no runtime was available |

### Single executable — the least to ask of a site

```bash
./tools/build_executable.sh        # on a machine with Python
# -> dist/qrp, one file
```

Copy that one file to the site. Nothing to install, no Python, no
admin rights:

```bash
./qrp doctor
./qrp run --study study.json --indata /path/to/scdm --out results/
```

**Verified with `PATH` pointed at an empty directory**, so that no
Python could possibly be found: `qrp doctor` passed 11/11 and the
production study ran end to end in 6.8 s, writing all 19 outputs.

Two costs to know about:

* **About 2 s slower per run** than a Python install (6.8 s against
  ~4 s on the production study). The executable unpacks itself to a
  temp directory on every launch. Irrelevant for a real study; visible
  on the demo.
* **One build per platform.** DuckDB's engine is a 60 MB compiled
  library, so a Linux build will not run on Windows. Build once for
  each OS the data partners use.

Why this over Docker for most sites: many data partners run locked-down
Windows servers where a container runtime needs admin rights, WSL2 or
Hyper-V, and a security review of its own. A single signed executable
is a much smaller ask.

### Docker

A `Dockerfile` is included, running as a non-root user with the SCDM
mounted read-only. **It has not been built or run** — no container
runtime was available where it was written. The executable route is
the verified one.

## The short version

```bash
tar -xzf qrp-offline-linux-py3.12.tar.gz
./qrp-offline/INSTALL.sh
```

That creates a virtual environment, installs from bundled wheels with no
network access, prints what it installed, and runs a self-check that
exercises the whole pipeline on generated data. If it finishes with
`All 11 checks passed`, the installation works.

---

## Why not `pip install qrp-duckdb`

Because it assumes the site can reach PyPI, and many cannot — air-gapped
networks, blocking proxies, or a security policy that forbids installing
from the internet. That is the normal case at a Data Partner, not the
exception, so it is the wrong first instruction for the people most
likely to be running this.

`tools/make_offline_bundle.sh` builds the bundle on a machine that DOES
have internet:

```bash
./tools/make_offline_bundle.sh              # core
./tools/make_offline_bundle.sh --with-ui    # plus the terminal UI
```

**Build it on the same OS and Python minor version as the target site.**
Wheels are platform-specific: a bundle built on Linux/3.12 will not
install on Windows/3.11. The filename records both
(`qrp-offline-linux-py3.12.tar.gz`) so a mismatch is visible before
anyone tries.

The installer uses `pip --no-index`, so it either succeeds from the
bundled wheels or fails loudly. It never half-installs from a stale
cache or silently reaches the network.

---

## Two commands that make support possible

### `qrp version`

```
qrp-duckdb   0.1.0 (a51bd9bc)
duckdb       1.5.5
textual      8.2.8
python       3.12.3
platform     Linux-6.18.44-fc-v32-x86_64-with-glibc2.39
host         1 CPU, 4.2GB RAM
```

The value in brackets is a **hash of the installed code**, not of the
version string. It changes if any shipped `.py` or `.sql` file differs
by so much as a line.

That matters because a version number cannot distinguish `0.1.0` from
`0.1.0 with a patch applied by hand three weeks ago`. Those two sites
report the same version and run different code, and the difference is
invisible until someone spends a day chasing a bug that was already
fixed at one of them.

### `qrp doctor`

Runs the whole pipeline on a small generated extract and checks the
answers — episodes inside enrollment, attrition never increasing,
outputs written, manifest present. **It needs no site data and touches
nothing.**

Run it:

* immediately after installing
* after applying any patch
* whenever something looks wrong and nobody is sure whether the tool or
  the data is at fault

It fails loudly and usefully:

```
[ FAIL ] attrition never increases

1 check(s) FAILED: attrition never increases

Send the version line at the top of this output, plus these
failures, to whoever provided the package.
```

The data and study it uses are generated **inside the package**, not
read from `tools/` or `study/`. An earlier version depended on those and
therefore worked from a source checkout and failed on every real
installation — which is exactly backwards. Verified by installing the
wheel into a clean environment with PyPI unreachable.

---

## Patching a live installation

### Option A — a whole new bundle (preferred)

```bash
./qrp-offline/INSTALL.sh ~/qrp-env-new
~/qrp-env-new/bin/qrp version      # confirm the fingerprint changed
~/qrp-env-new/bin/qrp doctor
```

Install alongside rather than over the top. The old environment stays
until the new one is confirmed working, and rolling back is deleting a
folder.

### Option B — a single file, for an urgent fix

The package is plain Python and SQL with no compiled extensions, so a
one-file fix can be dropped straight in:

```bash
# where the installed code lives
ENV=~/qrp-env
QRP=$("$ENV/bin/python" -c 'import qrp,pathlib;print(pathlib.Path(qrp.__file__).parent)')

cp 70_outputs.sql "$QRP/sql/70_outputs.sql"

"$ENV/bin/qrp" version    # the fingerprint MUST change
"$ENV/bin/qrp" doctor     # and the checks must still pass
```

**Always run `version` and `doctor` after.** The fingerprint changing
proves the patch actually landed — copying to the wrong path is the
commonest way a "patched" site turns out not to be. `doctor` proves the
patch did not break something else.

Record the new fingerprint. It is what identifies that site's code from
then on.

### What a rerun does to existing output

Rerunning into the same output directory clears **that run id's**
previous files first, so no stale results survive a rerun. Other runs'
outputs in the same directory are untouched. See
[OUTPUTS.md](OUTPUTS.md#rerun-safety).

---

## Reporting a problem

Send:

1. the full output of `qrp version`
2. the full output of `qrp doctor`
3. the run log (`--log-dir`), which records the effective memory limit,
   thread count, and every stage's row counts
4. the study file, if it can be shared

Do not send patient-level output. Nothing in the three items above
contains any: `doctor` uses generated data, and the run log records
counts and timings only. Database errors have quoted values redacted
before they are logged, for exactly this reason.
