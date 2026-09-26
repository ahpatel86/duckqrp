"""What am I running, and does it work?

The two questions a data partner needs answered before anyone can help
them, and the two that are hardest to answer over email.

`qrp version` prints an identity that is unambiguous even after a patch:
the release version, plus a FINGERPRINT of the installed SQL and Python.
A version string alone cannot distinguish 0.1.0 from 0.1.0-with-a-patch,
and a site that has applied a fix by hand looks identical to one that has
not. The fingerprint changes when any shipped file changes, so
"0.1.0 (a3f2c918)" in a bug report says exactly which code ran.

`qrp doctor` runs the whole pipeline on a small generated dataset and
checks the answers. It needs no site data and touches nothing, so it can
be run immediately after installing, after applying a patch, or when
something looks wrong and nobody is sure whether the tool or the data is
at fault.
"""
from __future__ import annotations

import hashlib
import platform
import sys
import tempfile
from pathlib import Path

RELEASE = "0.1.0"


def fingerprint() -> str:
    """A short hash over every shipped .py and .sql file.

    Hashes CONTENT, not a version string, so a hand-applied patch is
    visible. Sorted by path so the value is stable across filesystems.
    """
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.suffix in (".py", ".sql") and path.is_file():
            h.update(path.relative_to(root).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:8]


def version_report() -> str:
    try:
        import duckdb
        duck = duckdb.__version__
    except Exception:                                   # pragma: no cover
        duck = "NOT INSTALLED"
    try:
        import textual
        ui = textual.__version__
    except Exception:
        ui = "not installed (terminal UI unavailable)"

    from .sysinfo import HostInfo, human

    host = HostInfo.detect()
    return "\n".join([
        f"qrp-duckdb   {RELEASE} ({fingerprint()})",
        f"duckdb       {duck}",
        f"textual      {ui}",
        f"python       {sys.version.split()[0]}",
        f"platform     {platform.platform()}",
        f"host         {host.cpus} CPU, {human(host.memory_bytes)} RAM",
        "",
        "Quote the line above in full when reporting a problem. The value",
        "in brackets is a hash of the installed code: it changes if any",
        "file has been patched, so it distinguishes a stock install from",
        "a patched one that would otherwise look identical.",
    ])


def _tiny_study() -> dict:
    """A two-cohort Type 2 study, defined inline.

    Small enough to run in seconds, complete enough to exercise
    enrollment, exposure, stockpiling, episodes, events and attrition.
    """
    return {
        "qrp_parameters": [{"type": 2, "runid": "doctor",
                            "startdate": "2011-01-01",
                            "enddate": "2014-12-31"}],
        # Column names follow the real input contract exactly —
        # cohortfile uses `cohortgrp`, type2file uses `group`. Guessing
        # produced a KeyError that looked like a pipeline failure.
        "cohortfile": [{"cohortgrp": "drug_a", "enrollmentnum": 1,
                        "coverage": "MD", "enrolgap": 45, "chartres": "N",
                        "enrdays": 183, "agestrat": "18-44 45-64 65-74 75+",
                        "reqdaysaftind": 0}],
        "type2file": [{"group": "drug_a", "t2washper": 183, "point": "N",
                       "episodegap": 15, "episodegaptype": "F",
                       "expextper": 0, "minepisdur": 1, "maxepisdur": 365,
                       "t2fupwashper": 183, "eventcount": 1,
                       "censor_dth": "Y", "t2atriskstart": 0,
                       "blackoutper": 0, "reqdaysaftepi": 0}],
        "cohortcodes": (
            [{"group": "drug_a", "indexcriteria": "DEF", "codecat": "RX",
              "code": f"E{i:05d}"} for i in range(1, 11)]
            + [{"group": "drug_a", "indexcriteria": "EVENT", "codecat": "DX",
                "code": f"X{i:05d}", "caresettingprincipal": ""}
               for i in range(1, 6)]
        ),
    }


def _tiny_extract(out: Path) -> Path:
    """Generate a minimal SCDM extract with DuckDB alone.

    Deliberately does NOT import tools/gen_synthetic: that is not part
    of the installed package.
    """
    import duckdb

    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    n = 2000
    con.execute(f"CREATE TABLE _p AS SELECT i AS patid FROM range({n}) t(i)")

    def write(name: str, sql: str) -> None:
        (out / name).mkdir(exist_ok=True)
        con.execute(
            f"COPY ({sql}) TO '{out / name / 'data.parquet'}' (FORMAT PARQUET)")

    write("demographic", """
        SELECT patid, DATE '1950-01-01' + CAST(random()*18000 AS INTEGER)
                      AS birth_date,
               ['F','M'][1 + CAST(random() AS INTEGER)] AS sex,
               'U' AS hispanic, '0' AS race, '02138' AS postalcode,
               NULL::DATE AS postalcode_date
        FROM _p""")
    write("enrollment", """
        SELECT patid, DATE '2010-01-01' AS enr_start,
               DATE '2015-12-31' AS enr_end,
               'Y' AS medcov, 'Y' AS drugcov, 'N' AS chart
        FROM _p""")
    write("dispensing", """
        SELECT patid,
               DATE '2011-01-01' + CAST(random()*1000 AS INTEGER) AS rxdate,
               'E' || lpad((1 + CAST(random()*9 AS INTEGER))::VARCHAR, 5, '0')
                      AS rx,
               'ND' AS rx_codetype, 30 AS rxsup, 30 AS rxamt
        FROM _p, range(4)""")
    write("diagnosis", """
        SELECT patid,
               DATE '2011-01-01' + CAST(random()*1000 AS INTEGER) AS adate,
               'X' || lpad((1 + CAST(random()*4 AS INTEGER))::VARCHAR, 5, '0')
                      AS dx,
               '09' AS dx_codetype,
               ['IP','AV'][1 + CAST(random() AS INTEGER)] AS enctype,
               'P' AS pdx
        FROM _p, range(3)""")
    write("death", "SELECT patid, DATE '2016-01-01' AS deathdt FROM _p LIMIT 0")
    con.close()
    return out


def doctor(keep: bool = False) -> int:
    """Run the pipeline end to end on generated data and check it.

    Returns 0 if everything passed. Uses no site data.
    """
    ok: list[str] = []
    fail: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        (ok if condition else fail).append(name)
        mark = "  ok  " if condition else " FAIL "
        print(f"[{mark}] {name}" + (f"  — {detail}" if detail and not condition
                                    else ""))

    print(version_report().split("\n\n")[0])
    print()

    try:
        import duckdb
        check("duckdb imports", True)
        con = duckdb.connect()
        con.execute("SELECT 1 FROM range(1) QUALIFY row_number() OVER () = 1")
        check("duckdb supports QUALIFY", True)
        con.execute("SELECT 1 AS a FROM range(1) GROUP BY ALL")
        check("duckdb supports GROUP BY ALL", True)
        con.close()
    except Exception as exc:
        check("duckdb works", False, str(exc)[:60])
        print("\n  DuckDB is too old or missing. This package needs 1.5 or "
              "newer.\n  Reinstall with: pip install 'duckdb>=1.5,<2'")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="qrp-doctor-"))
    try:
        # Data and study are generated HERE, not read from tools/ or
        # study/. Those live outside the installed package, so a doctor
        # that depended on them failed on every real installation —
        # exactly where it is needed and nowhere else. Verified by
        # installing the wheel into a clean venv with PyPI unreachable.
        data = _tiny_extract(workdir / "data")
        check("generated test data", data.is_dir())

        from .config import load_study_dict
        from .engine import Engine
        from .pipeline import run as run_pipeline

        study = load_study_dict(_tiny_study())
        check("built a test study", bool(study.cohorts))

        eng = Engine(verbose=False)
        try:
            run_pipeline(study, str(data), engine=eng, verbose=False)
            check("pipeline ran to completion", True)

            episodes = eng.count("cohort_final")
            check("produced episodes", episodes > 0, f"got {episodes}")

            row = eng.con.execute("""
                SELECT count(*) FROM cohort_final
                WHERE indexdt < enr_start OR episodeenddt > enr_end
            """).fetchone()
            bad = row[0] if row else -1
            check("episodes lie within enrollment", bad == 0, f"{bad} outside")

            row = eng.con.execute(
                "SELECT count(*) FROM attrition WHERE excluded < 0"
            ).fetchone()
            neg = row[0] if row else -1
            check("attrition never increases", neg == 0)
        finally:
            eng.close()

        # A second pass that WRITES, because permissions and disk are
        # where a site fails and a developer does not.
        out = workdir / "out"
        run_pipeline(study, str(data), output_dir=str(out), names="sas",
                     verbose=False)
        wrote = list(out.rglob("*.parquet"))
        check("wrote output files", bool(wrote), f"{len(wrote)} files")
        check("wrote a manifest", (out / "manifest.json").exists())
    except Exception as exc:
        check("pipeline ran to completion", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        if keep:
            print(f"\n  Working files kept in {workdir}")
        else:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    if fail:
        print(f"{len(fail)} check(s) FAILED: {', '.join(fail)}")
        print()
        print("Send the version line at the top of this output, plus these")
        print("failures, to whoever provided the package.")
        return 1
    print(f"All {len(ok)} checks passed. The installation works.")
    return 0
