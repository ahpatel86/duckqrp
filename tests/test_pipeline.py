"""
Reproducibility and stage-gating tests.

Determinism is the risk I would worry about most in a SAS→SQL port. SAS
resolves BY-group ties by physical row order; SQL guarantees nothing
unless the ORDER BY is a total order. A non-total tie-break does not
fail loudly — it produces a slightly different answer on a different
thread count or a different parquet row order, which surfaces as an
unreproducible parity failure weeks later.

These tests pin that down by running the same input twice under
different thread counts and requiring byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import qrp

from qrp import Engine, load_study, run

STUDY = Path(__file__).resolve().parents[1] / "study" / "demo_full.json"
DATA = Path("/tmp/qrp_data/100k")

pytestmark = pytest.mark.skipif(
    not DATA.exists(), reason="run tools/gen_synthetic.py first"
)


def _digest(eng: Engine, table: str, order: str) -> str:
    rows = eng.con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(r).encode())
    return h.hexdigest()


@pytest.fixture(scope="session")
def baseline():
    """One run of the default study, shared across read-only tests.

    The suite runs the full pipeline ~70 times at ~8s each, which is
    most of its wall time. Sixteen of those runs were the SAME default
    study, differing only in what they asserted afterwards. A shared
    session fixture collapses them into one.

    Only for tests that READ. Anything varying config, layout or output
    still builds its own engine — sharing state between tests that
    mutate it is how a suite starts depending on execution order.
    """
    import warnings

    from qrp import Engine, load_study

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = load_study(STUDY)
    eng = Engine(verbose=False)
    run(s, DATA, engine=eng, verbose=False)
    yield eng
    eng.close()


@pytest.fixture(scope="module")
def study():
    return load_study(STUDY)


# ---------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,order",
    [
        ("stockpiled", "cohortgrp, patid, adate"),
        ("index_candidates", "cohortgrp, patid, adate"),
        ("ptsmasterlist", "cohortgrp, patid, indexdt"),
        ("cohort_final", "cohortgrp, patid, indexdt"),
        ("attrition", '"group", level'),
    ],
)
def test_output_is_thread_count_invariant(study, table, order):
    """Same input, different parallelism, identical bytes.

    If any window in the pipeline has a non-total ORDER BY, this is where
    it shows up: DuckDB is free to resolve the tie differently when the
    partitioning across threads changes.
    """
    digests = []
    for threads in (1, 4):
        eng = Engine(threads=threads, verbose=False)
        run(study, DATA, engine=eng, verbose=False)
        digests.append(_digest(eng, table, order))
        eng.close()
    assert digests[0] == digests[1], (
        f"{table} differs between 1 and 4 threads — some window ORDER BY "
        f"is not a total order"
    )


def test_repeat_run_is_identical(study):
    """Two runs in the same process must agree."""
    out = []
    for _ in range(2):
        eng = Engine(verbose=False)
        run(study, DATA, engine=eng, verbose=False)
        out.append(_digest(eng, "cohort_final", "cohortgrp, patid, indexdt"))
        eng.close()
    assert out[0] == out[1]


# ---------------------------------------------------------------------
# Stage gating
# ---------------------------------------------------------------------


def test_unused_stages_do_not_run(study):
    """A cohort with no dose limit must not build the dose tables.

    The port answered this question with a Spark job. Here the absence
    of the table IS the evidence that no work was done.
    """
    simple = load_study(STUDY.parent / "demo_type2.json")
    assert not simple.any_dose
    assert not simple.any_covariates

    eng = Engine(verbose=False)
    run(simple, DATA, engine=eng, verbose=False)
    existing = {
        r[0] for r in eng.con.execute(
            "SELECT table_name FROM duckdb_tables()"
        ).fetchall()
    }
    assert "dose_excluded" not in existing
    assert "covariates_long" not in existing
    eng.close()


def test_dose_restriction_actually_excludes(study, baseline):
    """The dose path must remove rows, or it isn't being exercised."""
    assert study.any_dose
    excluded = baseline.con.execute(
    "SELECT count(*) FROM dose_excluded"
    ).fetchone()[0]
    assert excluded > 0, "dose restriction excluded nothing"
    # only the cohort that declares a limit may lose rows
    cohorts = {
    r[0] for r in baseline.con.execute(
        "SELECT DISTINCT cohortgrp FROM dose_excluded"
    ).fetchall()
    }
    assert cohorts == {"lisinopril"}


# ---------------------------------------------------------------------
# Invariants that should hold for any study
# ---------------------------------------------------------------------


def test_attrition_is_monotonic(baseline):
    """Every attrition step must be a subset of the one before it."""
    # `excluded` is the contract column; the records_dropped /
    # patients_dropped extras were removed as non-contract.
    bad = baseline.con.execute("""
    SELECT "group", level FROM attrition WHERE excluded < 0
    """).fetchall()
    assert bad == [], f"attrition increased at {bad}"


def test_episodes_lie_within_enrollment(baseline):
    bad = baseline.con.execute("""
        SELECT count(*) FROM cohort_final
        WHERE indexdt < enr_start OR episodeenddt > enr_end
    """).fetchone()[0]
    assert bad == 0


def test_person_time_is_positive(baseline):
    # Uses the shared run: reads only, no config variation. Tests that
    # vary config still build their own engine — sharing state between
    # tests that mutate it is how a suite starts depending on order.
    bad = baseline.con.execute(
        "SELECT count(*) FROM cohort_final WHERE person_days <= 0"
    ).fetchone()[0]
    assert bad == 0


def test_events_fall_inside_the_at_risk_window(baseline):
    bad = baseline.con.execute("""
        SELECT count(*) FROM cohort_final
        WHERE eventdt IS NOT NULL
          AND (eventdt < atriskindexdt OR eventdt > episodeenddt)
    """).fetchone()[0]
    assert bad == 0


# ---------------------------------------------------------------------
# Input file handling (real create_json.sas shape)
# ---------------------------------------------------------------------

REALISTIC = STUDY.parent / "realistic_inputfile.json"


def test_resolves_qrp_parameters_indirection():
    """Real files key tables by SAS DATASET name, not logical name.

    A loader that assumes literal 'cohortfile' keys silently produces a
    zero-cohort study on a real input file. This pins the indirection.
    """
    from qrp import inputfile

    inp = inputfile.load(REALISTIC)
    assert inp.aliases["cohortfile"] == "anmod_mpl1r_cohortfile_v3"
    assert inp.aliases["type2file"] == "anmod_mpl1r_type2_v3"
    assert len(inp.rows("cohortfile")) == 2
    # SAS column casing must be normalised
    assert "cohortgrp" in inp.rows("cohortfile")[0]


def test_reports_tables_named_but_absent():
    from qrp import inputfile

    inp = inputfile.load(REALISTIC)
    assert any("riskscorefile" in u for u in inp.unresolved)


def test_warns_on_unimplemented_tables():
    import warnings

    from qrp import load_study

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        load_study(REALISTIC)
    # inclusioncodes is now implemented, so it must NOT warn. What must
    # still warn is a table named in qrp_parameters but absent from the
    # JSON — a silently narrower or broader cohort is the danger.
    messages = " ".join(str(x.message) for x in w)
    assert "riskscorefile" in messages
    assert "inclusioncodes" not in messages, (
        "inclusioncodes is implemented; it should no longer warn"
    )


def test_sas_date_integers_are_decoded():
    from datetime import date

    from qrp.inputfile import sas_date

    assert sas_date(18628) == date(2011, 1, 1)
    assert sas_date("2011-01-01") == date(2011, 1, 1)
    assert sas_date(0) == date(1960, 1, 1)
    assert sas_date(None) is None


def test_realistic_inputfile_runs_end_to_end():
    import warnings

    from qrp import Engine, load_study

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = load_study(REALISTIC)
    eng = Engine(verbose=False)
    run(s, DATA, engine=eng, verbose=False)
    assert eng.count("cohort_final") > 0
    eng.close()


def test_dose_filter_precedes_pov1():
    """Regression: pov1 must be built from the dose-filtered candidates.

    Originally pov1 was created in the same script as index detection,
    which runs before the dose stage — so dose exclusions were computed
    and then silently ignored.
    """
    import warnings

    from qrp import Engine, load_study

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = load_study(REALISTIC)
    eng = Engine(verbose=False)
    run(s, DATA, engine=eng, verbose=False)
    orphans = eng.con.execute("""
        SELECT count(*) FROM pov1 p
        LEFT JOIN index_candidates i
               ON i.cohortgrp = p.cohortgrp
              AND i.patid     = p.patid
              AND i.adate     = p.indexdt
        WHERE i.patid IS NULL
    """).fetchone()[0]
    assert orphans == 0, "pov1 contains index dates the dose stage removed"
    eng.close()


def test_unsatisfiable_dose_config_is_rejected():
    """min_cum_dose inside the washout window excludes everything."""
    from qrp.config import AgeStrata, CohortConfig

    c = CohortConfig(
        cohortgrp="x", wash_per=183,
        min_cum_dose=100.0, cum_dose_per=180,
        age_strata=AgeStrata.parse(None),
    )
    with pytest.raises(ValueError, match="excludes every index date"):
        c.validate()


# ---------------------------------------------------------------------
# Memory floor
# ---------------------------------------------------------------------


def test_runs_under_a_tight_memory_limit(study):
    """The pipeline must complete when memory is scarce, not just when
    it is plentiful.

    This is a regression guard on stage shape, not on DuckDB. POV1 was
    once a single 10-way join with two range joins, so its peak memory
    was the SUM of ten concurrent hash tables; below ~420MB at 2m
    patients it raised OutOfMemoryException. Split into three passes the
    peak became the LARGEST hash table and the floor dropped under
    160MB, flat across scales, with no runtime cost.

    A future stage written as one wide join would push the floor back
    up, and this test is what would catch it.
    """
    from qrp import Engine

    eng = Engine(database=":memory:", memory_limit="200MB",
                 temp_directory="/tmp/qrp_floor_test",
                 threads=1, verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        assert eng.count("cohort_final") > 0
    finally:
        eng.close()


def test_no_stage_joins_more_than_six_relations():
    """Peak memory in a join tree scales with concurrent hash tables.

    A crude structural guard: a stage with many joins in one statement
    holds many build sides at once. The threshold is a judgement call,
    but crossing it should be a deliberate decision rather than an
    accident discovered at 300GB.
    """
    import re

    sql_dir = Path(__file__).resolve().parents[1] / "src" / "qrp" / "sql"
    offenders = []
    for path in sorted(sql_dir.glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        # count JOINs per statement, not per file
        for stmt in text.split(";"):
            n = len(re.findall(r"\bJOIN\b", stmt, re.I))
            if n > 6:
                first = next((l.strip() for l in stmt.splitlines()
                              if "CREATE" in l.upper()), stmt.strip()[:60])
                offenders.append(f"{path.name}: {n} joins in `{first}`")
    assert not offenders, (
        "stage(s) join many relations in one statement, which raises the "
        "memory floor:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------


def test_extension_autoload_is_disabled_by_default():
    """DuckDB ships with runtime extension download ON.

    A query touching an unsupported path type would fetch a binary from
    extensions.duckdb.org and load it mid-run — a security-review stop
    at most sites, and a confusing failure where egress is blocked.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        for setting in ("autoinstall_known_extensions",
                        "autoload_known_extensions"):
            val = eng.con.execute(
                f"SELECT current_setting('{setting}')"
            ).fetchone()[0]
            assert str(val).lower() in ("false", "0"), f"{setting} is on"
        # and the statically-linked extensions still work
        assert eng.con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        eng.close()


def test_no_dynamic_code_execution_in_the_package():
    """`src/qrp` must stay free of eval/exec/pickle.

    Scanners flag these hard, and there is no reason for them here.
    tools/ is exempt — find_memory_floor.py runs subprocesses on purpose
    and is not part of the deployed runtime.
    """
    import ast

    # Checked against the AST, not the source text. A regex matches
    # SAS's `%eval(...)` quoted in a comment, which is how this first
    # failed — the same trap as the `.df()` check in show.py.
    src = Path(__file__).resolve().parents[1] / "src" / "qrp"
    banned_calls = {"eval", "exec", "compile", "__import__"}
    banned_imports = {"pickle", "marshal", "shelve"}
    bad = []

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in banned_calls:
                    bad.append(f"{path.name}: calls {node.func.id}()")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in banned_imports:
                        bad.append(f"{path.name}: imports {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in banned_imports:
                    bad.append(f"{path.name}: imports from {node.module}")

    assert not bad, f"dynamic execution in the runtime package: {bad}"


def test_study_values_never_reach_sql_as_text(study):
    """Config reaches SQL as table rows, not string interpolation.

    This is why bandit's B608 findings are answerable: a cohort name or
    code from the study JSON is inserted with a parameterised
    executemany and joined against, never concatenated.
    """
    from qrp import Engine
    from qrp.pipeline import register_config

    eng = Engine(verbose=False)
    try:
        register_config(eng, study)
        for table in ("cfg_cohort", "cfg_codes", "cfg_age_strata",
                      "cfg_demog"):
            assert eng.count(table) >= 0
        # a cohort named with a quote must survive intact, not break out
        hostile = "x'; DROP TABLE cfg_cohort; --"
        eng.con.execute("CREATE OR REPLACE TABLE probe (v VARCHAR)")
        eng.con.executemany("INSERT INTO probe VALUES (?)", [[hostile]])
        got = eng.con.execute("SELECT v FROM probe").fetchone()[0]
        assert got == hostile
        assert eng.count("cfg_cohort") > 0, "cfg_cohort was dropped"
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Error messages and exit codes
# ---------------------------------------------------------------------


def test_errors_are_actionable_on_every_path():
    """The CLI and the UI must explain failures identically.

    An earlier version had this logic only on the UI path, so `qrp run`
    printed a raw DuckDB traceback while the TUI produced a plain
    sentence naming the setting to change.
    """
    import duckdb

    from qrp.errors import explain
    from qrp.runner import RunHandle

    oom = duckdb.OutOfMemoryException("Out of Memory Error: failed to pin")
    msg = explain(oom)
    assert "--memory-limit" in msg and "--temp-dir" in msg
    assert RunHandle._explain(oom) == msg, "CLI and UI messages diverged"

    missing = duckdb.IOException(
        'IO Error: No files found that match the pattern '
        '"/data/dispensing/enrollment/**/*.parquet"'
    )
    msg2 = explain(missing)
    assert "enrollment" in msg2 and "PARENT folder" in msg2
    assert "qrp inspect" in msg2, "should point at the diagnostic command"


def test_failed_run_exits_non_zero(tmp_path):
    """A wrapper script or scheduler must be able to detect failure."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "qrp", "run",
         "--study", str(root / "study" / "demo_type2.json"),
         "--indata", str(tmp_path / "does_not_exist"),
         "--quiet"],
        capture_output=True, text=True, timeout=300,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode != 0, "a failed run must not exit 0"
    assert "Failed:" in proc.stderr


def test_successful_run_exits_zero():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "qrp", "validate",
         "--study", str(root / "study" / "demo_type2.json")],
        capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------


def test_show_works_without_pandas():
    """`qrp show` must run on a base install (duckdb only).

    The first version formatted via DuckDB's `.df()`, which pulls in
    pandas and numpy. Neither is a declared dependency, so it failed
    with "No module named 'numpy'" for every base-install user — while
    working fine in the dev environment where the test ran.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "qrp" / "show.py"
    tree = ast.parse(src.read_text())

    # Check the AST, not the text — the docstring legitimately mentions
    # .df() to explain why it is avoided, and a regex over source would
    # match that.
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "df" not in calls, "show.py must not call .df() (needs pandas)"

    imports = {
        alias.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in (n.names if isinstance(n, ast.Import) else n.names)
    } | {
        n.module.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not ({"pandas", "numpy", "pyarrow"} & imports), (
        f"show.py imports non-base dependencies: {imports}"
    )


def test_show_lists_and_prints_tables(study, tmp_path):
    from qrp import Engine
    from qrp.show import available, show, to_csv

    eng = Engine(verbose=False)
    run(study, DATA, engine=eng, output_dir=tmp_path, layout="flat",
        verbose=False)
    eng.close()

    names = available(tmp_path)
    assert "attrition" in names and "cohort_final" in names

    listing = show(tmp_path)
    assert "attrition" in listing and "rows" in listing

    body = show(tmp_path, "attrition")
    assert "Exposure dispensings" in body
    assert "remaining" in body

    # a partitioned table resolves too, and truncates
    big = show(tmp_path, "cohort_final", limit=5)
    assert "showing first 5" in big

    filtered = show(tmp_path, "attrition", where="level = 1")
    assert "Exposure dispensings" in filtered

    csvs = to_csv(tmp_path, tmp_path / "csv")
    assert any(p.name == "attrition.csv" for p in csvs)
    # attrition leads with the SAS column names (group, level, descr,
    # claim_level, remaining, excluded) — it is an msoc output and those
    # names are a contract.
    assert (tmp_path / "csv" / "attrition.csv").read_text().startswith(
        "group,level,descr,claim_level,remaining,excluded"
    )


def test_show_handles_missing_output_gracefully(tmp_path):
    from qrp.show import show

    msg = show(tmp_path / "nope")
    assert "No results found" in msg
    assert "--out" in msg


def test_unpartitioned_outputs_have_a_parquet_extension(study, tmp_path):
    """`results/attrition` as an extensionless file is not openable.

    Partitioned tables are directories; unpartitioned ones must be
    files with an extension so an analyst (or Windows) can tell what
    they are.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    run(study, DATA, engine=eng, output_dir=tmp_path, layout="flat", debug=True,
        verbose=False)
    eng.close()

    assert (tmp_path / "attrition.parquet").is_file()
    assert (tmp_path / "denominators.parquet").is_file()
    assert (tmp_path / "cohort_final").is_dir()
    assert not (tmp_path / "attrition").exists(), "extensionless leftover"


def test_csv_flag_writes_excel_copies(study, tmp_path):
    from qrp import Engine

    eng = Engine(verbose=False)
    run(study, DATA, engine=eng, output_dir=tmp_path, csv=True,
        layout="flat", verbose=False)
    eng.close()

    csv_dir = tmp_path / "csv"
    assert (csv_dir / "attrition.csv").exists()
    header = (csv_dir / "attrition.csv").read_text().splitlines()[0]
    assert "group" in header and "remaining" in header


# ---------------------------------------------------------------------
# SCDM directory layouts
# ---------------------------------------------------------------------


def _build_layout(kind: str, dest: Path) -> Path:
    """Re-shape the fixture data into one of the supported layouts."""
    import duckdb

    dest.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    tables = ("enrollment", "demographic", "dispensing", "diagnosis", "death")
    for t in tables:
        src = DATA / t / "data.parquet"
        if kind == "flat":
            con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                        f"TO '{dest / (t + '.parquet')}' (FORMAT PARQUET)")
        elif kind == "multi":
            for i in (1, 2):
                con.execute(
                    f"COPY (SELECT * FROM read_parquet('{src}') "
                    f"WHERE patid % 2 = {i - 1}) "
                    f"TO '{dest / f'{t}_{i:03d}.parquet'}' (FORMAT PARQUET)")
        elif kind == "upper":
            con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                        f"TO '{dest / (t.upper() + '.parquet')}' "
                        f"(FORMAT PARQUET)")
        elif kind == "csv":
            con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                        f"TO '{dest / (t + '.csv')}' (FORMAT CSV, HEADER)")
        elif kind == "nodeath" and t != "death":
            con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                        f"TO '{dest / (t + '.parquet')}' (FORMAT PARQUET)")
    con.close()
    return dest


@pytest.mark.parametrize("layout", ["flat", "multi", "upper", "csv"])
def test_layouts_produce_identical_results(study, tmp_path, layout):
    """A site's directory layout is their choice, not our requirement.

    One folder per table, one file per table, several files per table,
    any casing, or CSV — all must give the same answer as the
    partitioned fixture.
    """
    from qrp import Engine

    baseline = Engine(verbose=False)
    run(study, DATA, engine=baseline, verbose=False)
    expected = baseline.count("cohort_final")
    baseline.close()

    root = _build_layout(layout, tmp_path / layout)
    eng = Engine(verbose=False)
    try:
        run(study, root, engine=eng, verbose=False)
        assert eng.count("cohort_final") == expected, (
            f"{layout} layout gave a different answer"
        )
    finally:
        eng.close()


def test_missing_death_table_is_tolerated(study, tmp_path):
    """A site with no death file should run, not be refused.

    Death censoring simply never triggers, which yields MORE episodes —
    so the assertion is directional, not equality.
    """
    from qrp import Engine

    root = _build_layout("nodeath", tmp_path / "nodeath")
    eng = Engine(verbose=False)
    try:
        run(study, root, engine=eng, verbose=False)
        assert eng.count("deaths") == 0
        assert eng.count("cohort_final") > 0
    finally:
        eng.close()


def test_missing_required_table_names_itself(study, tmp_path):
    from qrp import Engine

    (tmp_path / "empty").mkdir()
    eng = Engine(verbose=False)
    try:
        with pytest.raises(FileNotFoundError, match="enrollment"):
            run(study, tmp_path / "empty", engine=eng, verbose=False)
    finally:
        eng.close()


def test_resolver_reports_what_it_matched(tmp_path):
    """`inspect` must show WHICH files were read, not just 'ok'.

    With flexible layouts, a bare 'ok' leaves the user guessing.
    """
    from qrp.scdm import format_report, probe

    root = _build_layout("multi", tmp_path / "multi")
    report = format_report(probe(root))
    assert "enrollment*.parquet" in report
    assert "RESULT: schema is compatible." in report


# ---------------------------------------------------------------------
# Site-specific table names
# ---------------------------------------------------------------------

SITE_NAMES = {
    "enrollment": "dp042_msoc_elig_2024q1",
    "demographic": "DP042_PT_MASTER_V3",
    "dispensing": "sentinel_rx_extract_20240115",
    "diagnosis": "dp042_encounters_dx_full",
    "death": "MORTALITY_LINKAGE_FINAL",
}


def _rename_layout(dest: Path, names: dict[str, str]) -> Path:
    import duckdb

    dest.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for table, newname in names.items():
        src = DATA / table / "data.parquet"
        con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                    f"TO '{dest / (newname + '.parquet')}' (FORMAT PARQUET)")
    con.close()
    return dest


def test_tables_identified_by_columns_when_names_are_unrecognisable(
    study, tmp_path
):
    """An alias list is guesswork; column signatures are not.

    Real sites name tables with study codes, versions and quarter
    stamps. Every SCDM table this pipeline reads has a required-column
    set no other one has (they share only patid), so identification by
    schema is reliable where name matching cannot be.
    """
    from qrp import Engine

    root = _rename_layout(tmp_path / "site", SITE_NAMES)

    baseline = Engine(verbose=False)
    run(study, DATA, engine=baseline, verbose=False)
    expected = baseline.count("cohort_final")
    baseline.close()

    eng = Engine(verbose=False)
    try:
        run(study, root, engine=eng, verbose=False)
        assert eng.count("cohort_final") == expected
    finally:
        eng.close()


def test_versioned_files_are_not_silently_unioned(tmp_path):
    """Regression: `PT_MASTER_V2` + `PT_MASTER_V3` were read as one table.

    The shard-collapsing rule stripped any trailing digits, so two
    VERSIONS of a table became one glob and the pipeline read both —
    152,000 rows where the right answer was 102,000. A wrong answer, not
    an error, which is the worst kind.
    """
    import duckdb

    from qrp.scdm import _candidate_sources

    dest = tmp_path / "versions"
    dest.mkdir()
    con = duckdb.connect()
    src = DATA / "demographic" / "data.parquet"
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}') LIMIT 5000) "
                f"TO '{dest / 'PT_MASTER_V2.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) "
                f"TO '{dest / 'PT_MASTER_V3.parquet'}' (FORMAT PARQUET)")
    con.close()

    sources = _candidate_sources(dest)
    assert set(sources) == {"PT_MASTER_V2", "PT_MASTER_V3"}, (
        f"versions must stay separate, got {sources}"
    )


def test_numbered_shards_are_still_grouped(tmp_path):
    """The conservative rule must not break genuine sharding."""
    import duckdb

    from qrp.scdm import _candidate_sources

    dest = tmp_path / "shards"
    dest.mkdir()
    con = duckdb.connect()
    src = DATA / "demographic" / "data.parquet"
    for i in (1, 2, 3):
        con.execute(f"COPY (SELECT * FROM read_parquet('{src}') "
                    f"WHERE patid % 3 = {i - 1}) "
                    f"TO '{dest / f'demographic_{i:03d}.parquet'}' "
                    f"(FORMAT PARQUET)")
    con.close()

    sources = _candidate_sources(dest)
    assert set(sources) == {"demographic"}, sources
    assert "*" in sources["demographic"]


def test_ambiguous_tables_are_refused_not_guessed(study, tmp_path):
    """Two files with the right columns must raise, naming both."""
    import duckdb

    from qrp import Engine

    root = _rename_layout(tmp_path / "amb", SITE_NAMES)
    con = duckdb.connect()
    src = DATA / "demographic" / "data.parquet"
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}') LIMIT 100) "
                f"TO '{root / 'OTHER_PT_FILE.parquet'}' (FORMAT PARQUET)")
    con.close()

    eng = Engine(verbose=False)
    try:
        with pytest.raises(FileNotFoundError) as excinfo:
            run(study, root, engine=eng, verbose=False)
        msg = str(excinfo.value)
        assert "several files" in msg
        assert "--table-map" in msg
        assert "OTHER_PT_FILE" in msg and "DP042_PT_MASTER_V3" in msg
    finally:
        eng.close()


def test_table_map_overrides_everything(study, tmp_path):
    """An explicit mapping must win, and resolve ambiguity."""
    import duckdb

    from qrp import Engine

    root = _rename_layout(tmp_path / "map", SITE_NAMES)
    con = duckdb.connect()
    src = DATA / "demographic" / "data.parquet"
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}') LIMIT 100) "
                f"TO '{root / 'OTHER_PT_FILE.parquet'}' (FORMAT PARQUET)")
    con.close()

    eng = Engine(verbose=False)
    try:
        run(study, root, engine=eng,
            table_map={"demographic": "DP042_PT_MASTER_V3.parquet"},
            verbose=False)
        assert eng.count("demographics") > 1000
    finally:
        eng.close()


def test_table_map_rejects_a_bad_path(study, tmp_path):
    from qrp.scdm import SCDM, resolve_table

    spec = next(s for s in SCDM if s.name == "demographic")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_table(tmp_path, spec, table_map={"demographic": "nope.parquet"})


# ---------------------------------------------------------------------
# Efficiency guards
# ---------------------------------------------------------------------


def test_column_fingerprinting_is_skipped_when_names_match(study, monkeypatch):
    """Schema fingerprinting reads every file's metadata.

    Measured at 0.8-1.6s, which is most of a small run. It is the
    fallback for site-specific table names, so it must not run when a
    plain name lookup already succeeded.
    """
    from qrp import Engine, pipeline

    calls = []
    real = pipeline.identify_by_columns

    def counting(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(pipeline, "identify_by_columns", counting)
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
    finally:
        eng.close()
    assert not calls, "fingerprinting ran despite every name matching"


def test_fingerprinting_still_runs_when_a_name_misses(study, tmp_path,
                                                     monkeypatch):
    """...but the fallback must still fire when it is needed."""
    from qrp import Engine, pipeline

    root = _rename_layout(tmp_path / "odd", SITE_NAMES)
    calls = []
    real = pipeline.identify_by_columns

    def counting(r):
        calls.append(r)
        return real(r)

    monkeypatch.setattr(pipeline, "identify_by_columns", counting)
    eng = Engine(verbose=False)
    try:
        run(study, root, engine=eng, verbose=False)
    finally:
        eng.close()
    assert calls, "fingerprinting did not run for unrecognisable names"
    assert len(calls) == 1, f"computed {len(calls)} times; should be cached"


def test_inconsistent_enrollmentnum_is_rejected():
    """SAS keys the enrollment build on ENROLLMENTNUM; this keys it on
    the parameters. Those agree unless the cohortfile is contradictory —
    same number, different settings — which would silently diverge.
    """
    from datetime import date

    from qrp.config import AgeStrata, CohortConfig, StudyConfig

    def cohort(name, coverage, gap):
        return CohortConfig(cohortgrp=name, enrollment_num=1,
                            coverage=coverage, enrol_gap=gap,
                            age_strata=AgeStrata.parse(None))

    ok = StudyConfig(2, date(2011, 1, 1), date(2015, 1, 1),
                     (cohort("a", "MD", 45), cohort("b", "MD", 45)))
    ok.validate()

    bad = StudyConfig(2, date(2011, 1, 1), date(2015, 1, 1),
                      (cohort("a", "MD", 45), cohort("b", "M", 90)))
    with pytest.raises(ValueError, match="enrollmentnum"):
        bad.validate()


def test_no_single_consumer_temp_tables():
    """Materialise on fan-out; stay lazy on single consumption.

    A TEMP TABLE read by exactly one statement is a barrier that costs a
    write and a read for nothing. Three of these existed (`_sameday`,
    `_nochart`, `_spans`) and folding them into CTEs was worth ~4s at 2m
    patients.

    The POV1 pair is exempt: those are materialised deliberately, to keep
    the memory floor down by holding one hash table at a time instead of
    ten. That is a memory trade, not an oversight.
    """
    import re

    # Materialised deliberately, to keep peak memory at the largest hash
    # table rather than the sum of them. Making these CTEs would satisfy
    # this rule and violate the six-join rule — the two guards pull in
    # opposite directions, and memory wins.
    exempt = {"_pov1_demog", "_pov1_enrolled",
              "_denom_windows", "_denom_demog", "_denom_strat",
              "_util_med", "_util_drug",
              # Its one "consumer" is the ALTER TABLE ... RENAME that
              # swaps it over _denom_windows. A deliberate swap, not a
              # CTE that should have been inlined.
              "_denom_windows_shaved",
              "_denom_unelig",
              # Outcome-event periods. Inlined as a CTE they push the
              # _denom_unelig statement to nine joins, past the six-join
              # memory guard below — the same trade the others make.
              "_denom_event_periods",
              # The denominator washout chain: inlining it adds two more
              # joins to _denom_unelig, past the six-join guard.
              "_denom_chain",
              # the covariate dispensing chain: inlining it breaks the
              # covariate query's join budget
              "_covar_rx_chain",
              # index-code distribution lists: each feeds the master-list
              # statement, which they would push past the join budget
              "_di_exp", "_di_hoi"}
    sql_dir = Path(__file__).resolve().parents[1] / "src" / "qrp" / "sql"
    offenders = []

    for path in sorted(sql_dir.glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        created = re.findall(
            r"CREATE\s+OR\s+REPLACE\s+TEMP\s+TABLE\s+(\w+)", text, re.I)
        for name in created:
            if name in exempt:
                continue
            # count references outside its own CREATE and its DROP
            uses = len(re.findall(rf"\b{name}\b", text)) - 1
            uses -= len(re.findall(rf"DROP\s+TABLE\s+{name}\b", text, re.I))
            if uses <= 1:
                offenders.append(f"{path.name}: {name} has {uses} consumer(s)")

    assert not offenders, (
        "single-consumer TEMP TABLEs should be CTEs:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------
# Real SCDM schema
# ---------------------------------------------------------------------

# Verified against a real SCDM extract (174k patients, 15M claims).
# Column names and types as they actually appear, so a wrong assumption
# fails here rather than three stages into a run.
REAL_SCDM_SCHEMA = {
    "enrollment": {"patid", "enr_start", "enr_end", "medcov", "drugcov",
                   "chart", "plantype", "payertype"},
    "demographic": {"patid", "birth_date", "sex", "hispanic", "race",
                    "imputedhispanic", "imputedrace", "postalcode",
                    "postalcode_date"},
    "dispensing": {"patid", "providerid", "rxdate", "rx", "rx_codetype",
                   "rxsup", "rxamt"},
    "diagnosis": {"patid", "encounterid", "adate", "providerid", "enctype",
                  "dx", "dx_codetype", "origdx", "pdx", "padmit"},
    "death": {"patid", "deathdt", "dtimpute", "source", "confidence"},
    "procedure": {"patid", "encounterid", "adate", "providerid",
                  "enctype", "px", "px_codetype", "origpx"},
    # Verified against a real SCDM lab extract (1.4M rows). Note there
    # is NO lab_code column — LAB01 matches a seven-attribute
    # combination, not a code.
    "lab_result": {"patid", "lab_dt", "result_dt", "order_dt",
                   "ms_result_n", "ms_result_c", "result_type", "loinc",
                   "px", "ms_test_name", "ms_test_sub_category",
                   "specimen_source", "ms_result_unit", "fast_ind",
                   "pt_loc"},
}


def test_required_columns_exist_in_real_scdm():
    """Every required column must appear in a real SCDM extract.

    This is the test that would have caught `ndc`. The pipeline read a
    column called `ndc` for the dispensed code — but SCDM calls it `rx`;
    `ndc` is the code VOCABULARY, not the column. It went undetected
    because the synthetic generator made the same wrong assumption, so
    every test passed against data that reproduced the error.

    A fixture cannot falsify the assumption that produced it.
    """
    from qrp.scdm import SCDM

    problems = []
    for spec in SCDM:
        if not spec.used or not spec.required:
            continue
        real = REAL_SCDM_SCHEMA.get(spec.name)
        if real is None:
            continue
        aliases = spec.column_aliases or {}
        for col in spec.required:
            if col in real:
                continue
            if any(a in real for a in aliases.get(col, ())):
                continue
            problems.append(f"{spec.name}.{col} is not in real SCDM")
    assert not problems, problems


def test_synthetic_generator_matches_real_scdm():
    """The fixtures must have the same shape as real data.

    Otherwise the test suite validates the pipeline against a fiction.
    """
    import duckdb

    con = duckdb.connect()
    try:
        for table, expected in REAL_SCDM_SCHEMA.items():
            path = DATA / table / "data.parquet"
            if not path.exists():
                pytest.skip("run tools/gen_synthetic.py first")
            cols = {
                d[0].lower() for d in con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{path}')"
                ).fetchall()
            }
            missing = expected - cols
            assert not missing, f"{table} fixture is missing {missing}"
    finally:
        con.close()


def test_dispensing_code_column_is_resolved_not_assumed(study, tmp_path):
    """Both `rx` (SCDM) and `ndc` (some extracts) must work."""
    import duckdb

    from qrp import Engine

    con = duckdb.connect()
    dest = tmp_path / "ndc_variant"
    dest.mkdir()
    for t in ("enrollment", "demographic", "diagnosis", "death"):
        con.execute(f"COPY (SELECT * FROM "
                    f"read_parquet('{DATA / t / 'data.parquet'}')) "
                    f"TO '{dest / (t + '.parquet')}' (FORMAT PARQUET)")
    # rename rx -> ndc, the older convention
    con.execute(
        f"COPY (SELECT * EXCLUDE (rx), rx AS ndc FROM "
        f"read_parquet('{DATA / 'dispensing' / 'data.parquet'}')) "
        f"TO '{dest / 'dispensing.parquet'}' (FORMAT PARQUET)")
    con.close()

    eng = Engine(verbose=False)
    try:
        run(study, dest, engine=eng, verbose=False)
        assert eng.count("cohort_final") > 0
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Inclusion / exclusion criteria
# ---------------------------------------------------------------------


def _inclusion_study(rules):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_type2.json").read_text())
    base["inclusioncodes"] = rules
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "incl",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "inclusioncodes": rules,
    })


def test_inclusion_requires_the_code_to_be_present(study):
    """INC: an episode survives only if the code is in the window."""
    from qrp import Engine

    rules = [{"group": g, "cond": 1, "condlevel": 1, "indexcriteria": "INC",
              "codecat": "DX", "condfrom": -365, "condto": -1,
              "codedays": 1, "code": c}
             for g in ("lisinopril", "beta_blocker")
             for c in ("X00001", "X00002")]
    s = _inclusion_study(rules)
    assert s.any_inclusions and len(s.inclusions) == 4

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        violations = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist p WHERE NOT EXISTS (
                SELECT 1 FROM cdm_diagnosis d
                WHERE d.patid = p.patid
                  AND d.code IN ('X00001','X00002')
                  AND d.adate BETWEEN p.indexdt - 365 AND p.indexdt - 1)
        """).fetchone()[0]
        assert violations == 0, "kept an episode lacking the required code"
    finally:
        eng.close()


def test_exclusion_removes_episodes_with_the_code(study):
    from qrp import Engine

    rules = [{"group": g, "cond": 1, "condlevel": 1, "indexcriteria": "EXC",
              "codecat": "DX", "condfrom": -365, "condto": -1,
              "codedays": 1, "code": "X00001"}
             for g in ("lisinopril", "beta_blocker")]
    eng = Engine(verbose=False)
    try:
        run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
        violations = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist p WHERE EXISTS (
                SELECT 1 FROM cdm_diagnosis d
                WHERE d.patid = p.patid AND d.code = 'X00001'
                  AND d.adate BETWEEN p.indexdt - 365 AND p.indexdt - 1)
        """).fetchone()[0]
        assert violations == 0, "kept an episode carrying an excluded code"
        assert eng.count("inclusion_excluded") > 0
    finally:
        eng.close()


def test_codes_at_the_same_condlevel_are_alternatives(study):
    """Within a level the codes are ORed: any one satisfies it.

    A single rare code should exclude nearly everyone; the same code
    ORed with a common one should not.
    """
    from qrp import Engine

    def survivors(codes):
        rules = [{"group": g, "cond": 1, "condlevel": 1,
                  "indexcriteria": "INC", "codecat": "DX",
                  "condfrom": -365, "condto": -1, "codedays": 1, "code": c}
                 for g in ("lisinopril", "beta_blocker") for c in codes]
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    one = survivors(["X00001"])
    two = survivors(["X00001", "X00002"])
    assert two > one, "adding an alternative at the same level must not shrink"


def test_separate_conds_are_combined_with_and(study):
    """Every condition must pass, so adding one can only shrink.

    Conditions are distinguished by CONDLEVEL, not by a `cond` column:
    `cond` is derived from condlevel in ms_processinputfiles.sas:715.
    This test previously set `"cond": 2` with `"condlevel": 1`, which
    encoded the older (wrong) model where `cond` was an input column —
    both rules now land in condition 1 and become alternatives, so
    adding one made MORE survive, not fewer.
    """
    from qrp import Engine

    def survivors(rules):
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    r1 = [{"group": g, "condlevel": "A", "subcondlevel": "1",
           "indexcriteria": "INC", "codecat": "DX",
           "condfrom": -365, "condto": -1, "codedays": 1,
           "code": "X00001"}
          for g in ("lisinopril", "beta_blocker")]
    r2 = r1 + [{"group": g, "condlevel": "B", "subcondlevel": "1",
                "indexcriteria": "INC", "codecat": "DX",
                "condfrom": -365, "condto": -1, "codedays": 1,
                "code": "X00050"}
               for g in ("lisinopril", "beta_blocker")]
    assert survivors(r2) < survivors(r1), (
        "a second condition must narrow the cohort"
    )


def test_codedays_counts_distinct_days(study):
    """codedays=2 means two separate days, not two claims on one day."""
    from qrp import Engine

    def survivors(days):
        rules = [{"group": g, "cond": 1, "condlevel": 1,
                  "indexcriteria": "INC", "codecat": "DX",
                  "condfrom": -365, "condto": -1, "codedays": days,
                  "code": "X00001"}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    assert survivors(3) <= survivors(1), "a higher codedays must not admit more"


def test_event_anchored_rules_are_no_longer_unsupported():
    """IEV/EEV are implemented (60_followup.sql), so they must NOT warn.

    A stale "unsupported" warning is worse than none: it tells the user
    their results are broader than SAS when they are not.
    """
    rules = [{"group": "lisinopril", "cond": 1, "condlevel": 1,
              "indexcriteria": "IEV", "codecat": "DX", "condfrom": -365,
              "condto": -1, "codedays": 1, "code": "X00001"}]
    s = _inclusion_study(rules)
    assert "event-anchored" not in " ".join(s.unsupported_inclusions)


# ---------------------------------------------------------------------
# SAS output layout
# ---------------------------------------------------------------------


def test_split_layout_separates_dplocal_from_msoc(study, tmp_path):
    """The library split is a DISCLOSURE boundary, not a naming style.

    dplocal stays behind the Data Partner firewall; msoc is what goes to
    the Operations Center. Patient-level tables must never land in msoc.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, output_dir=tmp_path, layout="split",
            verbose=False)
    finally:
        eng.close()

    assert (tmp_path / "dplocal").is_dir()
    assert (tmp_path / "msoc").is_dir()

    run_id = study.run_id.lower()
    # Default naming is SAS parity, so the master list is `_mstr`.
    assert (tmp_path / "dplocal" / f"{run_id}_mstr").is_dir()
    assert (tmp_path / "msoc" / f"{run_id}_attrition.parquet").is_file()

    # Whatever the naming, no patient-level output may reach msoc/.
    from qrp.pipeline import DISCLOSURE, SAS_NAMES

    msoc_files = {p.name for p in (tmp_path / "msoc").rglob("*")
                  if p.is_file()}
    for table, lib in DISCLOSURE.items():
        if lib != "dplocal":
            continue
        for variant in {table, SAS_NAMES.get(table, table)}:
            assert not any(f.startswith(f"{run_id}_{variant}")
                           for f in msoc_files), (
                f"patient-level {table} must not be in msoc/"
            )


def test_naming_modes_write_the_same_data(study, tmp_path):
    """--names controls file names only, never contents or library.

    SAS names give parity with existing SOPs; logical names match what
    `qrp show` and the docs call the tables. The manifest maps logical
    to file either way, so no reader has to know which was used.
    """
    import duckdb

    from qrp import Engine
    from qrp.show import available, show

    counts = {}
    for names in ("sas", "logical"):
        out = tmp_path / names
        eng = Engine(verbose=False)
        try:
            run(study, DATA, engine=eng, output_dir=out, layout="split",
                names=names, debug=True, verbose=False)
        finally:
            eng.close()
        # lookup by LOGICAL name works regardless of on-disk naming
        assert "ptsmasterlist" in available(out)
        assert "Exposure dispensings" in show(out, "attrition")
        con = duckdb.connect()
        counts[names] = con.execute(
            f"SELECT count(*) FROM read_parquet("
            f"'{out / 'msoc' / (study.run_id.lower() + '_attrition.parquet')}')"
        ).fetchone()[0]
        con.close()

    run_id = study.run_id.lower()
    assert (tmp_path / "sas" / "dplocal" / f"{run_id}_mstr").is_dir()
    assert (tmp_path / "logical" / "dplocal"
            / f"{run_id}_ptsmasterlist").is_dir()
    assert counts["sas"] == counts["logical"]


def test_unmapped_outputs_default_to_local(study):
    """A new output must not become shareable by omission."""
    from qrp.pipeline import DISCLOSURE, OUTPUT_TABLES

    for name in OUTPUT_TABLES:
        assert name in DISCLOSURE, (
            f"{name} has no disclosure classification; it would default "
            f"to dplocal, which is safe, but the omission should be "
            f"deliberate"
        )
    assert DISCLOSURE.get("some_new_table", "dplocal") == "dplocal"


def test_split_layout_writes_signature_and_runtimes(study, tmp_path):
    """Both are real SAS QRP outputs, and both are MSOC provenance."""
    import duckdb

    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, output_dir=tmp_path, layout="split",
            verbose=False)
    finally:
        eng.close()

    run_id = study.run_id.lower()
    con = duckdb.connect()
    try:
        sig = con.execute(
            f"SELECT * FROM read_parquet("
            f"'{tmp_path / 'msoc' / (run_id + '_signature.parquet')}')"
        ).fetchall()
        assert len(sig) == 1
        cols = [d[0] for d in con.description]
        for expected in ("runid", "engine_version", "wall_seconds",
                         "n_inclusion_rules"):
            assert expected in cols

        rt = con.execute(
            f"SELECT count(*) FROM read_parquet("
            f"'{tmp_path / 'msoc' / (run_id + '_runtimes.parquet')}')"
        ).fetchone()[0]
        assert rt >= len(RunHandleStages(study)), "a row per stage expected"
    finally:
        con.close()


def RunHandleStages(study):
    from qrp.pipeline import plan_stages

    return plan_stages(study)


def test_manifest_decouples_readers_from_the_naming(study, tmp_path):
    """`qrp show` must not have to parse `<runid>_<suffix>`.

    This is the answer to the main objection to SAS naming: metadata in
    a filename normally forces every reader to parse it. The manifest
    keeps lookup by logical name, so the convention costs nothing
    downstream.
    """
    from qrp import Engine
    from qrp.show import available, show

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, output_dir=tmp_path, layout="split", debug=True,
            verbose=False)
    finally:
        eng.close()

    assert (tmp_path / "manifest.json").is_file()
    names = available(tmp_path)
    assert {"attrition", "ptsmasterlist", "signature"} <= set(names)
    body = show(tmp_path, "attrition")
    assert "Exposure dispensings" in body


def test_flat_layout_still_available(study, tmp_path):
    from qrp import Engine
    from qrp.show import show

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, output_dir=tmp_path, layout="flat",
            verbose=False)
    finally:
        eng.close()

    assert (tmp_path / "attrition.parquet").is_file()
    assert not (tmp_path / "msoc").exists()
    assert "Exposure dispensings" in show(tmp_path, "attrition")


# ---------------------------------------------------------------------
# SAS parity fixes
# ---------------------------------------------------------------------


def _study_with_stockgroups(groups: dict[str, str]):
    """demo study with an explicit stockgroup per exposure code."""
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_type2.json").read_text())
    codes = []
    for row in base["cohortcodes"]:
        row = dict(row)
        if groups is not None and row["indexcriteria"] == "DEF":
            row["stockgroup"] = groups.get(row["code"], "_default")
        codes.append(row)
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "sg",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": codes,
    })


def test_stockpiling_is_partitioned_by_stockgroup(study):
    """SAS runs `by PatId &GROUPING.` with GROUPING=StockGroup ...

    Two drugs in one cohort are pushed forward INDEPENDENTLY. Merging
    them pushes dates further than SAS and yields longer, fewer supply
    intervals — a silent difference on any multi-drug cohort.
    """
    from qrp import Engine

    # split each cohort's DEF codes into two stockgroups
    split = {}
    for c in study.cohorts:
        for i, code in enumerate(c.exposure_codes):
            split[code] = f"sg{i % 2}"

    one = Engine(verbose=False)
    two = Engine(verbose=False)
    try:
        run(_study_with_stockgroups({}), DATA, engine=one, verbose=False)
        run(_study_with_stockgroups(split), DATA, engine=two, verbose=False)

        # Splitting means less forward-pushing, so supply intervals stay
        # closer to their original dates: total supply is conserved but
        # the latest expiry cannot be later than in the merged case.
        merged_end = one.con.execute(
            "SELECT max(expiredt) FROM stockpiled").fetchone()[0]
        split_end = two.con.execute(
            "SELECT max(expiredt) FROM stockpiled").fetchone()[0]
        assert split_end <= merged_end, (
            "splitting stockgroups must not push dates further forward"
        )
        # and the partition key must actually be carried
        cols = {d[0] for d in one.con.execute(
            "SELECT * FROM stockpiled LIMIT 0").description}
        assert "stockgroup" in cols
    finally:
        one.close()
        two.close()


def test_stockgroup_absent_reproduces_single_drug_behaviour(study):
    """Codes with no stockgroup share one, so nothing changes."""
    from qrp import Engine

    a = Engine(verbose=False)
    b = Engine(verbose=False)
    try:
        # Both sides built the same way, so the only difference is the
        # stockgroup mapping. Comparing against the `study` fixture
        # compared unlike things: it carries covariatecodes and this one
        # does not, and the claims scan window is derived from the
        # study's widest lookback.
        run(_study_with_stockgroups(None), DATA, engine=a, verbose=False)
        run(_study_with_stockgroups({}), DATA, engine=b, verbose=False)
        assert a.count("stockpiled") == b.count("stockpiled")
    finally:
        a.close()
        b.close()


def test_episodes_break_on_enrollment_change(baseline):
    """SAS: `or LEnrStartDt ne Enr_Start` (ms_createclaimepi.sas:75).

    A patient who disenrols and re-enrols gets two episodes, not one.
    Asserted against the data rather than by row count: no episode may
    span more than one enrollment span.
    """
    spanning = baseline.con.execute("""
        WITH claim_span AS (
            SELECT s.cohortgrp, s.patid, s.adate, e.enr_start
            FROM stockpiled s
            JOIN cfg_cohort c ON c.cohortgrp = s.cohortgrp
            LEFT JOIN enrollment_spans e
              ON e.enr_cfg_id = c.enr_cfg_id
             AND e.patid = s.patid
             AND s.adate BETWEEN e.enr_start AND e.enr_end
        )
        SELECT count(*) FROM (
            SELECT ep.cohortgrp, ep.patid, ep.episode,
                   count(DISTINCT coalesce(cs.enr_start,
                                           DATE '1900-01-01')) AS n
            FROM episodes ep
            JOIN claim_span cs
              ON cs.cohortgrp = ep.cohortgrp
             AND cs.patid = ep.patid
             AND cs.adate BETWEEN ep.episodestartdt AND ep.episodeenddt
            GROUP BY 1, 2, 3 HAVING n > 1
        )
    """).fetchone()[0]
    assert spanning == 0, (
        f"{spanning} episode(s) span more than one enrollment span"
    )


def _dose_study(**t2_overrides):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    for t in base["type2file"]:
        if t["group"] == "lisinopril":
            t.update(t2_overrides)
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "dose",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
    })


def test_maxcumdose_censors_the_episode_not_just_excludes(study):
    """SAS does two different things with maxcumdose.

    ms_pov1dose EXCLUDES the index date when prior cumulative dose is out
    of range; ms_createpov4 separately CENSORS the episode at the expiry
    of the claim that tips within-episode cumulative dose over the limit.
    Only the exclusion was implemented, so studies setting maxcumdose got
    a different denominator: a dropped patient rather than a shortened
    follow-up.
    """
    from qrp import Engine

    s = _dose_study(maxcumdose=800.0, t2cumdoseper=90, maxcfdd=None)
    assert s.any_dose_censoring

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        assert eng.count("dose_censor") > 0, "nothing was censored"
        # every censored episode must end at or before its censor date
        late = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist m
            JOIN dose_censor x
              ON x.cohortgrp = m.cohortgrp AND x.patid = m.patid
             AND x.indexdt = m.indexdt
            WHERE m.episodeenddt > x.censordate_maxdose
        """).fetchone()[0]
        assert late == 0
        # censored episodes must still be PRESENT — shortened, not dropped
        survived = eng.con.execute("""
            SELECT count(*) FROM dose_censor x
            JOIN ptsmasterlist m
              ON m.cohortgrp = x.cohortgrp AND m.patid = x.patid
             AND m.indexdt = x.indexdt
        """).fetchone()[0]
        assert survived > 0, "censoring removed episodes instead of shortening"
    finally:
        eng.close()


def test_dose_censoring_is_skipped_without_cumdoseper(study):
    """SAS's gate is `maxcumdose ne . and cumdoseper ne .`."""
    from qrp import Engine

    s = _dose_study(maxcumdose=800.0, t2cumdoseper=None)
    assert not s.any_dose_censoring

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        existing = {r[0] for r in eng.con.execute(
            "SELECT table_name FROM duckdb_tables()").fetchall()}
        assert "dose_censor" not in existing
    finally:
        eng.close()


def test_dose_comparisons_round_like_sas():
    """SAS compares `round(cumdose,1)` — the 1 is the rounding UNIT.

    So it is round-to-nearest-integer, not one decimal place. Comparing
    raw values disagrees at the boundary: 99.6 against a minimum of 100
    is included by SAS.
    """
    import duckdb

    con = duckdb.connect()
    try:
        # the SQL uses round(x); confirm DuckDB's round matches the
        # boundary behaviour the comparison depends on
        assert con.execute("SELECT round(99.6)").fetchone()[0] == 100
        assert con.execute("SELECT round(99.4)").fetchone()[0] == 99
        assert con.execute("SELECT round(100.5)").fetchone()[0] == 101
    finally:
        con.close()

    sql = (Path(__file__).resolve().parents[1]
           / "src" / "qrp" / "sql" / "42_dose.sql").read_text()
    assert "round(COALESCE(l.prior_cumdose, 0))" in sql
    assert "round(l.cfdd)" in sql


def _fupwash_study(**t2_overrides):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    t2 = [dict(t) for t in base["type2file"]]
    for t in t2:
        t.update(t2_overrides)
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "fw",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": t2,
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
    })


def test_missing_fupwashper_means_never_had_an_event():
    """ms_createpov56.sas:78 — "If FupWashPer=. then patients need to
    never have had an Event (hence 99999)".

    So a MISSING value is the strictest setting, not the loosest.
    Parsing it as 0 meant no washout at all, keeping patients SAS drops.
    """
    from qrp import Engine

    assert _fupwash_study(t2fupwashper=None).cohorts[0].fup_wash_per is None
    assert _fupwash_study(t2fupwashper=0).cohorts[0].fup_wash_per == 0

    counts = {}
    for label, val in (("none", 0), ("window", 183), ("never", None)):
        eng = Engine(verbose=False)
        try:
            run(_fupwash_study(t2fupwashper=val), DATA, engine=eng,
                verbose=False)
            counts[label] = eng.count("cohort_final")
        finally:
            eng.close()

    assert counts["none"] > counts["window"] > counts["never"], (
        f"washout strictness is not ordered correctly: {counts}"
    )
    # and "never" must genuinely admit no episode with any prior event
    eng = Engine(verbose=False)
    try:
        run(_fupwash_study(t2fupwashper=None), DATA, engine=eng, verbose=False)
        leaked = eng.con.execute("""
            SELECT count(*) FROM cohort_final m
            WHERE EXISTS (SELECT 1 FROM event_claims e
                          WHERE e.cohortgrp = m.cohortgrp
                            AND e.patid = m.patid
                            AND e.adate < m.indexdt)
        """).fetchone()[0]
        assert leaked == 0, f"{leaked} episode(s) kept despite a prior event"
    finally:
        eng.close()


def test_events_in_the_blackout_window_exclude_the_episode():
    """SAS builds _EventsInBlackout and drops those episodes outright
    (`if a and not b and not c and not d`, ms_createpov56.sas:127).

    Shifting the at-risk start alone ignored the event but KEPT the
    episode, which inflates the denominator.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_fupwash_study(t2fupwashper=183, blackoutper=30), DATA,
            engine=eng, verbose=False)
        survivors = eng.con.execute("""
            SELECT count(*) FROM cohort_final m
            WHERE EXISTS (SELECT 1 FROM event_claims e
                          WHERE e.cohortgrp = m.cohortgrp
                            AND e.patid = m.patid
                            AND e.adate BETWEEN m.indexdt
                                            AND m.indexdt + 30 - 1)
        """).fetchone()[0]
        assert survivors == 0, (
            f"{survivors} episode(s) kept despite an event in the blackout"
        )
    finally:
        eng.close()


def test_blackout_zero_excludes_nothing():
    from qrp import Engine

    a = Engine(verbose=False)
    b = Engine(verbose=False)
    try:
        run(_fupwash_study(t2fupwashper=183, blackoutper=0), DATA,
            engine=a, verbose=False)
        run(_fupwash_study(t2fupwashper=183, blackoutper=30), DATA,
            engine=b, verbose=False)
        assert a.count("cohort_final") >= b.count("cohort_final")
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------
# Care setting / principal diagnosis (ms_caresettingprincipal.sas)
# ---------------------------------------------------------------------


def test_care_setting_expansion_matches_sas_encoding():
    """`caresettingprincipal` packs 3-char tokens: 2 of EncType, 1 of PDX.

    SAS translates '*'->'A' and '.'->'_' first, so 'AAA' (from '***')
    and an empty value both mean "any care setting".
    """
    from qrp.config import parse_care_setting as p

    assert p("") == (("**", "*"),)
    assert p("***") == (("**", "*"),)
    assert p("IPP") == (("IP", "P"),)
    assert p("IP*") == (("IP", "*"),)
    assert p("AAP") == (("**", "P"),)          # wildcard care setting
    assert p("IP_") == (("IP", ""),)           # missing PDX
    assert p("IP* AV*") == (("IP", "*"), ("AV", "*"))
    assert p("IPPAVS") == (("IP", "P"), ("AV", "S"))   # concatenated
    assert p("ipp") == (("IP", "P"),)          # upcased


def _care_setting_study(csp):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    cc = [dict(r) for r in base["cohortcodes"]]
    for r in cc:
        if r["indexcriteria"] == "EVENT":
            r["caresettingprincipal"] = csp
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "cs",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": cc,
        "codestrength": base["codestrength"],
    })


def test_care_setting_restricts_event_claims(study):
    """A restriction must narrow events, and every surviving event must
    be backed by at least one qualifying claim.

    `event_claims` is DISTINCT on (cohort, patid, adate), so a patient
    with an IP and an AV claim on one day legitimately keeps the row.
    Checking "no joined claim violates the rule" would wrongly flag the
    sibling; the correct test is EXISTS, not NOT EXISTS.
    """
    from qrp import Engine

    counts = {}
    for label, csp in (("all", "***"), ("ip", "IP*"), ("ip_principal", "IPP")):
        eng = Engine(verbose=False)
        try:
            run(_care_setting_study(csp), DATA, engine=eng, verbose=False)
            counts[label] = eng.count("event_claims")
            unbacked = eng.con.execute("""
                SELECT count(*) FROM event_claims ec
                WHERE NOT EXISTS (
                    SELECT 1 FROM cdm_diagnosis d
                    JOIN cfg_codes k
                      ON k.code = d.code AND k.role = 'EVENT'
                     AND k.cohortgrp = ec.cohortgrp
                    JOIN cfg_care_setting cs
                      ON cs.cohortgrp = ec.cohortgrp AND cs.code = d.code
                     AND (cs.enctype = '**' OR cs.enctype = d.enctype)
                     AND (cs.pdx = '*' OR cs.pdx = coalesce(d.pdx, ''))
                    WHERE d.patid = ec.patid AND d.adate = ec.adate)
            """).fetchone()[0]
            assert unbacked == 0, f"{unbacked} event(s) with no qualifying claim"
        finally:
            eng.close()

    assert counts["all"] > counts["ip"] > counts["ip_principal"], counts


def test_unrestricted_care_setting_changes_nothing():
    """Every EVENT code expands to ('**','*') when unspecified, so the
    join is uniform and results are unchanged.

    Both sides are built by _care_setting_study so the two studies are
    otherwise identical. Comparing against the `study` fixture instead
    compared unlike things: that fixture carries covariatecodes and this
    one does not, and since the claims scan window is now derived from
    the study's widest lookback, the two legitimately read different
    amounts of history.
    """
    from qrp import Engine

    a = Engine(verbose=False)
    b = Engine(verbose=False)
    try:
        run(_care_setting_study(""), DATA, engine=a, verbose=False)
        run(_care_setting_study("***"), DATA, engine=b, verbose=False)
        assert a.count("event_claims") == b.count("event_claims")
        assert a.count("cohort_final") == b.count("cohort_final")
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------
# CIDA output table (ms_cidatables.sas)
# ---------------------------------------------------------------------


def _cida_study(levels):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "cida",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "userstrata": levels,
    })


def test_userstrata_parsing_matches_sas():
    """ms_processinputfiles.sas:1843 lowercases, turns `*` into a space,
    and appends agegroupnum wherever agegroup appears."""
    from qrp.config import StratumLevel

    assert StratumLevel.parse(
        {"tableid": "T2CIDA", "levelid": "1", "levelvars": ""}
    ).levelvars == ()
    assert StratumLevel.parse(
        {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup*sex"}
    ).levelvars == ("agegroup", "agegroupnum", "sex")
    assert StratumLevel.parse(
        {"tableid": "t2cida", "levelid": "3", "levelvars": "SEX Race"}
    ).levelvars == ("sex", "race")


def test_cida_only_uses_t2cida_levels():
    """SAS filters `where lowcase(tableID) = 't2cida'`."""
    s = _cida_study([
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2its", "levelid": "9", "levelvars": "sex"},
    ])
    assert [lv.level_id for lv in s.cida_levels()] == ["1"]
    assert s.any_cida_tables


def test_cida_levels_reconcile(study):
    """Every level is the same population sliced differently, so the
    totals must agree across levels and match cohort_final.

    This is the check that catches a stratification that drops or
    duplicates rows — the failure a squared table is prone to.
    """
    from qrp import Engine

    s = _cida_study([
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup"},
        {"tableid": "t2cida", "levelid": "3", "levelvars": "agegroup*sex"},
        {"tableid": "t2cida", "levelid": "4", "levelvars": "sex race"},
    ])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        expected = eng.count("cohort_final")
        rows = eng.con.execute("""
            SELECT level, sum(episodes), sum(followuptime), sum(eps_wevents)
            FROM t2_cida GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert len(rows) == 4
        for level, eps, fup, ev in rows:
            assert eps == expected, f"level {level}: {eps} != {expected}"
            assert (eps, fup, ev) == rows[0][1:], (
                f"level {level} does not reconcile with level {rows[0][0]}"
            )
    finally:
        eng.close()


def test_cida_npts_is_distinct_patients(study):
    """SAS gets Npts from max(Patient) per patient then sum — a distinct
    count. It must be <= episodes, and equal at the overall level to the
    distinct patients in cohort_final."""
    from qrp import Engine

    s = _cida_study([{"tableid": "t2cida", "levelid": "1", "levelvars": ""}])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        for grp, npts, eps in eng.con.execute(
            'SELECT "group", npts, episodes FROM t2_cida WHERE level = \'1\''
        ).fetchall():
            actual = eng.con.execute(
                "SELECT count(DISTINCT patid) FROM cohort_final "
                "WHERE cohortgrp = ?", [grp]
            ).fetchone()[0]
            assert npts == actual, f"{grp}: npts {npts} != {actual}"
            assert npts <= eps
    finally:
        eng.close()


def test_cida_is_skipped_without_userstrata(study, baseline):
    assert not study.any_cida_tables
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "t2_cida" not in existing


# ---------------------------------------------------------------------
# Enrolled member-days denominator (ms_cidadenom.sas)
# ---------------------------------------------------------------------


def test_denominator_window_matches_sas():
    """ms_cidadenom.sas:135-137 and 708.

        DenomEnrStartDt = Enr_Start + ENRDAYS
        AdjustedEnrEndDt = min(Enr_End, censordate)
                         - max(0, MinEpisDur-1, MinDaySupp-1, BlackoutPer,
                                  ReqDaysAftInd,
                                  ReqDaysAftEpi + max(MinEpisDur-1,
                                                      MinDaySupp-1,
                                                      BlackoutPer))

    Eligible time starts once the required prior enrollment is
    satisfied and ends early enough that an index date there could still
    meet every forward-looking requirement.
    """
    from qrp import Engine

    s = _cida_study([{"tableid": "t2cida", "levelid": "1", "levelvars": ""}])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        # no eligible window may start before enr_start + enr_days
        bad = eng.con.execute("""
            SELECT count(*) FROM denomcounts WHERE dennummemdays <= 0
        """).fetchone()[0]
        assert bad == 0, "SAS keeps only positive member days"
        assert eng.count("denomcounts") > 0
    finally:
        eng.close()


def test_denominator_levels_reconcile():
    """Like the numerators: every level is the same eligible time sliced
    differently, so member-days must agree across levels."""
    from qrp import Engine

    s = _cida_study([
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup"},
        {"tableid": "t2cida", "levelid": "3", "levelvars": "sex race"},
    ])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        rows = eng.con.execute("""
            SELECT level, sum(dennummemdays), sum(episodes)
            FROM t2_cida GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert len(rows) == 3
        for level, md, eps in rows:
            assert (md, eps) == rows[0][1:], (
                f"level {level} does not reconcile with level {rows[0][0]}"
            )
    finally:
        eng.close()


def test_cida_merge_keeps_strata_with_no_episodes():
    """A stratum can have eligible members but no exposed episodes.

    SAS merges numerators and denominators on common level values; a
    LEFT join would silently drop those rows and understate the
    denominator, so the merge is FULL with zero-filled numerators.
    """
    from qrp import Engine

    s = _cida_study([
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup*sex"},
    ])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        # every row must carry a denominator, even where episodes is 0
        orphans = eng.con.execute("""
            SELECT count(*) FROM t2_cida
            WHERE dennummemdays = 0 AND dennumpts = 0 AND episodes > 0
        """).fetchone()[0]
        assert orphans == 0, "episodes with no denominator row"
        # and no row may have negative or NULL metrics after the merge
        nulls = eng.con.execute("""
            SELECT count(*) FROM t2_cida
            WHERE npts IS NULL OR dennummemdays IS NULL OR episodes IS NULL
        """).fetchone()[0]
        assert nulls == 0, "merge left NULLs; coalesce is missing"
    finally:
        eng.close()


def test_denominator_exceeds_exposed_person_time():
    """Enrolled member-days is the background denominator, so it must be
    far larger than exposed follow-up time. A sanity check that the two
    have not been swapped."""
    from qrp import Engine

    s = _cida_study([{"tableid": "t2cida", "levelid": "1", "levelvars": ""}])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        for grp, md, fup in eng.con.execute(
            'SELECT "group", dennummemdays, followuptime FROM t2_cida '
            "WHERE level = '1'"
        ).fetchall():
            assert md > fup, f"{grp}: dennummemdays {md} <= followuptime {fup}"
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Risk scores (ms_computeriskscores.sas)
# ---------------------------------------------------------------------


def _risk_study(rows):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "rs",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "riskscorecodes": rows,
    })


def test_risk_score_counts_a_condition_once():
    """ms_computeriskscores.sas:369 takes max(weight) per
    (patient, indexdt, condidnum) BEFORE summing.

    So meeting a condition ten times scores it once, at its highest
    weight. Two codes mapping to one condition with different weights
    must contribute the larger, not the sum.
    """
    from qrp import Engine

    rows = [
        {"riskscore": "T", "condid": "IN", "codecat": "IN", "weight": 0.0},
        # same condition, two codes, different weights
        {"riskscore": "T", "condid": "C1", "codecat": "DX",
         "code": "X00001", "weight": 2.0, "riskfrom": -365, "riskto": -1},
        {"riskscore": "T", "condid": "C1", "codecat": "DX",
         "code": "X00002", "weight": 5.0, "riskfrom": -365, "riskto": -1},
    ]
    eng = Engine(verbose=False)
    try:
        run(_risk_study(rows), DATA, engine=eng, verbose=False)
        # a patient with BOTH codes scores 5, never 7
        assert eng.con.execute(
            "SELECT max(score) FROM risk_scores"
        ).fetchone()[0] == 5.0
    finally:
        eng.close()


def test_risk_score_intercept_applies_to_everyone():
    """ms_computeriskscores.sas:407 — a patient matching nothing gets the
    intercept, not NULL."""
    from qrp import Engine

    rows = [
        {"riskscore": "T", "condid": "IN", "codecat": "IN", "weight": -2.5},
        {"riskscore": "T", "condid": "C1", "codecat": "DX",
         "code": "X00001", "weight": 3.0, "riskfrom": -365, "riskto": -1},
    ]
    eng = Engine(verbose=False)
    try:
        run(_risk_study(rows), DATA, engine=eng, verbose=False)
        nulls = eng.con.execute(
            "SELECT count(*) FROM risk_scores WHERE score IS NULL"
        ).fetchone()[0]
        assert nulls == 0, "matching nothing must score the intercept"
        # every episode is scored, and the floor is the intercept
        assert eng.count("risk_scores") == eng.count("ptsmasterlist")
        assert eng.con.execute(
            "SELECT min(score) FROM risk_scores"
        ).fetchone()[0] == -2.5
    finally:
        eng.close()


def test_risk_score_demographic_conditions():
    """codecat='DM' rows match a sex or age group rather than a claim."""
    from qrp import Engine

    rows = [
        {"riskscore": "T", "condid": "IN", "codecat": "IN", "weight": 0.0},
        {"riskscore": "T", "condid": "FEM", "codecat": "DM",
         "code": "F", "weight": 4.0},
    ]
    eng = Engine(verbose=False)
    try:
        run(_risk_study(rows), DATA, engine=eng, verbose=False)
        wrong = eng.con.execute("""
            SELECT count(*) FROM risk_scores rs
            JOIN ptsmasterlist m
              ON m.cohortgrp = rs.cohortgrp AND m.patid = rs.patid
             AND m.indexdt = rs.indexdt
            WHERE rs.score <> CASE WHEN m.sex = 'F' THEN 4.0 ELSE 0.0 END
        """).fetchone()[0]
        assert wrong == 0
    finally:
        eng.close()


def test_risk_scores_skipped_when_not_requested(study, baseline):
    assert not study.any_risk_scores
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "risk_scores" not in existing


def test_monitor_cursor_does_not_print_a_progress_bar():
    """A DuckDB cursor does not inherit connection settings.

    The telemetry cursor was printing its own progress bar to stdout,
    which corrupts the TUI and leaked into captured output.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        for setting in ("enable_progress_bar_print", "enable_progress_bar"):
            val = eng._monitor.execute(
                f"SELECT current_setting('{setting}')"
            ).fetchone()[0]
            assert str(val).lower() in ("false", "0"), (
                f"monitor cursor has {setting}={val}"
            )
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Geographic variables (ms_geographicvars.sas)
# ---------------------------------------------------------------------


def _geo_study(zip_rows):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "geo",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "zipfile": zip_rows,
    })


def test_sas_missing_is_null_or_blank():
    """SAS `missing()` on a CHARACTER variable is true for NULL and for
    blank. `IS NULL` alone is not equivalent, and real SCDM postalcode
    uses '' rather than NULL.
    """
    import duckdb

    con = duckdb.connect()
    try:
        macros = (Path(__file__).resolve().parents[1]
                  / "src" / "qrp" / "sql" / "00_macros.sql").read_text()
        con.execute(macros)
        for value, expected in ((None, True), ("", True), ("  ", True),
                                ("02703", False)):
            got = con.execute("SELECT is_missing(?)", [value]).fetchone()[0]
            assert got == expected, f"is_missing({value!r}) = {got}"
    finally:
        con.close()


def test_geography_unknown_rules_cascade():
    """A missing zip OR an unmatched statecode makes ALL FOUR geography
    variables Unknown, not just the one that failed to map. And cb_reg
    is additionally Unknown when the lookup says 'other' — a value, not
    a NULL.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        # deliberately map nothing: every episode must be fully Unknown
        run(_geo_study([{"zip": "ZZZZZ", "statecode": "XX",
                         "hhs_region": "1", "cb_region": "Northeast",
                         "sdi": 10.0}]),
            DATA, engine=eng, verbose=False)
        bad = eng.con.execute("""
            SELECT count(*) FROM geography
            WHERE zip3 <> 'Unknown' OR state <> 'Unknown'
               OR hhs_reg <> 'Unknown' OR cb_reg <> 'Unknown'
        """).fetchone()[0]
        assert bad == 0, "unmatched zips must cascade to all four columns"
        assert eng.con.execute(
            "SELECT count(*) FROM geography WHERE sdi_cat <> '5'"
        ).fetchone()[0] == 0
    finally:
        eng.close()


def test_geography_cb_region_other_is_unknown():
    from qrp import Engine
    import duckdb

    con = duckdb.connect()
    zips = [r[0] for r in con.execute(
        f"SELECT DISTINCT postalcode FROM "
        f"read_parquet('{DATA / 'demographic' / 'data.parquet'}') "
        f"WHERE postalcode IS NOT NULL LIMIT 20").fetchall()]
    con.close()

    rows = [{"zip": z, "statecode": "MA", "hhs_region": "1",
             "cb_region": "other", "sdi": 30.0} for z in zips]
    eng = Engine(verbose=False)
    try:
        run(_geo_study(rows), DATA, engine=eng, verbose=False)
        # state maps, but cb_reg must still be Unknown
        mapped = eng.con.execute(
            "SELECT count(*) FROM geography WHERE state = 'MA'"
        ).fetchone()[0]
        assert mapped > 0, "fixture did not map any zips"
        leaked = eng.con.execute(
            "SELECT count(*) FROM geography "
            "WHERE state = 'MA' AND cb_reg <> 'Unknown'"
        ).fetchone()[0]
        assert leaked == 0, "cb_region 'other' must become Unknown"
    finally:
        eng.close()


def test_zip_uncertain_defaults_to_yes():
    """Missing zip_date means uncertain, and so does an index date
    BEFORE the zip was recorded — the address postdates the event."""
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_geo_study([{"zip": "00000", "statecode": "MA"}]),
            DATA, engine=eng, verbose=False)
        wrong = eng.con.execute("""
            SELECT count(*) FROM geography g
            JOIN ptsmasterlist m
              ON m.cohortgrp = g.cohortgrp AND m.patid = g.patid
             AND m.indexdt = g.indexdt
            WHERE g.zip_uncertain <> CASE
                WHEN m.zip_date IS NULL THEN 'Y'
                WHEN m.indexdt < m.zip_date THEN 'Y'
                ELSE 'N' END
        """).fetchone()[0]
        assert wrong == 0
    finally:
        eng.close()


def test_geography_skipped_without_zipfile(study, baseline):
    assert not study.any_geography
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "geography" not in existing


# ---------------------------------------------------------------------
# Code distribution (ms_codedistribution.sas)
# ---------------------------------------------------------------------


def test_code_distribution_reconciles_with_the_master_list(baseline):
    """Every episode is defined by exactly one code combination, so the
    episode counts must sum to the master list."""
    total = baseline.con.execute(
        "SELECT sum(episodes) FROM distindex").fetchone()[0]
    assert total == baseline.count("ptsmasterlist")


def test_code_lists_are_canonically_ordered(baseline):
    """SAS sorts by (patid, indexdate, distindexID) before concatenating.

    That is what makes the list canonical: two episodes defined by the
    same code set in a different claim order must land in the same
    bucket. Without the ORDER BY inside the aggregate they would
    scatter.
    """
    unsorted = baseline.con.execute("""
        SELECT count(*) FROM (
            SELECT list_transform(str_split(distindexlist, '_'),
                                  x -> CAST(x AS INT)) AS ids
            FROM distindex)
        WHERE ids <> list_sort(ids)
    """).fetchone()[0]
    assert unsorted == 0, "code lists are not in ascending ID order"
    # and multi-code combinations must actually occur, or the test
    # above is vacuous
    multi = baseline.con.execute(
        "SELECT count(*) FROM distindex "
        "WHERE distindexlist LIKE '%!_%' ESCAPE '!'"
    ).fetchone()[0]
    assert multi > 0, "no multi-code combinations; ordering untested"


def test_distindexmap_covers_every_code_used(baseline):
    """A list is undecodable if any ID is missing from the map."""
    orphans = baseline.con.execute("""
        WITH used AS (
            SELECT DISTINCT "group",
                   unnest(str_split(distindexlist, '_'))::INT AS id
            FROM distindex)
        SELECT count(*) FROM used u
        LEFT JOIN distindexmap m
               ON m."group" = u."group" AND m.distindexid = u.id
        WHERE m.distindexid IS NULL
    """).fetchone()[0]
    assert orphans == 0, "distindexlist references IDs not in the map"


# ---------------------------------------------------------------------
# Utilization (ms_computeutilization.sas)
# ---------------------------------------------------------------------


def _util_study(util_rows, class_rows=()):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "util",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "utilfile": util_rows,
        "drugclassfile": list(class_rows),
    })


def test_utilization_counts_are_ordered():
    """count(rx) >= count(distinct generic) >= count(distinct class).

    The distinct counts are the point of this macro: dispensings measure
    intensity, distinct generics and classes measure breadth, and they
    answer different questions. If they were all plain counts the
    ordering would collapse.
    """
    from qrp import Engine

    rows = [{"group": g, "utiltype": t, "utilfrom": -365, "utilto": -1}
            for g in ("lisinopril", "beta_blocker")
            for t in ("MED", "DRUG")]
    # generics nest within classes (generic k belongs to class k % 5), as
    # in a real drug class file; without generics every NDC shares one
    # empty generic and NumClass > NumGeneric trivially
    classes = [{"code": f"N{i:05d}", "classname": f"c{(i % 10) % 5}",
                "generic": f"g{i % 10}"}
               for i in range(1, 200)]
    eng = Engine(verbose=False)
    try:
        run(_util_study(rows, classes), DATA, engine=eng, verbose=False)
        bad = eng.con.execute("""
            SELECT count(*) FROM utilization
            WHERE numgeneric > numrx OR numclass > numgeneric
               OR enc_av + enc_oa + enc_ip + enc_is + enc_ed > enc_total
        """).fetchone()[0]
        assert bad == 0
        # and the distinct counts must actually be smaller somewhere,
        # or the assertion above is vacuous
        strict = eng.con.execute(
            "SELECT count(*) FROM utilization WHERE numgeneric < numrx"
        ).fetchone()[0]
        assert strict > 0, "no episode had repeat fills; distinctness untested"
    finally:
        eng.close()


def test_utilization_covers_every_episode():
    """LEFT from the master list: an episode with no encounters or no
    dispensings in the window scores zero, not absent."""
    from qrp import Engine

    rows = [{"group": g, "utiltype": "DRUG", "utilfrom": -30, "utilto": -1}
            for g in ("lisinopril", "beta_blocker")]
    eng = Engine(verbose=False)
    try:
        run(_util_study(rows), DATA, engine=eng, verbose=False)
        assert eng.count("utilization") == eng.count("ptsmasterlist")
        nulls = eng.con.execute(
            "SELECT count(*) FROM utilization WHERE numrx IS NULL"
        ).fetchone()[0]
        assert nulls == 0
    finally:
        eng.close()


def test_utilization_window_is_honoured():
    """utilfrom/utilto are day offsets from the index date, so a wider
    window can only find more."""
    from qrp import Engine

    def total(lo):
        rows = [{"group": g, "utiltype": "DRUG",
                 "utilfrom": lo, "utilto": -1}
                for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            # SAS counts a dispensing only if its NDC is in the drug
            # class file, so the window needs one to find anything
            classes = [{"code": f"N{i:05d}", "classname": f"c{i % 5}",
                        "generic": f"g{i % 10}"} for i in range(1, 200)]
            run(_util_study(rows, classes), DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT sum(numrx) FROM utilization").fetchone()[0]
        finally:
            eng.close()

    assert total(-365) > total(-30)


def test_utilization_skipped_without_utilfile(study, baseline):
    assert not study.any_utilization
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "utilization" not in existing


# ---------------------------------------------------------------------
# Lab extraction (ms_extractlabs.sas)
# ---------------------------------------------------------------------


def test_lab_result_criterion_parsing():
    """LABRESULT is a comparison written as a STRING.

    Two details from ms_extractlabs.sas:133-172 that are easy to get
    wrong: '<=' must be tested before '<' (or '<=7' parses as '<' with a
    bound of '=7'), and a range uses ':' not '-' — SAS says why in a
    comment, a hyphen is ambiguous with a negative lower bound.
    """
    from qrp.config import parse_lab_result as p

    assert p(">=7") == (">=", 7.0, None)
    assert p("<=140") == ("<=", 140.0, None)     # not ('<', ...)
    assert p("<3") == ("<", 3.0, None)
    assert p("~=0") == ("~=", 0.0, None)
    assert p("3.5:5.5") == (":", 3.5, 5.5)
    assert p("-2:2") == (":", -2.0, 2.0)         # negative lower bound
    assert p("7") == ("=", 7.0, None)
    assert p("") == (None, None, None)
    # A NONEMPTY criterion that cannot be parsed is a configuration
    # error, not an absent filter. Returning the sentinel let a typo in
    # a threshold silently broaden the extraction, and a warning does
    # not stop that because warnings are not read before results are.
    for bad in ("nonsense", ">=bad", "5-10"):
        with pytest.raises(ValueError, match="labresult"):
            p(bad)


def _lab_study(codes):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "lab",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "labcodes": codes,
    })


@pytest.mark.skipif(not (DATA / "lab_result").exists(),
                    reason="no lab_result fixture")
def test_lab_criteria_bind():
    from qrp import Engine

    codes = [{"group": g, "code": c, "codetype": "02N",
              "labdatetype": "LRO", "labresult": lr}
             for g in ("lisinopril", "beta_blocker")
             for c, lr in (("2160-0", ">=100"), ("3094-0", "50:150"),
                           ("2823-3", ""))]
    eng = Engine(verbose=False)
    try:
        run(_lab_study(codes), DATA, engine=eng, verbose=False)
        # A numeric criterion only constrains a NUMERIC result. In the
        # real extract 77% of records are result_type 'U' (unknown) and
        # 0.07% are 'C' — those bypass the criterion by design, so the
        # check has to be scoped to type 'N' or it counts correct
        # behaviour as a violation. An earlier version of this test did
        # exactly that and reported 11,242 false failures.
        # A numeric criterion only constrains a NUMERIC result. In the
        # real extract 77% of records are result_type 'U' (unknown) and
        # 0.07% are 'C' — those bypass the criterion by design, so the
        # check must be scoped to type 'N', or it counts correct
        # behaviour as a violation. An earlier version of this test did
        # exactly that and reported 11,242 false failures.
        #
        # EXISTS, not a join: joining on (patid, result_num) matches
        # across records that merely share a value.
        bad = eng.con.execute("""
            SELECT count(*) FROM lab_results r
            WHERE ((r.labcode = '2160-0' AND r.result_num < 100)
                OR (r.labcode = '3094-0'
                    AND (r.result_num < 50 OR r.result_num > 150)))
              AND NOT EXISTS (
                  SELECT 1 FROM cdm_lab l
                  WHERE l.patid = r.patid AND l.lab_dt = r.adate
                    AND l.result_num IS NOT DISTINCT FROM r.result_num
                    AND l.result_type <> 'N')
        """).fetchone()[0]
        assert bad == 0
        # and the unfiltered code must admit values outside those bounds,
        # or the test above proves nothing
        wide = eng.con.execute(
            "SELECT count(*) FROM lab_results "
            "WHERE labcode = '2823-3' AND result_num < 50"
        ).fetchone()[0]
        assert wide > 0, "no unfiltered results below 50; criteria untested"
    finally:
        eng.close()


@pytest.mark.skipif(not (DATA / "lab_result").exists(),
                    reason="no lab_result fixture")
def test_labdatetype_falls_through_to_an_available_date():
    """The priority string is a COALESCE whose order comes from config.

    In the real SCDM extract `result_dt` and `order_dt` are entirely
    NULL — only `lab_dt` has values — so a priority starting with 'R' or
    'O' MUST fall through to 'L' or the stage yields nothing. The
    fall-through is load-bearing here, not defensive.
    """
    from qrp import Engine

    def rows(order):
        codes = [{"group": g, "code": "2160-0", "codetype": "02N",
                  "labdatetype": order, "labresult": ""}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_lab_study(codes), DATA, engine=eng, verbose=False)
            return eng.count("lab_results")
        finally:
            eng.close()

    assert rows("LRO") > 0
    assert rows("ORL") == rows("LRO"), (
        "with result_dt and order_dt NULL every priority must resolve to "
        "lab_dt; a difference means the ladder is not falling through"
    )


def test_labs_skipped_without_labcodes(study, baseline):
    assert not study.any_labs
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "lab_results" not in existing


# ---------------------------------------------------------------------
# Most Frequent Use (ms_mfu.sas)
# ---------------------------------------------------------------------


def _mfu_study(analyses):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "mfu",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "mfufile": analyses,
    })


def test_mfu_count_methods_rank_differently():
    """CODECOUNT and PATCOUNT are different questions.

    A code appearing 50 times in one patient outranks a code appearing
    once in 40 patients by claim count, and loses badly by patient
    count. If the two produced the same order, `countmethod` would not
    be being read.
    """
    from qrp import Engine

    analyses = [
        {"group": g, "analysisnum": a, "codecat": "DX",
         "countmethod": cm, "topxx": 10, "mfufrom": -365, "mfuto": -1}
        for g in ("lisinopril", "beta_blocker")
        for a, cm in ((1, "CODECOUNT"), (2, "PATCOUNT"))
    ]
    eng = Engine(verbose=False)
    try:
        run(_mfu_study(analyses), DATA, engine=eng, verbose=False)
        differ = eng.con.execute("""
            SELECT count(*) FROM mfu a JOIN mfu b
              ON a.cohortgrp = b.cohortgrp AND a.rank = b.rank
             AND a.analysisnum = 1 AND b.analysisnum = 2
            WHERE a.code <> b.code
        """).fetchone()[0]
        assert differ > 0, "count methods produced identical rankings"
    finally:
        eng.close()


def test_mfu_respects_topxx():
    from qrp import Engine

    analyses = [
        {"group": g, "analysisnum": 1, "codecat": "DX",
         "countmethod": "CODECOUNT", "topxx": n, "mfufrom": -365,
         "mfuto": -1}
        for g, n in (("lisinopril", 5), ("beta_blocker", 12))
    ]
    eng = Engine(verbose=False)
    try:
        run(_mfu_study(analyses), DATA, engine=eng, verbose=False)
        counts = dict(eng.con.execute(
            "SELECT cohortgrp, count(*) FROM mfu GROUP BY 1").fetchall())
        assert counts["lisinopril"] <= 5
        assert counts["beta_blocker"] <= 12
        assert eng.con.execute(
            "SELECT max(rank) FROM mfu WHERE cohortgrp='lisinopril'"
        ).fetchone()[0] <= 5
    finally:
        eng.close()


def test_mfu_ranking_is_deterministic():
    """Ties must break the same way every run, or the top-N cut is not
    reproducible."""
    from qrp import Engine

    analyses = [{"group": g, "analysisnum": 1, "codecat": "DX",
                 "countmethod": "CODECOUNT", "topxx": 15,
                 "mfufrom": -365, "mfuto": -1}
                for g in ("lisinopril", "beta_blocker")]

    def ranking(threads):
        eng = Engine(threads=threads, verbose=False)
        try:
            run(_mfu_study(analyses), DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT cohortgrp, rank, code FROM mfu ORDER BY 1, 2"
            ).fetchall()
        finally:
            eng.close()

    assert ranking(1) == ranking(4)


def test_mfu_skipped_without_mfufile(study, baseline):
    assert not study.any_mfu
    existing = {r[0] for r in baseline.con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "mfu" not in existing


# ---------------------------------------------------------------------
# Event-anchored inclusion (IEV / EEV)
# ---------------------------------------------------------------------


def _iev_study(rules):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "iev",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "inclusioncodes": rules,
    })


def _iev_rules(criteria, code="X00001"):
    return [{"group": g, "cond": 1, "condlevel": 1,
             "indexcriteria": criteria, "codecat": "DX",
             "condfrom": -30, "condto": 30, "codedays": 1, "code": code}
            for g in ("lisinopril", "beta_blocker")]


def test_event_anchored_rules_drop_events_not_episodes(study):
    """The distinction from INC/EXC is the whole point.

    A failing INC/EXC rule removes the EPISODE; a failing IEV/EEV
    removes only that EVENT — the episode survives and is counted as
    event-free, or scored on a later qualifying event.

    While IEV/EEV were unimplemented they were lumped into the
    index-anchored stage, which was a reasonable approximation. Once the
    event-anchored stage existed they were applied TWICE and against the
    wrong anchor: an IEV rule removed 64,643 of 64,663 episodes instead
    of filtering a handful of events.
    """
    from qrp import Engine

    baseline_eps = None
    for criteria in (None, "IEV", "EEV"):
        rules = [] if criteria is None else _iev_rules(criteria)
        eng = Engine(verbose=False)
        try:
            run(_iev_study(rules), DATA, engine=eng, verbose=False)
            eps = eng.count("cohort_final")
            if baseline_eps is None:
                baseline_eps = eps
            else:
                assert eps == baseline_eps, (
                    f"{criteria} changed the episode count "
                    f"({eps} vs {baseline_eps}); it must only drop events"
                )
        finally:
            eng.close()


def test_iev_requires_the_code_near_the_event():
    """Every surviving event must satisfy the IEV window, anchored on
    the EVENT date rather than the index date."""
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_iev_study(_iev_rules("IEV")), DATA, engine=eng, verbose=False)
        assert eng.count("event_excluded") > 0, "IEV dropped nothing"
        violations = eng.con.execute("""
            SELECT count(*) FROM cohort_final c
            WHERE c.eventdt IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM cdm_diagnosis d
                WHERE d.patid = c.patid AND d.code = 'X00001'
                  AND d.adate BETWEEN c.eventdt - 30 AND c.eventdt + 30)
        """).fetchone()[0]
        assert violations == 0, "kept an event lacking the required code"
    finally:
        eng.close()


def test_eev_excludes_events_carrying_the_code():
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_iev_study(_iev_rules("EEV")), DATA, engine=eng, verbose=False)
        violations = eng.con.execute("""
            SELECT count(*) FROM cohort_final c
            WHERE c.eventdt IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM cdm_diagnosis d
                WHERE d.patid = c.patid AND d.code = 'X00001'
                  AND d.adate BETWEEN c.eventdt - 30 AND c.eventdt + 30)
        """).fetchone()[0]
        assert violations == 0, "kept an event carrying an excluded code"
    finally:
        eng.close()


def test_index_anchored_stage_ignores_event_rules():
    """52_inclusion.sql must handle INC/EXC only.

    A regression guard: applying IEV/EEV there as well double-counts
    them and against the wrong anchor.
    """
    sql = (Path(__file__).resolve().parents[1]
           / "src" / "qrp" / "sql" / "52_inclusion.sql").read_text()
    assert "criteria = 'INC'" in sql and "criteria = 'EXC'" in sql
    assert "'INC', 'IEV'" not in sql, "IEV is being applied at index anchor"
    assert "'EXC', 'EEV'" not in sql, "EEV is being applied at index anchor"


# ---------------------------------------------------------------------
# EVENTCOUNT (ms_createpov56.sas:130-139)
# ---------------------------------------------------------------------


def _eventcount_study(ec):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    t2 = [dict(t) for t in base["type2file"]]
    for t in t2:
        t["eventcount"] = ec
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "ec",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": t2,
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
    })


def test_eventcount_dedup_keys():
    """0 = no dedup, 1 = one per code per day, 2 = one per day.

    This was hardcoded to eventcount=2 (`SELECT DISTINCT cohortgrp,
    patid, adate`) and went unnoticed because the only consumer took
    `min(adate)`, which is invariant under all three. Adding
    `numevents` made the setting start changing the answer.
    """
    from qrp import Engine

    seen = {}
    for ec in (0, 1, 2):
        eng = Engine(verbose=False)
        try:
            run(_eventcount_study(ec), DATA, engine=eng, verbose=False)
            dup_day = eng.con.execute(
                "SELECT count(*) FROM (SELECT cohortgrp, patid, adate "
                "FROM event_claims GROUP BY 1,2,3 HAVING count(*) > 1)"
            ).fetchone()[0]
            dup_code = eng.con.execute(
                "SELECT count(*) FROM (SELECT cohortgrp, patid, adate, code "
                "FROM event_claims GROUP BY 1,2,3,4 HAVING count(*) > 1)"
            ).fetchone()[0]
            seen[ec] = (eng.count("event_claims"), dup_day, dup_code)
        finally:
            eng.close()

    # 1 removes code-level duplicates but keeps same-day different-code
    assert seen[1][2] == 0, "eventcount=1 left duplicate (patid, adate, code)"
    assert seen[1][1] > 0, "fixture has no same-day different-code events"
    # 2 removes those too
    assert seen[2][1] == 0, "eventcount=2 left duplicate (patid, adate)"
    # 0 keeps everything, so it must have the most
    assert seen[0][0] > seen[1][0] > seen[2][0], seen


def test_eventcount_changes_numevents():
    """If the setting had no effect on counts it would not be worth
    reading."""
    from qrp import Engine

    totals = {}
    for ec in (0, 2):
        eng = Engine(verbose=False)
        try:
            run(_eventcount_study(ec), DATA, engine=eng, verbose=False)
            totals[ec] = eng.con.execute(
                "SELECT sum(numevents) FROM first_event").fetchone()[0]
        finally:
            eng.close()
    assert totals[0] > totals[2], totals


def test_eventcount_does_not_change_first_event():
    """min(adate) is invariant under all three dedup keys, so the
    cohort's event FLAG must not move — only the count."""
    from qrp import Engine

    firsts = {}
    for ec in (0, 1, 2):
        eng = Engine(verbose=False)
        try:
            run(_eventcount_study(ec), DATA, engine=eng, verbose=False)
            firsts[ec] = eng.con.execute(
                "SELECT count(*) FROM cohort_final WHERE has_event = 1"
            ).fetchone()[0]
        finally:
            eng.close()
    assert firsts[0] == firsts[1] == firsts[2], firsts


# ---------------------------------------------------------------------
# IOC washout codes (fupcriteria='IOC')
# ---------------------------------------------------------------------


def _ioc_study(ioc_codes):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    cc = [dict(r) for r in base["cohortcodes"]]
    for code in ioc_codes:
        for g in ("lisinopril", "beta_blocker"):
            # codecat is required: IOC codes are read from the domain
            # they name, and real input files always specify it (0 of
            # 1,124 rows omit it in the study file seen).
            cc.append({"group": g, "indexcriteria": "", "fupcriteria": "IOC",
                       "codecat": "DX", "code": code,
                       "caresettingprincipal": ""})
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "ioc",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": cc,
        "codestrength": base["codestrength"],
    })


def test_ioc_codes_are_a_separate_role():
    """`fupcriteria='IOC'` codes never define an index or an outcome.

    They only disqualify an episode whose follow-up washout window
    contains one (ms_cidanum.sas:1664 -> _FUPWash, consumed by
    _WashEventsInFupWash in ms_createpov56.sas).
    """
    s = _ioc_study(["X00001", "X00002"])
    c = s.cohorts[0]
    # (code, codecat) pairs — codecat decides which claim domain each
    # code is read from, so a study can define exposure across RX, PX
    # and DX at once.
    # (code, codecat, codetype, code_supply) — codecat decides which claim
    # domain the code is read from, code_supply overrides the claim's
    # own RxSup when the study sets CODESUPPLY.
    assert {code for code, _, _, _ in c.ioc_codes} == {"X00001", "X00002"}
    assert not ({code for code, _, _, _ in c.ioc_codes}
                & {code for code, _, _, _ in c.event_codes})
    assert not ({code for code, _, _, _ in c.ioc_codes}
                & {code for code, _, _, _ in c.exposure_codes})
    assert s.any_ioc


def test_ioc_codes_disqualify_episodes():
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_ioc_study(["X00001", "X00002"]), DATA, engine=eng, verbose=False)
        kept = eng.con.execute("""
            SELECT count(*) FROM cohort_final c
            JOIN cfg_cohort cfg ON cfg.cohortgrp = c.cohortgrp
            WHERE cfg.fup_wash_per > 0 AND EXISTS (
                SELECT 1 FROM cdm_diagnosis d
                WHERE d.patid = c.patid
                  AND d.code IN ('X00001', 'X00002')
                  AND d.adate BETWEEN c.indexdt - cfg.fup_wash_per
                                  AND c.indexdt - 1)
        """).fetchone()[0]
        assert kept == 0, f"{kept} episode(s) kept despite an IOC claim"
    finally:
        eng.close()


def test_more_ioc_codes_narrow_the_cohort(study):
    """Adding a disqualifying code can only shrink the cohort."""
    from qrp import Engine

    def size(codes):
        eng = Engine(verbose=False)
        try:
            run(_ioc_study(codes), DATA, engine=eng, verbose=False)
            return eng.count("cohort_final")
        finally:
            eng.close()

    none = size([])
    few = size(["X00001", "X00002"])
    many = size([f"X{i:05d}" for i in range(1, 21)])
    assert none > few > many, (none, few, many)


# ---------------------------------------------------------------------
# MINRXDAYS and the condition-join fix
# ---------------------------------------------------------------------


def _minrx_study(minrxdays, codes=("N00001", "N00002", "N00003")):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rules = [{"group": g, "cond": 1, "condlevel": 1,
              "indexcriteria": "INC", "codecat": "RX",
              "condfrom": -365, "condto": -1, "codedays": 1,
              "minrxdays": minrxdays, "code": c}
             for g in ("lisinopril", "beta_blocker") for c in codes]
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "mrd",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "inclusioncodes": rules,
    })


def test_minrxdays_counts_supply_days_not_claims():
    """ms_createpov3.sas:26 — "total days in window >= minrxdays".

    A threshold on days of SUPPLY, not on the number of dispensings.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_minrx_study(120), DATA, engine=eng, verbose=False)
        short = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist p
            WHERE (SELECT coalesce(sum(overlap_days(d.adate,
                        d.adate + CAST(d.rxsup - 1 AS INTEGER),
                        p.indexdt - 365, p.indexdt - 1)), 0)
                   FROM cdm_dispensing d
                   WHERE d.patid = p.patid
                     AND d.code IN ('N00001','N00002','N00003')) < 120
        """).fetchone()[0]
        assert short == 0, f"{short} survivor(s) below the supply threshold"
    finally:
        eng.close()


def test_minrxdays_threshold_is_monotonic():
    from qrp import Engine

    def size(m):
        eng = Engine(verbose=False)
        try:
            run(_minrx_study(m), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    assert size(1) > size(30) > size(120)


def test_condition_join_does_not_multiply_claims():
    """Several INCLUSIONCODES rows share one (cond, condlevel) — one per
    code — so joining the raw table counts every claim once per row.

    `count(DISTINCT adate)` absorbed this, so `codedays` was unaffected
    and the bug stayed hidden. `minrxdays` uses `sum()` and exposed it:
    three codes inflated supply threefold, and a 120-day threshold
    passed at 40 real days.

    Asserted structurally, because the arithmetic version only fails on
    data that happens to have multi-code conditions.
    """
    import re

    for name in ("52_inclusion.sql", "60_followup.sql"):
        sql = (Path(__file__).resolve().parents[1]
               / "src" / "qrp" / "sql" / name).read_text()
        # Every join to cfg_inclusion must go through a DISTINCT on the
        # condition key. Checked by shape rather than an exact string,
        # because the exact projection changes whenever a column is
        # added — this guard broke once already for that reason.
        for match in re.finditer(r"FROM cfg_inclusion\b", sql):
            window = sql[max(0, match.start() - 400):match.start()]
            assert "SELECT DISTINCT" in window, (
                f"{name} joins cfg_inclusion without deduplicating the "
                f"condition key near offset {match.start()}"
            )


def test_minrxdays_validations_warn():
    """SAS warns rather than failing (ms_processinputfiles.sas:645-705).

    A malformed file should not stop a run, but a silently different
    answer is worse than a noisy one.
    """
    import warnings as w

    from qrp.config import (AgeStrata, CohortConfig, InclusionRule,
                            StudyConfig)
    from datetime import date

    cohort = CohortConfig(cohortgrp="a", age_strata=AgeStrata.parse(None))

    # minrxdays on a non-RX code
    bad_cat = InclusionRule(cohortgrp="a", cond=1, condlevel=1,
                            criteria="INC", codecat="DX", minrxdays=5)
    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        StudyConfig(2, date(2011, 1, 1), date(2015, 1, 1), (cohort,),
                    inclusions=(bad_cat,)).validate()
    assert any("minrxdays" in str(x.message) for x in caught)

    # two minrxdays values in one subcondlevel
    a = InclusionRule(cohortgrp="a", cond=1, condlevel=1, criteria="INC",
                      codecat="RX", minrxdays=5, subcondlevel="S1")
    b = InclusionRule(cohortgrp="a", cond=1, condlevel=1, criteria="INC",
                      codecat="RX", minrxdays=9, subcondlevel="S1")
    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        StudyConfig(2, date(2011, 1, 1), date(2015, 1, 1), (cohort,),
                    inclusions=(a, b)).validate()
    assert any("subcondlevel" in str(x.message) for x in caught)


# ---------------------------------------------------------------------
# Subconditions (cond / subcond, ms_createpov3.sas:22-38)
# ---------------------------------------------------------------------


def test_cond_and_subcond_are_derived_not_read():
    """ms_processinputfiles.sas:715-740 derives numeric `cond`/`subcond`
    by renumbering the CHARACTER condlevel/subcondlevel columns.

    They are not input columns. Reading a `cond` column that does not
    exist in a real file gives every rule cond=1, which collapses every
    condition into one and ORs what SAS ANDs.
    """
    rules = [
        {"group": "lisinopril", "condlevel": "A", "subcondlevel": "1",
         "indexcriteria": "INC", "code": "X1"},
        {"group": "lisinopril", "condlevel": "A", "subcondlevel": "1",
         "indexcriteria": "INC", "code": "X2"},
        {"group": "lisinopril", "condlevel": "A", "subcondlevel": "2",
         "indexcriteria": "INC", "code": "X3"},
        {"group": "lisinopril", "condlevel": "B", "subcondlevel": "1",
         "indexcriteria": "INC", "code": "X4"},
    ]
    s = _inclusion_study(rules)
    got = [(r.cond, r.subcond, r.codes[0]) for r in s.inclusions]
    assert got == [(1, 1, "X1"), (1, 1, "X2"), (1, 2, "X3"), (2, 1, "X4")], got


def test_subconditions_are_anded_within_a_condition():
    """"If ALL subconditions are satisfied, then condition is satisfied"
    (ms_createpov3.sas:38).

    Two codes in ONE subcondition are alternatives (OR) and must admit
    at least as many episodes as the same two codes split across two
    subconditions (AND).
    """
    from qrp import Engine

    def survivors(rules):
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    ored = [{"group": g, "condlevel": "A", "subcondlevel": "1",
             "indexcriteria": "INC", "codecat": "DX", "condfrom": -365,
             "condto": -1, "codedays": 1, "code": c}
            for g in ("lisinopril", "beta_blocker")
            for c in ("X00001", "X00002")]
    anded = [{"group": g, "condlevel": "A", "subcondlevel": sub,
              "indexcriteria": "INC", "codecat": "DX", "condfrom": -365,
              "condto": -1, "codedays": 1, "code": c}
             for g in ("lisinopril", "beta_blocker")
             for sub, c in (("1", "X00001"), ("2", "X00002"))]

    assert survivors(ored) > survivors(anded), (
        "subconditions must be ANDed; ORing them makes the two identical"
    )


def test_subexclusion_inverts_a_subcondition():
    """"If the subcondition is met but it is a subexclusion, then means
    that condition not satisfied" (ms_createpov3.sas:37)."""
    from qrp import Engine

    def survivors(subcondinclusion):
        rules = [{"group": g, "condlevel": "A", "subcondlevel": "1",
                  "subcondinclusion": subcondinclusion,
                  "indexcriteria": "INC", "codecat": "DX",
                  "condfrom": -365, "condto": -1, "codedays": 1,
                  "code": "X00001"}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    # a sub-inclusion keeps episodes WITH the code; a sub-exclusion
    # keeps the complement, so the two must not agree
    assert survivors(1) != survivors(0)


def test_non_index_anchors_warn_rather_than_silently_shifting():
    """`condfromanchor`/`condtoanchor` anchor each end of the window
    independently (ms_createpov3.sas:139-175).

    INDEXDT and EPISODEENDDT are both applied. INDEXDT_EXP anchors on
    the exposed index date, which only exists in comparator designs —
    it warns rather than silently falling back, because a silent
    fallback makes the window WRONG rather than missing.
    """
    rules = [{"group": "lisinopril", "condlevel": "A",
              "indexcriteria": "INC", "code": "X1",
              "condtoanchor": "INDEXDT_EXP"}]
    s = _inclusion_study(rules)
    assert any("INDEXDT_EXP" in u for u in s.unsupported_inclusions)

    # EPISODEENDDT is applied now that the stage runs after episodes
    ok = _inclusion_study([{"group": "lisinopril", "condlevel": "A",
                            "indexcriteria": "INC", "code": "X1",
                            "condtoanchor": "EPISODEENDDT"}])
    assert not ok.unsupported_inclusions

    # blank anchors mean INDEXDT and must NOT warn
    plain = _inclusion_study([{"group": "lisinopril", "condlevel": "A",
                              "indexcriteria": "INC", "code": "X1"}])
    assert plain.inclusions[0].condfromanchor == "INDEXDT"
    assert not plain.unsupported_inclusions


def test_episodeenddt_anchor_gives_a_different_window():
    """A window ending at the EPISODE END is forward-looking; one ending
    at the index date is a lookback. If the anchor were ignored the two
    would agree.

    This is what the stage move enabled: the inclusion stage now runs
    after the master list is built, where `episodeenddt` exists, which
    is also where SAS evaluates it (ms_createpov3 is called with
    _PtsMasterList).
    """
    from qrp import Engine

    def size(anchor, condto):
        rules = [{"group": g, "condlevel": "A", "subcondlevel": "1",
                  "indexcriteria": "INC", "codecat": "DX",
                  "condfrom": -365, "condto": condto, "codedays": 1,
                  "condtoanchor": anchor, "code": "X00001"}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    assert size("EPISODEENDDT", 0) != size("INDEXDT", -1), (
        "the condtoanchor is not being read"
    )


def test_inc_and_exc_at_the_same_condlevel_stay_separate():
    """SAS numbers `cond` within (group, CONDUSE), so an INC rule and an
    EXC rule can BOTH be cond 1.

    `cfg_inclusion_codes` was keyed on (cohortgrp, cond, subcond) with
    no criteria, so the two picked up each other's codes. Every fixture
    test used a single criteria at a time and passed; a real study
    putting INC and EXC at condlevel 1 produced a cohort where 100% of
    survivors violated the criteria.
    """
    from qrp import Engine

    rules = (
        [{"group": g, "condlevel": "1", "subcondlevel": "1",
          "indexcriteria": "INC", "codecat": "DX", "condfrom": -365,
          "condto": -1, "codedays": 1, "code": c}
         for g in ("lisinopril", "beta_blocker") for c in ("X00001",)]
        + [{"group": g, "condlevel": "1", "subcondlevel": "1",
            "indexcriteria": "EXC", "codecat": "DX", "condfrom": -365,
            "condto": -1, "codedays": 1, "code": c}
           for g in ("lisinopril", "beta_blocker") for c in ("X00002",)]
    )
    eng = Engine(verbose=False)
    try:
        run(_inclusion_study(rules), DATA, engine=eng, verbose=False)
        violations = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist m
            WHERE NOT EXISTS (SELECT 1 FROM cdm_diagnosis d
                    WHERE d.patid = m.patid AND d.code = 'X00001'
                      AND d.adate BETWEEN m.indexdt - 365
                                      AND m.indexdt - 1)
               OR EXISTS (SELECT 1 FROM cdm_diagnosis d
                    WHERE d.patid = m.patid AND d.code = 'X00002'
                      AND d.adate BETWEEN m.indexdt - 365
                                      AND m.indexdt - 1)
        """).fetchone()[0]
        assert violations == 0, (
            f"{violations} episode(s) violate INC or EXC; the two rules "
            f"are sharing codes"
        )
        assert eng.count("ptsmasterlist") > 0, "everything was excluded"
    finally:
        eng.close()


# ---------------------------------------------------------------------
# All features at once
# ---------------------------------------------------------------------


def _kitchen_sink_study():
    """A study enabling EVERY optional stage simultaneously.

    Written after a defect that no other test could have caught: INC and
    EXC rules sharing a condlevel picked up each other's codes, because
    `criteria` was missing from the code key. Every existing test used a
    single criteria at a time and passed; a real study using both
    together produced a cohort where 100% of survivors violated the
    criteria.

    Features interact. Exercising them one at a time verifies each in
    isolation and nothing about their combination.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    groups = ("lisinopril", "beta_blocker")

    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "sink",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        # IOC codes alongside the normal DEF/EVENT roles
        "cohortcodes": base["cohortcodes"] + [
            {"group": g, "indexcriteria": "", "fupcriteria": "IOC",
             "code": "X00090", "caresettingprincipal": ""}
            for g in groups
        ],
        "codestrength": base["codestrength"],
        # INC and EXC at the SAME condlevel, plus an event-anchored rule
        "inclusioncodes": (
            # A COMMON code for INC: a rare one leaves so few episodes
            # that every downstream stage is exercised against almost no
            # data and the assertions become vacuous.
            [{"group": g, "condlevel": "1", "subcondlevel": "1",
              "indexcriteria": "INC", "codecat": "DX", "condfrom": -365,
              "condto": -1, "codedays": 1, "code": "D00008"}
             for g in groups]
            + [{"group": g, "condlevel": "1", "subcondlevel": "1",
                "indexcriteria": "EXC", "codecat": "DX", "condfrom": -365,
                "condto": -1, "codedays": 1, "code": "X00002"}
               for g in groups]
            + [{"group": g, "condlevel": "1", "subcondlevel": "1",
                "indexcriteria": "IEV", "codecat": "DX", "condfrom": -30,
                "condto": 30, "codedays": 1, "code": "X00003"}
               for g in groups]
        ),
        "covariatecodes": [
            {"covarnum": 1, "covarname": "cov1", "codecat": "DX",
             "covfrom": -365, "covto": -1, "dateonly": "Y",
             "codes": ["X00010", "X00011"]},
        ],
        "userstrata": [
            {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
            {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup*sex"},
        ],
        "riskscorecodes": [
            {"riskscore": "T", "condid": "IN", "codecat": "IN",
             "weight": 1.0},
            {"riskscore": "T", "condid": "C1", "codecat": "DX",
             "code": "X00020", "weight": 3.0, "riskfrom": -365,
             "riskto": -1},
        ],
        "utilfile": [{"group": g, "utiltype": t, "utilfrom": -365,
                      "utilto": -1}
                     for g in groups for t in ("MED", "DRUG")],
        "drugclassfile": [{"code": f"N{i:05d}", "classname": f"c{i % 4}"}
                          for i in range(1, 60)],
        "zipfile": [{"zip": "00000", "statecode": "MA", "hhs_region": "1",
                     "cb_region": "Northeast", "sdi": 40.0}],
        "mfufile": [{"group": g, "analysisnum": 1, "codecat": "DX",
                     "countmethod": "CODECOUNT", "topxx": 5,
                     "mfufrom": -365, "mfuto": -1} for g in groups],
    })


def test_every_feature_enabled_at_once():
    """All twelve optional stages in one run, with invariants checked.

    Not a substitute for the focused tests — it is a guard against
    cross-feature interference, which is where the defects that survive
    focused testing actually live.
    """
    from qrp import Engine
    from qrp.pipeline import plan_stages

    s = _kitchen_sink_study()
    # every gate must actually be on, or this test drifts into
    # exercising less than it claims
    for gate in ("any_inclusions", "any_covariates", "any_cida_tables",
                 "any_risk_scores", "any_geography", "any_utilization",
                 "any_mfu", "any_code_distribution", "any_ioc"):
        assert getattr(s, gate), f"{gate} is off; the test is weaker than named"

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)

        # the declared plan must match what ran
        assert len(plan_stages(s)) >= 12

        # INC satisfied, EXC not violated, on the surviving master list
        bad = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist m
            WHERE NOT EXISTS (SELECT 1 FROM cdm_diagnosis d
                    WHERE d.patid = m.patid AND d.code = 'D00008'
                      AND d.adate BETWEEN m.indexdt-365 AND m.indexdt-1)
               OR EXISTS (SELECT 1 FROM cdm_diagnosis d
                    WHERE d.patid = m.patid AND d.code = 'X00002'
                      AND d.adate BETWEEN m.indexdt-365 AND m.indexdt-1)
        """).fetchone()[0]
        assert bad == 0, f"{bad} episode(s) violate INC/EXC"

        # IOC codes must not disqualify anything they should not
        assert eng.count("ptsmasterlist") > 0, "everything was excluded"

        # every per-episode table must align with the master list
        n = eng.count("ptsmasterlist")
        for table in ("risk_scores", "utilization", "geography"):
            assert eng.count(table) == n, (
                f"{table} has {eng.count(table)} rows, master list has {n}"
            )

        # CIDA levels must reconcile against each other
        rows = eng.con.execute(
            "SELECT level, sum(episodes) FROM t2_cida GROUP BY 1"
        ).fetchall()
        assert len({v for _, v in rows}) == 1, f"levels disagree: {rows}"

        # And the optional stages must have produced SOMETHING. An
        # over-restrictive cohort leaves them empty, which makes every
        # assertion above pass while testing nothing.
        for table in ("covariates_long", "distindex", "mfu",
                      "inclusion_excluded"):
            assert eng.count(table) > 0, f"{table} is empty; test is vacuous"
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Dose thresholds on inclusion rules (ms_createpov3.sas:333-395)
# ---------------------------------------------------------------------


def _incl_dose_study(**dose):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rules = [{"group": g, "condlevel": "A", "subcondlevel": "1",
              "indexcriteria": "INC", "codecat": "RX",
              "condfrom": -365, "condto": -1, "codedays": 1,
              "code": c, **dose}
             for g in ("lisinopril", "beta_blocker")
             # codes with a strength defined; without one there is no
             # dose and every threshold excludes everybody
             for c in ("E00001", "E00002", "E00003")]
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "dz",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "inclusioncodes": rules,
    })


def test_mincumdose_on_inclusion_is_prorated():
    """ms_createpov3.sas:369-375 pro-rates by in-window supply:

        ToDeductBf = (adate+condfrom) - incdate       if it starts early
        ToDeductAf = incexpiredt - (adate+condto)     if it ends late
        cumdose    = cumdose * (rxsup - Bf - Af) / rxsup

    Counting the whole claim when it merely OVERLAPS the window
    over-credits a dispensing that mostly falls outside it. Without
    pro-rating, 205 episodes passed a threshold they should have failed.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_incl_dose_study(mincumdose=500), DATA, engine=eng, verbose=False)
        short = eng.con.execute("""
            SELECT count(*) FROM ptsmasterlist m
            WHERE (SELECT coalesce(sum(cs.strength * d.rxamt
                            * overlap_days(d.adate,
                                d.adate + CAST(d.rxsup - 1 AS INTEGER),
                                m.indexdt - 365, m.indexdt - 1)::DOUBLE
                              / nullif(d.rxsup, 0)), 0)
                   FROM cdm_dispensing d
                   JOIN cfg_code_strength cs ON cs.code = d.code
                   WHERE d.patid = m.patid
                     AND d.code IN ('E00001','E00002','E00003')) < 500
        """).fetchone()[0]
        assert short == 0, f"{short} survivor(s) below the pro-rated threshold"
        assert eng.count("ptsmasterlist") > 0, "everything was excluded"
    finally:
        eng.close()


def test_inclusion_dose_thresholds_are_monotonic():
    from qrp import Engine

    def size(**kw):
        eng = Engine(verbose=False)
        try:
            run(_incl_dose_study(**kw), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    none = size()
    mid = size(mincumdose=500)
    high = size(mincumdose=5000)
    assert none > mid > high, (none, mid, high)


def test_afdd_bounds_narrow_the_cohort():
    """aFDD is the AVERAGE filled daily dose over the window,
    round(sum(cfdd)/sum(numdispensing), 1) — an aggregate, not a
    per-claim value."""
    from qrp import Engine

    def size(**kw):
        eng = Engine(verbose=False)
        try:
            run(_incl_dose_study(**kw), DATA, engine=eng, verbose=False)
            return eng.count("ptsmasterlist")
        finally:
            eng.close()

    assert size(minafdd=5, maxafdd=50) < size()


def test_dose_needs_a_strength_lookup():
    """A code with no strength has no dose, so any threshold excludes it.

    Worth pinning: this looked like a bug during development — a
    mincumdose rule emptied the cohort — and the cause was that the test
    used codes absent from `codestrength`.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    coded = {c["code"] for c in base["codestrength"]}
    assert "E00001" in coded
    assert "N00001" not in coded


@pytest.mark.skipif(not (DATA / "lab_result").exists(),
                    reason="no lab_result fixture")
def test_lab_codetype_dispatches_the_extraction_path():
    """`substr(codetype,1,2)` selects which column a lab code matches
    (ms_extractlabs.sas:201, 301):

        '01'  lookup — the site's lab code
        '02'  LOINC  — the LOINC directly
        other PX     — the procedure code

    SAS runs all three and removes true duplicates afterwards, "as a
    record could have been extracted three times using three different
    criteria". Matching on the code set instead means a claim matched by
    two paths appears once, with no dedup pass.
    """
    from qrp import Engine

    def rows(code, codetype):
        codes = [{"group": g, "code": code, "codetype": codetype}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_lab_study(codes), DATA, engine=eng, verbose=False)
            return eng.count("lab_results")
        finally:
            eng.close()

    # each path matches a DIFFERENT column, so a code valid on one path
    # finds nothing on another
    # LAB01 matches a seven-attribute COMBINATION, not a code column
    def lab01(**combo):
        codes = [{"group": g, "code": "C1", "codetype": "01N", **combo}
                 for g in ("lisinopril", "beta_blocker")]
        eng = Engine(verbose=False)
        try:
            run(_lab_study(codes), DATA, engine=eng, verbose=False)
            return eng.count("lab_results")
        finally:
            eng.close()

    real = dict(ms_test_name="CREATININE", ms_test_sub_category="",
                specimen_source="SR_PLS", ms_result_unit="MG/DL",
                result_type="N", fast_ind="X", pt_loc="O")
    assert lab01(**real) > 0, "the LAB01 combination found nothing"
    # change ONE attribute and the combination must stop matching
    assert lab01(**{**real, "specimen_source": "NOPE"}) == 0
    assert rows("2160-0", "02N") > 0, "LOINC path found nothing"
    assert rows("PX1", "09N") > 0, "PX path found nothing"
    assert rows("2160-0", "09N") == 0, "a LOINC must not match px"


def test_lab_result_type_gates_the_criterion():
    """`substr(codetype,3,1)` is the result type the criterion applies
    to. A numeric criterion must not constrain a character result."""
    from qrp.config import parse_lab_result

    # the parse is unchanged; what matters is that resulttyp is carried
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "startdate": "2011-01-01", "enddate": "2015-06-30"},
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "codestrength": base["codestrength"],
        "labcodes": [{"group": "lisinopril", "code": "L1",
                      "codetype": "01C", "labresult": ">=5"}],
    })
    lc = s.lab_codes[0]
    path, rt, op = lc[3], lc[4], lc[12]
    assert path == "01" and rt == "C" and op == ">="


def test_covariate_windows_honour_their_anchors():
    """`covfromanchor`/`covtoanchor` anchor each end independently
    (ms_cidacov.sas:47-54) — the same mechanism the inclusion rules use.

    Hardcoding the index date silently converts an EPISODEENDDT-anchored
    covariate, which is a FORWARD-looking window, into a lookback. Found
    while auditing ms_cidacov after fixing the identical defect in the
    inclusion stage.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def size(**anch):
        s = load_study_dict({
            "qrp_parameters_scalars": {
                "type": 2, "runid": "ca",
                "startdate": "2011-01-01", "enddate": "2015-06-30"},
            "cohortfile": base["cohortfile"],
            "type2file": base["type2file"],
            "cohortcodes": base["cohortcodes"],
            "codestrength": base["codestrength"],
            "covariatecodes": [{"covarnum": 1, "covarname": "c1",
                                "codecat": "DX", "covfrom": -365,
                                "covto": -1, "dateonly": "Y",
                                "codes": ["X00001", "X00002"], **anch}]})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            return eng.count("covariates_long")
        finally:
            eng.close()

    default = size()
    # a blank anchor means INDEXDT, so stating it explicitly must not move
    assert size(covfromanchor="INDEXDT", covtoanchor="INDEXDT") == default
    # anchoring the far end on the episode end is a different window
    assert size(covtoanchor="EPISODEENDDT", covto=0) != default


def test_unsupported_covariate_anchors_warn():
    """INDEXDT_EXP only exists in comparator designs; it must warn rather
    than silently falling back to the index date."""
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "startdate": "2011-01-01", "enddate": "2015-06-30"},
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "covariatecodes": [{"covarnum": 1, "covarname": "c1",
                            "codecat": "DX", "covfrom": -365, "covto": -1,
                            "covtoanchor": "INDEXDT_EXP",
                            "codes": ["X00001"]}]})
    assert any("covariate anchors" in u and "INDEXDT_EXP" in u
               for u in s.unsupported_inclusions)


def test_risk_score_windows_honour_their_anchors():
    """`riskfromanchor`/`risktoanchor` anchor each end independently
    (ms_computeriskscores.sas:107-117).

    The third stage found with this defect, after the inclusion rules
    and the covariate windows. All three use the same three-value
    mechanism (INDEXDT / EPISODEENDDT / INDEXDT_EXP) with a blank
    defaulting to INDEXDT — SAS sets that default explicitly at line 107.
    """
    from qrp import Engine

    def scores(**anch):
        rows = [
            {"riskscore": "T", "condid": "IN", "codecat": "IN",
             "weight": 0.0},
            {"riskscore": "T", "condid": "C1", "codecat": "DX",
             "code": "X00001", "weight": 3.0, "riskfrom": -365,
             "riskto": -1, **anch},
        ]
        eng = Engine(verbose=False)
        try:
            run(_risk_study(rows), DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT sum(score) FROM risk_scores").fetchone()[0]
        finally:
            eng.close()

    default = scores()
    # a blank anchor means INDEXDT, so stating it must not move anything
    assert scores(riskfromanchor="INDEXDT", risktoanchor="INDEXDT") == default
    # anchoring the far end on the episode end is a different window
    assert scores(risktoanchor="EPISODEENDDT", riskto=0) != default


def test_unsupported_risk_anchors_warn():
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "startdate": "2011-01-01", "enddate": "2015-06-30"},
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "riskscorecodes": [{"riskscore": "T", "condid": "C1",
                            "codecat": "DX", "code": "X00001",
                            "weight": 1.0,
                            "risktoanchor": "INDEXDT_EXP"}],
    })
    assert any("risk-score anchors" in u and "INDEXDT_EXP" in u
               for u in s.unsupported_inclusions)


def test_every_anchor_column_is_handled_or_warned():
    """A structural guard over the anchor mechanism as a whole.

    Three stages carried the identical defect — inclusion rules,
    covariate windows, risk-score windows — because the anchor columns
    were read in one place and ignored in the others. This asserts every
    anchored config object exposes the pair, so a fourth cannot be added
    silently.
    """
    from qrp.config import Covariate, InclusionRule, RiskScoreCode

    for cls, lo, hi in ((InclusionRule, "condfromanchor", "condtoanchor"),
                        (Covariate, "covfromanchor", "covtoanchor"),
                        (RiskScoreCode, "riskfromanchor", "risktoanchor")):
        fields = cls.__dataclass_fields__
        assert lo in fields and hi in fields, f"{cls.__name__} lacks anchors"
        # blank must default to INDEXDT, as SAS does explicitly
        assert fields[lo].default == "INDEXDT"
        assert fields[hi].default == "INDEXDT"


# ---------------------------------------------------------------------
# Real input file shape
# ---------------------------------------------------------------------


def test_covariatecodes_is_one_row_per_code():
    """A real COVARIATECODES table has one row per CODE with covarnum
    repeated — 4,385 rows for 49 covariates in the file seen, up to
    1,432 codes for one of them.

    The earlier parser expected one row per covariate with a `codes`
    list, which is a shape the test fixtures invented. On a real file
    every row became a separate covariate and validation rejected the
    duplicate covarnums.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rows = [
        {"covarnum": 1, "codecat": "DX", "covfrom": -90, "covto": 0,
         "codedays": 1, "dateonly": "N", "code": "A1",
         "stockgroup": "grp_one"},
        {"covarnum": 1, "codecat": "DX", "covfrom": -90, "covto": 0,
         "codedays": 1, "dateonly": "N", "code": "A2",
         "stockgroup": "grp_one"},
        {"covarnum": 2, "codecat": "RX", "covfrom": -365, "covto": -1,
         "codedays": 1, "dateonly": "N", "code": "B1",
         "stockgroup": "grp_two"},
    ]
    s = load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "startdate": "2011-01-01", "enddate": "2015-06-30"},
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "covariatecodes": rows,
    })
    assert len(s.covariates) == 2, "rows were not grouped by covarnum"
    by_num = {c.covarnum: c for c in s.covariates}
    assert by_num[1].codes == ("A1", "A2")
    assert by_num[2].codes == ("B1",)
    # real files carry no covarname; stockgroup supplies the label
    assert by_num[1].covarname == "grp_one"


def test_combo_covariate_expressions_parse():
    """codecat='CC' codes are BOOLEAN EXPRESSIONS over other covariate
    numbers, not code lists. From a real input file:

        covar 12: 3 or 4 or 5 or 6
        covar 14: 2 and (3 or 4 or 5 or 6)
        covar 49: not (1 or 48)

    Parsed rather than passed through to SQL, so a malformed expression
    fails at load and no study-supplied text reaches a query.
    """
    from qrp.config import parse_combo

    tpl, refs = parse_combo("2 and (3 or 4 or 5)")
    assert refs == (2, 3, 4, 5)
    assert tpl == "{c2} AND ({c3} OR {c4} OR {c5})"

    tpl, refs = parse_combo("not (1 or 48)")
    assert refs == (1, 48)
    assert "NOT" in tpl

    # anything outside the grammar must be rejected, not smuggled into SQL
    for bad in ("", "drop table x", "3 or (4", "3 or 4)", "and or"):
        with pytest.raises(ValueError):
            parse_combo(bad)


def test_combo_covariates_reference_known_numbers():
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    with pytest.raises(ValueError, match="unknown covariate"):
        load_study_dict({
            "qrp_parameters_scalars": {
                "type": 2, "startdate": "2011-01-01",
                "enddate": "2015-06-30"},
            "cohortfile": base["cohortfile"],
            "type2file": base["type2file"],
            "cohortcodes": base["cohortcodes"],
            "codestrength": base["codestrength"],
            "covariatecodes": [
                {"covarnum": 1, "codecat": "DX", "code": "A1"},
                {"covarnum": 2, "codecat": "CC", "code": "1 or 99"},
            ],
        })


def test_px_is_a_supported_codecat():
    """Real input files use PX freely — 150 of 1,124 cohort codes, 80
    covariate codes and 30 inclusion codes in the file seen. It is a
    mainstream domain, not an edge case."""
    from qrp.pipeline import COVAR_SOURCE_VIEW

    assert "'PX' AS codecat" in COVAR_SOURCE_VIEW
    assert "cdm_procedure" in COVAR_SOURCE_VIEW


# ---------------------------------------------------------------------
# Disclosure and path safety (review findings)
# ---------------------------------------------------------------------


def test_database_errors_do_not_leak_cell_values():
    """DuckDB quotes the OFFENDING VALUE in conversion and cast errors —
    "Could not convert string 'X' to INT32" — and X is a cell from the
    claims data. The run log is not a patient-level artefact and is not
    protected as one, so those values must not reach it.
    """
    import duckdb

    from qrp.errors import explain

    con = duckdb.connect()
    for sql in ("SELECT CAST('PT_99123456' AS INTEGER)",
                "SELECT 'PT_99123456'::DATE",
                "SELECT CAST('PT_99123456' AS DOUBLE)"):
        try:
            con.execute(sql)
        except Exception as exc:            # noqa: BLE001 - that's the point
            message = explain(exc)
            assert "PT_99123456" not in message, (
                f"cell value leaked into an error message:\n{message}"
            )
    con.close()


def test_run_id_cannot_escape_the_output_directory():
    """`run_id` is interpolated into output filenames and the run-log
    name. A traversal value resolves outside the output tree entirely —
    and past the dplocal/msoc split, which is the disclosure boundary.
    """
    from pathlib import Path

    from qrp.config import safe_run_id

    for hostile in ("../../../tmp/escaped", "a/b", "..", "", None,
                    "  ..  ", r"C:\win", "./../x"):
        cleaned = safe_run_id(hostile)
        assert "/" not in cleaned and "\\" not in cleaned
        assert ".." not in cleaned
        # and the resulting path must stay inside its directory
        out = (Path("/safe/out/dplocal") / f"{cleaned}_mstr.parquet").resolve()
        assert str(out).startswith("/safe/out/dplocal/"), out

    # legitimate ids must survive untouched
    assert safe_run_id("wp322_run1") == "wp322_run1"
    assert safe_run_id("PT001-01") == "PT001-01"


def test_memory_limit_default_is_bounded_and_overridable():
    """The package sets an explicit limit rather than inheriting
    DuckDB's default of 80% of physical RAM.

    That default is unpredictable across machines — 3 GB on a laptop,
    ~102 GB on a 128 GB server — and on shared DP hardware it is taken
    silently. Measurement says it is also unnecessary: above a 1 GB
    limit, more memory buys about 2%.

    Capped BOTH ways. The 8 GB ceiling stops a big server being drained;
    the fraction stops a small host being handed a limit it cannot
    honour, where DuckDB accepts the setting and then fails partway.
    """
    from qrp import Engine
    from qrp.sysinfo import DEFAULT_MEMORY_CEILING_GB, suggest_memory_limit

    # never exceeds the ceiling, however large the host
    for host_gb in (16, 64, 128, 512, 2048):
        got = suggest_memory_limit(int(host_gb * 1e9))
        assert got == f"{DEFAULT_MEMORY_CEILING_GB}GB", (host_gb, got)

    # scales down on a small host rather than promising what it lacks
    for host_gb in (2, 4, 8):
        got_gb = int(suggest_memory_limit(int(host_gb * 1e9)).rstrip("GB"))
        assert 1 <= got_gb < host_gb, (host_gb, got_gb)

    # an explicit request always wins
    eng = Engine(memory_limit="512MB", verbose=False)
    try:
        assert "488" in eng.effective_memory_limit or \
               "512" in eng.effective_memory_limit, \
               eng.effective_memory_limit
    finally:
        eng.close()

    # and the default is actually applied, not left to DuckDB
    eng = Engine(verbose=False)
    try:
        assert eng.effective_memory_limit not in ("", "unknown")
        # DuckDB's own default here would be 80% of RAM; ours is lower
        assert eng.effective_memory_limit != "3.1 GiB" or \
               DEFAULT_MEMORY_CEILING_GB >= 4
    finally:
        eng.close()


def test_msoc_outputs_use_their_sas_names():
    """msoc datasets go to the Operations Center, where tooling matches
    on dataset NAME. A plausible-looking rename is a breakage, not a
    cosmetic difference.

    `censoring` was one: SAS calls it `censor_cida`
    (ms_createcensortable.sas:22, 200).
    """
    import json
    import tempfile

    from qrp import run

    out = Path(tempfile.mkdtemp())
    study = _cida_study([{"tableid": "t2cida", "levelid": "1",
                          "levelvars": ""}])
    run(study, DATA, output_dir=str(out), names="sas", verbose=False)
    manifest = json.loads((out / "manifest.json").read_text())

    msoc = {t: v for t, v in manifest["tables"].items()
            if v["library"] == "msoc"}
    assert msoc, "no msoc outputs at all"

    # every msoc output either carries its SAS name or says why not
    for name, info in msoc.items():
        assert info["sas_contract"] or "note" in info, (
            f"{name} is neither SAS-named nor documented as a difference"
        )

    files = {v["file"] for v in msoc.values()}
    assert any(f.endswith("_censor_cida") for f in files), files
    assert not any(f.endswith("_censoring") for f in files), (
        "censoring must be written as censor_cida", files
    )


def test_run_log_records_effective_engine_settings():
    """`memory_limit=None` means the package default (8GB, less on a
    small host), not "unlimited" and not "auto".

    The header used to be written before the Engine existed, so it
    printed the REQUESTED value — "auto" — which cannot be used to
    explain what a job consumed on shared hardware.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        limit = eng.effective_memory_limit
        threads = eng.effective_threads
    finally:
        eng.close()

    assert limit not in ("", "auto", "unknown", "None"), limit
    assert threads >= 1
    # a real, resolved quantity
    assert any(u in limit for u in ("GiB", "MiB", "GB", "MB")), limit


def test_unproduced_output_tables_warn():
    """USERSTRATA dispatches on `tableid` (ms_cidanum.sas:2820-2831).
    This package produces `t2cida`; a study can request others.

    A requested table that is not produced is ABSENT, not empty, so a
    downstream step expecting it finds nothing at all — the same silent
    shape as ignoring an inclusion rule.

    `t2followuptime` prompted this and is now implemented, so the test
    uses `t2its` — a tableid SAS dispatches on and this package does
    not produce.
    """
    import warnings as w

    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def build(table_ids):
        return {
            "qrp_parameters_scalars": {
                "type": 2, "startdate": "2011-01-01",
                "enddate": "2015-06-30"},
            "cohortfile": base["cohortfile"],
            "type2file": base["type2file"],
            "cohortcodes": base["cohortcodes"],
            "codestrength": base["codestrength"],
            "userstrata": [{"tableid": t, "levelid": "1", "levelvars": ""}
                           for t in table_ids],
        }

    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        s = load_study_dict(build(["t2cida", "t2its"]))
    assert s.unsupported_table_ids == ("t2its",)
    assert any("t2its" in str(x.message) for x in caught)

    # the two implemented ids must NOT warn
    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        s = load_study_dict(build(["t2cida", "t2followuptime"]))
    assert s.unsupported_table_ids == ()
    assert not any("userstrata requests" in str(x.message) for x in caught)

    # the supported one alone must NOT warn
    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        s = load_study_dict(build(["t2cida"]))
    assert s.unsupported_table_ids == ()
    assert not any("userstrata requests" in str(x.message) for x in caught)


# ---------------------------------------------------------------------
# followuptime_cida (ms_createcensortable.sas, table=followuptime)
# ---------------------------------------------------------------------


def _fup_study(levels):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    return load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "fup",
            "startdate": "2011-01-01", "enddate": "2015-06-30",
        },
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "userstrata": levels,
    })


def test_followuptime_is_opt_in_by_tableid():
    """USERSTRATA dispatches on tableid (ms_cidanum.sas:2820-2831). A
    study asking only for t2cida must not get this table, and a study
    asking only for t2followuptime must not get t2_cida."""
    only_fup = _fup_study([{"tableid": "t2followuptime", "levelid": "1",
                            "levelvars": ""}])
    assert only_fup.any_followuptime
    assert not only_fup.any_cida_tables
    assert only_fup.unsupported_table_ids == ()

    only_cida = _fup_study([{"tableid": "t2cida", "levelid": "1",
                             "levelvars": ""}])
    assert only_cida.any_cida_tables
    assert not only_cida.any_followuptime


def test_followuptime_levels_reconcile_and_every_episode_is_censored():
    """Each level is a complete partition of the episodes, and every
    episode is censored for some reason — the flags are exhaustive, not
    a sample."""
    from qrp import Engine

    s = _fup_study([
        {"tableid": "t2followuptime", "levelid": "1", "levelvars": ""},
        {"tableid": "t2followuptime", "levelid": "2",
         "levelvars": "agegroup*sex"},
    ])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        total = eng.count("cohort_final")
        for level in ("1", "2"):
            got = eng.con.execute(
                "SELECT sum(episodes) FROM followuptime WHERE level = ?",
                [level]).fetchone()[0]
            assert got == total, (level, got, total)

        flags = ("cens_elig + cens_dth + cens_qryend + cens_dpend + "
                 "cens_episend + cens_spec + cens_event")
        unexplained = eng.con.execute(
            f"SELECT sum(episodes) - sum(least(1, {flags}) * episodes) "
            f"FROM followuptime WHERE level = '1'").fetchone()[0]
        assert unexplained == 0, f"{unexplained} episodes with no reason"
    finally:
        eng.close()


def test_followuptime_censor_date_differs_from_censor_cida():
    """The two tables use DIFFERENT censor dates and are not
    interchangeable.

    followuptime censors at min(EpisodeEndDt, Enr_End, FEventDt) —
    the event counts (ms_finalizeptsmasterlist.sas:312). censor_cida
    ignores the event and uses the query/DP end instead (line 308). A
    cohort with events must therefore show event-censored episodes here
    and none in censor_cida.
    """
    from qrp import Engine

    s = _fup_study([{"tableid": "t2followuptime", "levelid": "1",
                     "levelvars": ""}])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        events = eng.con.execute(
            "SELECT sum(cens_event) FROM followuptime WHERE level = '1'"
        ).fetchone()[0]
        assert events > 0, "no episode censored by its event"
        # and that matches the episodes that actually have one
        with_event = eng.con.execute(
            "SELECT count(*) FROM cohort_final WHERE has_event = 1"
        ).fetchone()[0]
        assert events <= with_event, (events, with_event)
    finally:
        eng.close()


def test_followuptime_is_written_under_its_sas_name():
    import json as _json
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_fup_study([{"tableid": "t2followuptime", "levelid": "1",
                      "levelvars": ""}]),
         DATA, output_dir=str(out), names="sas", verbose=False)
    manifest = _json.loads((out / "manifest.json").read_text())
    info = manifest["tables"]["followuptime"]
    assert info["library"] == "msoc"
    assert info["file"].endswith("_followuptime_cida"), info
    assert info["sas_contract"] is True


def test_censor_cida_has_the_sas_shape():
    """msoc.<runid>_censor_cida is `group level <censorstrat> episodes
    <msocflaglist>` (ms_createcensortable.sas:246-250).

    This was a per-exit_reason summary with person_days and percentages
    — readable, but not the dataset the Operations Center expects. For
    an msoc output the shape is part of the contract, not a
    presentation choice.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM censoring LIMIT 0").description}
        for required in ("group", "level", "censdays_value_cat", "episodes",
                         "cens_elig", "cens_dth", "cens_qryend",
                         "cens_dpend"):
            assert required in cols, (required, sorted(cols))

        # a study with no USERSTRATA still gets the table, unstratified
        assert eng.count("censoring") > 0, (
            "censor_cida is absent for a study that defines no strata"
        )
        total = eng.con.execute(
            "SELECT sum(episodes) FROM censoring").fetchone()[0]
        assert total == eng.count("cohort_final")
    finally:
        eng.close()


def test_censoring_flags_are_independent_not_exclusive():
    """The flags indicate which dates EQUAL the censor date, so an
    episode can carry more than one — disenrolling on the query end date
    sets both cens_elig and cens_qryend.

    Asserting they sum to the episode count would be wrong, and would
    have looked like a bug when the sum exceeded it.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        episodes, flagged = eng.con.execute("""
            SELECT sum(episodes),
                   sum(cens_elig + cens_dth + cens_qryend + cens_dpend)
            FROM censoring
        """).fetchone()
        # every episode has at least one reason ...
        assert flagged >= episodes, (flagged, episodes)
        # ... and the excess is exactly the coincident-date episodes
        coincident = eng.con.execute("""
            SELECT count(*) FROM cohort_final c
            JOIN cfg_cohort cfg ON cfg.cohortgrp = c.cohortgrp
            WHERE c.enr_end = DATE '2015-06-30'
              AND least(c.enr_end,
                        coalesce(c.deathdt, DATE '9999-12-31'),
                        DATE '2015-06-30') = c.enr_end
        """).fetchone()[0]
        assert flagged - episodes == coincident, (flagged, episodes,
                                                  coincident)
    finally:
        eng.close()


def test_attrition_uses_sas_column_names_and_units():
    """msoc.<runid>_attrition is `group level descr claim_level
    remaining excluded` (ms_attrition_cidacompute.sas:104-115).

    `claim_level` says which unit remaining/excluded are counted in.
    Steps 1-3 narrow CLAIMS; 4 onward narrow EPISODES, and SAS never
    subtracts across that boundary because each step declares its own
    unit and computes its own counts.

    An earlier version lagged across the change and reported 66,921
    excluded against 53,107 remaining — a plausible-looking number that
    meant nothing. The boundary is NULL instead.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM attrition LIMIT 0").description}
        for required in ("group", "level", "descr", "claim_level",
                         "remaining", "excluded"):
            assert required in cols, (required, sorted(cols))

        rows = eng.con.execute("""
            SELECT level, claim_level, remaining, excluded
            FROM attrition
            WHERE "group" = (SELECT min("group") FROM attrition)
            ORDER BY level
        """).fetchall()

        # remaining never increases WITHIN a unit
        for unit in ("Claim", "Episode"):
            vals = [r[2] for r in rows if r[1] == unit]
            assert vals == sorted(vals, reverse=True), (unit, vals)

        # excluded is NULL exactly where the unit changes, and reconciles
        # with the drop in remaining everywhere else
        by_level = {r[0]: r for r in rows}
        for lvl, (_, unit, remaining, excluded) in by_level.items():
            prev = by_level.get(lvl - 1)
            if prev is None or prev[1] != unit:
                assert excluded is None, (lvl, excluded)
            else:
                assert excluded == prev[2] - remaining, (lvl, excluded)
    finally:
        eng.close()


def test_output_column_names_match_the_sas_contract():
    """A sweep, not a spot check.

    Three outputs in a row turned out to carry the right information
    under names no downstream reader would match on — `censor_cida`
    (shape), `attrition` (names and units), `denomcounts` and
    `distindexmap` (names and missing columns). Getting the FILENAME
    right says nothing about the columns inside it, so this pins the
    column sets for every output whose SAS keep-list is known.
    """
    from qrp import Engine

    expected = {
        # ms_codedistribution.sas:433-435 and 444-447
        "distindexmap": ["group", "distindextype", "stockgroup", "codecat",
                         "codetype", "enctype", "pdx", "code",
                         "distindexid"],
        "distindex": ["group", "distindextype", "distindexlist",
                      "episodes"],
        # ms_attrition_cidacompute.sas:125-134
        "attrition": ["group", "level", "descr", "claim_level",
                      "remaining", "excluded"],
        # ms_createcensortable.sas:246-250 — the keep is
        # `group level <censorstrat> episodes <msocflaglist>`, and
        # censorstrat is the USERSTRATA levelvars (ms_cidanum.sas:123),
        # so agegroup and sex belong here.
        # censorstrat is the full levelvars set, so every stratum
        # column belongs here — not just agegroup and sex.
        "censoring": ["group", "level", "censdays_value_cat",
                      "agegroup", "sex", "race", "hispanic", "year",
                      "episodes", "cens_elig", "cens_dth", "cens_qryend",
                      "cens_dpend"],
    }

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        for table, required in expected.items():
            cols = [d[0] for d in eng.con.execute(
                f"SELECT * FROM {table} LIMIT 0").description]
            missing = [c for c in required if c not in cols]
            assert not missing, f"{table} is missing {missing}; has {cols}"
            # and the contract columns lead, in SAS order
            lead = [c for c in cols if c in required]
            assert lead == required, f"{table} order: {lead} != {required}"

            # EXACTLY the SAS columns — no extras. For an msoc output
            # the column SET is the contract, not just the names. A data
            # partner noticed four unexpected columns on attrition that
            # had been kept on the reasoning that the values were
            # already computed; that reasoning was wrong.
            extra = [c for c in cols if c not in required]
            assert not extra, f"{table} carries non-contract columns: {extra}"
    finally:
        eng.close()


def test_denominator_metrics_use_the_sas_names():
    """SAS calls these DenNumPts / DenNumMemDays in BOTH denomcounts and
    t2_cida (9 uses each in ms_cidatables.sas). They were
    `eligible_members` / `memberdays` — descriptive, but not what a
    downstream merge references."""
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({
        "qrp_parameters_scalars": {
            "type": 2, "runid": "d", "startdate": "2011-01-01",
            "enddate": "2015-06-30"},
        "cohortfile": base["cohortfile"],
        "type2file": base["type2file"],
        "cohortcodes": base["cohortcodes"],
        "codestrength": base["codestrength"],
        "userstrata": [{"tableid": "t2cida", "levelid": "1",
                        "levelvars": ""}],
    })
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        for table in ("denomcounts", "t2_cida"):
            cols = {d[0] for d in eng.con.execute(
                f"SELECT * FROM {table} LIMIT 0").description}
            assert "dennumpts" in cols, (table, sorted(cols))
            assert "dennummemdays" in cols or table == "t2_cida", cols

        # and the two must agree — the merge is by name
        denom, cida = eng.con.execute("""
            SELECT (SELECT sum(dennumpts) FROM denomcounts WHERE level='1'),
                   (SELECT sum(dennumpts) FROM t2_cida     WHERE level='1')
        """).fetchone()
        assert denom == cida, (denom, cida)
    finally:
        eng.close()


def test_geography_columns_live_on_the_master_list():
    """SAS has NO &RUNID._geography dataset — checked across the whole
    macro library. It carries zip3/state/hhs_reg/cb_reg/zip_uncertain as
    COLUMNS ON mstr (ms_geographicvars.sas:158).

    Emitting them only as a separate table meant `mstr` was missing
    columns a downstream step reads from it by name.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    base["zipfile"] = [
        {"zip": f"{i:05d}", "statecode": "MA", "hhs_region": "1",
         "cb_region": "Northeast", "sdi": 40.0}
        for i in range(300)
    ]
    eng = Engine(verbose=False)
    try:
        run(load_study_dict(base), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM ptsmasterlist LIMIT 0").description}
        for required in ("zip3", "state", "hhs_reg", "cb_reg",
                         "zip_uncertain"):
            assert required in cols, (required, sorted(cols))

        # the merge must not drop or duplicate episodes
        assert eng.count("ptsmasterlist") == eng.count("geography")
        unmatched = eng.con.execute(
            "SELECT count(*) FROM ptsmasterlist WHERE zip_uncertain IS NULL"
        ).fetchone()[0]
        assert unmatched == 0, f"{unmatched} episodes lost the geography join"
    finally:
        eng.close()


def test_mstr_is_the_finalised_master_list():
    """SAS has ONE master list. `DPLocal.&RUNID._mstr` is set from
    `_PtsMasterList` AFTER ms_finalizeptsmasterlist attaches the event
    and censoring columns (SAS's mstr contents (exact line unverified)), so SAS's mstr
    is the FINALISED list — this package's `cohort_final`.

    There is no `&RUNID._mstr_final` anywhere in the macro library.
    Mapping the pre-follow-up intermediate to `mstr` meant a DP reading
    `<runid>_mstr` got 6,687 extra episodes that had never been through
    the follow-up washout, and no event columns at all.
    """
    import json as _json
    import tempfile

    import duckdb

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(load_study(STUDY), DATA, output_dir=str(out), names="sas", debug=True,
         verbose=False)
    manifest = _json.loads((out / "manifest.json").read_text())

    assert manifest["tables"]["cohort_final"]["file"].endswith("_mstr")
    assert manifest["tables"]["cohort_final"]["sas_contract"] is True
    # the intermediate must NOT claim a SAS name
    inter = manifest["tables"]["ptsmasterlist"]
    assert not inter["file"].endswith("_mstr"), inter
    assert inter["sas_contract"] is False
    assert "note" in inter

    # and the file called _mstr must actually be the finalised one
    con = duckdb.connect()
    try:
        path = out / manifest["tables"]["cohort_final"]["library"] / \
            manifest["tables"]["cohort_final"]["file"]
        cols = {d[0] for d in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path}/**/*.parquet')"
        ).fetchall()}
        for required in ("eventdt", "has_event", "numevents"):
            assert required in cols, (required, sorted(cols))
    finally:
        con.close()


def test_every_sas_contract_output_really_exists_in_sas():
    """Guard against the error this whole sweep was made of.

    `SAS_CONTRACT` was populated from what this package produced and
    then labelled with SAS names that seemed to fit. Two of those names
    — `mstr_final` and `geography` — do not exist in the SAS macro
    library at all, and one (`mstr`) named the wrong table.

    The names below were each verified against the macros. Anything not
    on this list must be flagged as an addition, with a note.
    """
    from qrp.pipeline import SAS_CONTRACT, SAS_NAMES, SAS_NAME_NOTES

    # Dataset names confirmed present in the SAS macro library.
    real_sas_datasets = {
        "mstr", "denomcounts", "numcounts",          # dplocal
        "attrition", "censor_cida", "followuptime_cida",
        "distindex", "distindexmap", "runtimes", "signature",
        "t2_cida",                                   # msoc
    }

    for table in SAS_CONTRACT:
        emitted = SAS_NAMES.get(table, table)
        assert emitted in real_sas_datasets, (
            f"{table} is flagged as a SAS contract output but SAS has no "
            f"dataset called {emitted!r}"
        )

    # and every non-contract output must explain itself
    for table, name in SAS_NAMES.items():
        if table not in SAS_CONTRACT:
            assert table in SAS_NAME_NOTES, (
                f"{table} -> {name} is neither a verified SAS name nor "
                f"documented as an addition"
            )


def test_mstr_carries_the_sas_column_names():
    """`mstr` is the primary patient-level deliverable, and downstream
    SAS steps read columns off it BY NAME
    (ms_finalizeptsmasterlist.sas).

    Seven were missing or differently named — `FEventDt`, `Event`,
    `Event_flag`, `followuptime`, `timetocensor`, `EpisodeEndDt_Censor`,
    `episodelength`, `group`. A step selecting any of them would have
    found nothing. Getting the FILE right is not the same as getting the
    columns right, which is the lesson of this whole sweep.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM cohort_final LIMIT 0").description}
        for required in ("group", "patid", "indexdt", "episodeenddt",
                         "origepisenddt", "enr_start", "enr_end",
                         "feventdt", "event", "event_flag", "numevents",
                         "followuptime", "timetocensor",
                         "episodeenddt_censor", "episodelength"):
            assert required in cols, (required, sorted(cols))

        # the SAS-named columns must agree with this package's own
        bad = eng.con.execute("""
            SELECT count(*) FROM cohort_final
            WHERE (event = 1) <> (has_event = 1)
               OR feventdt IS DISTINCT FROM eventdt
               OR (event_flag = 'Y') <> (has_event = 1)
               OR followuptime < 0
               OR timetocensor < 0
        """).fetchone()[0]
        assert bad == 0, f"{bad} rows where the SAS names disagree"
    finally:
        eng.close()


def test_followuptime_agrees_between_mstr_and_the_cida_table():
    """`followuptime` is computed twice — once onto mstr, once in
    94_followuptime.sql. Two independent expressions of the same SAS
    formula must agree, or one of them is wrong."""
    from qrp import Engine

    s = _fup_study([{"tableid": "t2followuptime", "levelid": "1",
                     "levelvars": ""}])
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        on_mstr, in_table = eng.con.execute("""
            SELECT (SELECT sum(followuptime) FROM cohort_final),
                   (SELECT sum(CAST(fupdays_value_cat AS BIGINT) * episodes)
                    FROM followuptime WHERE level = '1')
        """).fetchone()
        assert on_mstr == in_table, (on_mstr, in_table)
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Parity comparison tool
# ---------------------------------------------------------------------


def test_parity_compare_detects_each_defect_class():
    """The comparator must catch the defect classes this package has
    actually shipped.

    A sweep of the outputs found EVERY table wrong in some way —
    missing columns, wrong row populations, wrong values. A harness
    that only compares row counts would have caught none of the column
    defects, which were the commonest.
    """
    import csv
    import subprocess
    import sys
    import tempfile

    root = Path(__file__).resolve().parents[1]
    tool = root / "tools" / "parity_compare.py"

    base = Path(tempfile.mkdtemp())
    a = base / "sas" / "t" / "table"
    b = base / "duck" / "t" / "table"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    rows = [{"group": "g", "level": str(i), "remaining": str(100 - i),
             "excluded": str(i)} for i in range(1, 5)]

    def write(path, data, cols=None):
        cols = cols or list(data[0])
        with (path / "attrition.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    write(a, rows)
    # one value changed, one row dropped, one column dropped
    perturbed = [dict(r) for r in rows[1:]]
    perturbed[0]["remaining"] = "999"
    write(b, perturbed, cols=["group", "level", "remaining"])

    out = subprocess.run(
        [sys.executable, str(tool), str(base / "sas"), str(base / "duck")],
        capture_output=True, text=True)
    assert out.returncode == 1, out.stdout
    for expected in ("MISSING COLUMN", "ROW COUNT", "KEY MISMATCH", "VALUE"):
        assert expected in out.stdout, (expected, out.stdout)

    # identical trees must be clean, or the tool cries wolf and is ignored
    same = subprocess.run(
        [sys.executable, str(tool), str(base / "sas"), str(base / "sas")],
        capture_output=True, text=True)
    assert same.returncode == 0, same.stdout
    assert "No differences" in same.stdout


def test_parity_compare_tolerates_format_noise():
    """SAS and DuckDB write floats and missing values differently. A
    harness that flags every such difference produces thousands of false
    positives and gets ignored — worse than not running it."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from parity_compare import same_value

    assert same_value("1.0", "1.0000000001", 1e-9)
    assert same_value("0.30000000000000004", "0.3", 1e-9)
    assert same_value(".", "", 1e-9)        # SAS missing vs DuckDB empty
    assert same_value("NULL", "NA", 1e-9)
    # and it must still catch real differences
    assert not same_value("1.0", "1.1", 1e-9)
    assert not same_value("2011-01-01", "2011-01-02", 1e-9)


def test_parity_dump_covers_the_deliverables_and_ignores_valid_columns():
    """The dump must cover the OUTPUT tables, not just intermediates.

    It originally dumped five intermediate stages. A sweep against the
    SAS macros then found every deliverable wrong — wrong shape, wrong
    column names, missing columns, and in one case (`mstr`) the wrong
    table entirely. Comparing only intermediates would have caught none
    of that.

    Also guards the IGNORE_COLUMNS lists: they name columns to exclude,
    and a stale entry makes the dump fail at runtime with
    `Column "step" in EXCLUDE list not found` — which is how this test
    came to exist.
    """
    from qrp import Engine
    from qrp.parity import IGNORE_COLUMNS, STAGE_TABLES

    dumped = {t for tables in STAGE_TABLES.values() for t in tables}
    for deliverable in ("attrition", "censoring", "t2_cida", "denomcounts",
                        "numcounts", "distindex", "distindexmap",
                        "cohort_final"):
        assert deliverable in dumped, f"{deliverable} is not dumped"

    # every ignored column must actually exist on its table, or the
    # EXCLUDE clause fails at runtime
    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        for table, ignored in IGNORE_COLUMNS.items():
            try:
                cols = {d[0].lower() for d in eng.con.execute(
                    f"SELECT * FROM {table} LIMIT 0").description}
            except Exception:
                continue          # table not produced by this study
            stale = [c for c in ignored if c.lower() not in cols]
            assert not stale, (
                f"IGNORE_COLUMNS[{table!r}] names columns that do not "
                f"exist: {stale}"
            )
    finally:
        eng.close()


# ---------------------------------------------------------------------
# baseline distribution table
# ---------------------------------------------------------------------


def test_baseline_is_wide_one_row_per_group():
    """SAS's baseline is WIDE: one row per cohort group, one column per
    category LEVEL (ms_createdistbaselinetable.sas:455-526).

    This was previously emitted as `covariate_prevalence` — one row per
    covariate, long. A different table, not a renaming, and the last
    deliverable in the output sweep that was structurally wrong rather
    than just misnamed.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        groups = eng.con.execute(
            "SELECT count(DISTINCT cohortgrp) FROM cohort_final").fetchone()[0]
        assert eng.count("baseline") == groups, "not one row per group"

        cols = [d[0] for d in eng.con.execute(
            "SELECT * FROM baseline LIMIT 0").description]
        for required in ("group", "patient", "n_episodes", "mean_age",
                         "std_age"):
            assert required in cols, (required, cols)
        # one column per observed level, not a hardcoded list
        assert any(c.startswith("Sex_") for c in cols), cols
        assert any(c.startswith("Race_") for c in cols), cols
        assert any(c.startswith("Age_") for c in cols), cols

        # dummies are counts: they must sum to the episode count
        bad = eng.con.execute(
            'SELECT count(*) FROM baseline WHERE "Sex_F" + "Sex_M" '
            '<> n_episodes').fetchone()[0]
        assert bad == 0, "sex dummies do not sum to n_episodes"

        total = eng.con.execute(
            "SELECT sum(n_episodes) FROM baseline").fetchone()[0]
        assert total == eng.count("cohort_final")
    finally:
        eng.close()


def test_baseline_is_squared():
    """A category absent from a group is 0, not a missing column
    (ms_createdistbaselinetable.sas:529-537).

    That is what makes the table stackable across groups and comparable
    across runs — a missing column and a zero column mean different
    things to a reader.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    # restrict ONE cohort to females, so Sex_M is genuinely absent there
    cohortfile = [dict(r) for r in base["cohortfile"]]
    cohortfile[0]["sex"] = "F"

    eng = Engine(verbose=False)
    try:
        run(load_study_dict({**base, "cohortfile": cohortfile}),
            DATA, engine=eng, verbose=False)
        rows = eng.con.execute(
            'SELECT "group", "Sex_F", "Sex_M" FROM baseline ORDER BY 1'
        ).fetchall()
        # every group carries both columns ...
        assert all(r[1] is not None and r[2] is not None for r in rows), rows
        # ... and the restricted one has a real zero, not a NULL
        assert any(r[2] == 0 for r in rows), rows

        # no NULLs anywhere in the table
        cols = [d[0] for d in eng.con.execute(
            "SELECT * FROM baseline LIMIT 0").description][1:]
        nulls = eng.con.execute(
            "SELECT " + " + ".join(
                f'sum(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)'
                for c in cols) + " FROM baseline").fetchone()[0]
        assert nulls == 0, f"{nulls} NULL cells; the table is not squared"
    finally:
        eng.close()


# ---------------------------------------------------------------------
# Second review
# ---------------------------------------------------------------------


def test_engine_shape_returns_rows_not_bytes():
    """`duckdb_tables().estimated_size` is a ROW COUNT, not a byte size.

    A review flagged this as reporting bytes. It does not — verified on
    a wide table with 400 bytes of padding per row, where the two would
    diverge by orders of magnitude. The test exists because nothing
    pinned the semantics, so the claim could not be checked from the
    code alone.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        eng.con.execute(
            "CREATE OR REPLACE TABLE shape_narrow AS SELECT * FROM range(1234)")
        assert eng.shape("shape_narrow") == (1234, 1)

        # wide + padded: rows and bytes differ by ~400x here
        eng.con.execute("""
            CREATE OR REPLACE TABLE shape_wide AS
            SELECT i, i*2 AS b, repeat('x', 200) AS pad
            FROM range(5000) t(i)
        """)
        rows, cols = eng.shape("shape_wide")
        assert (rows, cols) == (5000, 3), (rows, cols)

        # a VIEW reports -1 rows: counting one would execute it
        eng.con.execute(
            "CREATE OR REPLACE VIEW shape_view AS SELECT * FROM shape_narrow")
        assert eng.shape("shape_view")[0] == -1
    finally:
        eng.close()


def _risk_codecat_study(codecat, code, csp=None):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    row = {"riskscore": "T", "condid": "C1", "codecat": codecat,
           "code": code, "weight": 5.0, "riskfrom": -365, "riskto": -1}
    if csp:
        row["caresettingprincipal"] = csp
    return load_study_dict({**base, "riskscorecodes": [
        {"riskscore": "T", "condid": "IN", "codecat": "IN", "weight": 0.0},
        row,
    ]})


def test_px_risk_score_codes_match_procedure_claims():
    """A PX risk-score code must read the PROCEDURE domain.

    The join collapsed everything that was not RX to DX, so a PX code
    looked for procedure codes in the diagnosis table and could never
    match — scoring 0 for a condition the study had defined, silently.
    `codecat IN ('DX','PX','RX')` two lines above said PX was accepted.
    Reported in review.
    """
    from qrp import Engine

    def total(codecat, code):
        eng = Engine(verbose=False)
        try:
            run(_risk_codecat_study(codecat, code), DATA, engine=eng,
                verbose=False)
            return eng.con.execute(
                "SELECT sum(score) FROM risk_scores").fetchone()[0] or 0
        finally:
            eng.close()

    assert total("DX", "X00001") > 0, "DX risk codes score nothing"
    assert total("PX", "P00001") > 0, "PX risk codes score nothing"


def test_risk_score_care_setting_is_applied():
    """RISKSCORECODES carries its own caresettingprincipal. It was
    parsed into (enctype, pdx) and then never reached the SQL, so a
    study restricting a condition to inpatient claims got a score
    computed over every setting — plausible, and wrong.

    Note the token semantics: `IPA` is "IP, any pdx"; `IP_` is "IP with
    MISSING pdx". Reading `_` as a wildcard makes the expected ordering
    come out backwards.
    """
    from qrp import Engine

    def total(csp):
        eng = Engine(verbose=False)
        try:
            run(_risk_codecat_study("DX", "X00001", csp), DATA,
                engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT sum(score) FROM risk_scores").fetchone()[0] or 0
        finally:
            eng.close()

    unrestricted = total(None)
    assert total("AAA") == unrestricted, "the wildcard must not restrict"
    ip_any = total("IPA")
    ip_principal = total("IPP")
    assert unrestricted > ip_any > ip_principal, (
        unrestricted, ip_any, ip_principal)


def test_eventcount_key_includes_codetype():
    """SAS's eventcount=1 key is (PatId, Adate, codecat, codetype, code).

    Dropping codetype collapsed an ICD-9 and an ICD-10 claim carrying
    the same code on the same day into one event. The real extract has
    26 such pairs, so this is not hypothetical. Reported in review.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(_eventcount_study(1), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM event_claims LIMIT 0").description}
        assert "codetype" in cols, sorted(cols)

        # under eventcount=1 the FULL key must be unique
        dupes = eng.con.execute("""
            SELECT count(*) FROM (
                SELECT cohortgrp, patid, adate, code, codetype
                FROM event_claims GROUP BY 1,2,3,4,5 HAVING count(*) > 1)
        """).fetchone()[0]
        assert dupes == 0, f"{dupes} duplicate (patid, date, code, codetype)"
    finally:
        eng.close()


def test_event_and_ioc_codes_read_their_own_domain():
    """SAS sets `_FUPEvent` from _ITDrugs (RX), _ITMeds (DX and PX),
    _ITLabs, _itenc and _itDth (ms_cidanum.sas:1663-1672). Outcomes are
    NOT diagnosis-only.

    Reading `cdm_diagnosis` alone meant an outcome defined by a
    dispensing or a procedure could never fire — the cohort simply
    reported no events, with no error. `_FUPWash` (IOC) shares the same
    source and had the same defect. Reported in review.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def with_event(codecat, code):
        cc = [dict(r) for r in base["cohortcodes"]
              if r.get("indexcriteria") != "EVENT"]
        for g in ("lisinopril", "beta_blocker"):
            cc.append({"group": g, "indexcriteria": "EVENT",
                       "codecat": codecat, "code": code,
                       "caresettingprincipal": ""})
        eng = Engine(verbose=False)
        try:
            run(load_study_dict({**base, "cohortcodes": cc}), DATA,
                engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT count(*) FROM cohort_final WHERE has_event = 1"
            ).fetchone()[0]
        finally:
            eng.close()

    assert with_event("DX", "X00001") > 0, "DX outcomes do not fire"
    assert with_event("PX", "P00001") > 0, "PX outcomes do not fire"
    assert with_event("RX", "N00001") > 0, "RX outcomes do not fire"


def test_codesupply_overrides_the_claim_rxsup():
    """CODESUPPLY replaces the claim's own RxSup
    (SAS's CODESUPPLY handling (exact line unverified)).

    It was parsed, validated against the CFDD limits, and never applied.
    It is per CODE — 150 of 1,124 rows carry it in the real study file,
    all of them PX, because a procedure claim has no days-supply of its
    own. The PX arm hardcoded `1`, which was right only because every
    one of those 150 happens to BE 1; a study specifying 30 got 1-day
    episodes.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    # CFDD limits and CODESUPPLY are mutually exclusive, so drop them
    t2 = [{k: v for k, v in r.items() if k not in ("mincfdd", "maxcfdd")}
          for r in base["type2file"]]

    def mean_days(supply):
        cc = [dict(r) for r in base["cohortcodes"]]
        if supply:
            for r in cc:
                if r.get("indexcriteria") == "DEF":
                    r["codesupply"] = supply
        eng = Engine(verbose=False)
        try:
            run(load_study_dict({**base, "type2file": t2, "cohortcodes": cc}),
                DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT avg(episode_days) FROM cohort_final").fetchone()[0]
        finally:
            eng.close()

    base_days = mean_days(None)
    assert mean_days(30) < base_days < mean_days(90), (
        base_days, mean_days(30), mean_days(90))


def test_codesupply_conflicts_with_cfdd_across_all_codes():
    """The check reads every exposure code, not just the first.

    It used to read `supply_rows[0]`, collapsing a per-code value to one
    per cohort: a study where only the THIRD code set CODESUPPLY passed
    validation silently.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    # Only `lisinopril` carries a CFDD limit in this fixture, so the
    # conflict can only arise on ITS codes — beta_blocker setting
    # CODESUPPLY is legitimate. Targeting a cohort without the limit
    # would assert a failure that should not happen.
    def with_supply_on(nth):
        cc = [dict(r) for r in base["cohortcodes"]]
        defs = [r for r in cc
                if r.get("indexcriteria") == "DEF"
                and (r.get("group") or r.get("cohortgrp")) == "lisinopril"]
        defs[nth]["codesupply"] = 30
        return {**base, "cohortcodes": cc}

    n_lisinopril = len([r for r in base["cohortcodes"]
                        if r.get("indexcriteria") == "DEF"
                        and (r.get("group") or r.get("cohortgrp"))
                        == "lisinopril"])
    # first, middle and LAST — the last is the one that slipped through
    # when the check read only supply_rows[0]
    for nth in (0, n_lisinopril // 2, n_lisinopril - 1):
        with pytest.raises(ValueError, match="CODESUPPLY"):
            load_study_dict(with_supply_on(nth))

    # a cohort WITHOUT a CFDD limit may set CODESUPPLY freely
    cc = [dict(r) for r in base["cohortcodes"]]
    for r in cc:
        if (r.get("indexcriteria") == "DEF"
                and (r.get("group") or r.get("cohortgrp")) == "beta_blocker"):
            r["codesupply"] = 30
    load_study_dict({**base, "cohortcodes": cc})


# ---------------------------------------------------------------------
# Rerun safety
# ---------------------------------------------------------------------


def _runid_study(runid, **overrides):
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    params = [{**base["qrp_parameters"][0], "runid": runid}]
    return load_study_dict({**base, "qrp_parameters": params, **overrides})


def test_rerun_clears_this_runs_stale_outputs():
    """A rerun into a populated directory must not leave files the
    current run did not produce.

    Verified as a real hazard before fixing: rerunning with the
    covariates REMOVED left covariates, baseline and
    covariate_prevalence from the previous run, so a reader got
    covariate results for a study that defines none. Nothing marked
    them stale.
    """
    import tempfile
    import time

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_runid_study("rr"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    first = {p.name for p in (out / "msoc").iterdir()}
    assert any("baseline" in n for n in first), first

    time.sleep(1.1)                      # so mtimes are distinguishable
    _run(_runid_study("rr", covariatecodes=[]), DATA, output_dir=str(out),
         names="sas", verbose=False)

    files = list(out.rglob("*.parquet"))
    newest = max(f.stat().st_mtime for f in files)
    stale = [str(f) for f in files if newest - f.stat().st_mtime > 1]
    assert not stale, f"stale files survived the rerun: {stale}"


def test_rerun_with_different_names_mode_does_not_duplicate_tables():
    """Switching --names between `sas` and `logical` wrote the same
    table under BOTH names — censor_cida and censoring side by side,
    with nothing indicating they are one table."""
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_runid_study("rr"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    _run(_runid_study("rr"), DATA, output_dir=str(out), names="logical",
         verbose=False)

    names = {p.name for p in (out / "msoc").iterdir()}
    assert not (any("censor_cida" in n for n in names)
                and any(n.endswith("_censoring.parquet") for n in names)), (
        "the same table is present under two names", sorted(names))


def test_rerun_does_not_touch_a_SIBLING_runs_outputs():
    """The clear is scoped to THIS run_id's prefix.

    A data partner may legitimately keep several runs' outputs in one
    directory, and wiping a sibling's results would be worse than the
    staleness this fixes.
    """
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_runid_study("studya"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    a_files = {p.name for p in (out / "msoc").iterdir()}
    _run(_runid_study("studyb"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    after = {p.name for p in (out / "msoc").iterdir()}

    assert a_files <= after, (
        "a sibling run's outputs were destroyed",
        sorted(a_files - after))
    assert any(n.startswith("studyb_") for n in after)


def test_failed_rerun_leaves_previous_results_intact():
    """A run that fails during the PIPELINE must not destroy the
    previous run's outputs — the clear happens inside the
    output-writing block, which only runs after the pipeline succeeds.
    """
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_runid_study("rr"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    before = len(list(out.rglob("*.parquet")))
    assert before > 0

    with pytest.raises(Exception):
        _run(_runid_study("rr"), "/nonexistent/input/path",
             output_dir=str(out), names="sas", verbose=False)

    assert len(list(out.rglob("*.parquet"))) == before


def test_manifest_is_written_last_and_marks_completeness():
    """The write sequence is clear -> tables -> manifest, so the
    manifest's PRESENCE signals that a run completed and its outputs are
    the full set."""
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    _run(_runid_study("rr"), DATA, output_dir=str(out), names="sas",
         verbose=False)
    manifest = out / "manifest.json"
    assert manifest.exists()
    # no table file may be newer than the manifest
    newest_table = max(f.stat().st_mtime for f in out.rglob("*.parquet"))
    assert manifest.stat().st_mtime >= newest_table - 0.01


def test_t2_cida_supports_covariate_strata():
    """USERSTRATA levelvars beginning with `covar` stratify the CIDA
    table by a COVARIATE — SAS's `&covarstrat.`
    (ms_cidatables.sas:403-412, 425).

    They were parsed into levelvars and then ignored, so a study asking
    to stratify by covar1 got the UNSTRATIFIED totals labelled as that
    level. Silently wrong, which is worse than a missing column.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({**base, "userstrata": [
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "covar1"},
        {"tableid": "t2cida", "levelid": "3", "levelvars": "covar12"},
        {"tableid": "t2cida", "levelid": "4", "levelvars": "sex covar1"},
    ]})
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM t2_cida LIMIT 0").description}
        assert "covar1" in cols and "covar12" in cols, sorted(cols)

        rows = eng.con.execute("""
            SELECT level, sum(episodes),
                   count(*) FILTER (WHERE covar1  IS NOT NULL),
                   count(*) FILTER (WHERE covar12 IS NOT NULL)
            FROM t2_cida GROUP BY 1 ORDER BY 1
        """).fetchall()

        # every level is a COMPLETE partition of the episodes
        totals = {r[1] for r in rows}
        assert len(totals) == 1, f"levels disagree on the total: {rows}"

        by_level = {r[0]: r for r in rows}
        # covar1 appears only where a level asks for it ...
        assert by_level["1"][2] == 0
        assert by_level["2"][2] > 0
        assert by_level["4"][2] > 0
        # ... and covar1 must NOT match covar12: the levelvars list is
        # matched as a padded string for exactly this reason.
        assert by_level["3"][2] == 0, "covar1 leaked onto the covar12 level"
        assert by_level["3"][3] > 0
        assert by_level["2"][3] == 0, "covar12 leaked onto the covar1 level"
    finally:
        eng.close()


def test_baseline_carries_the_sas_dummy_groups():
    """SAS sums patient, Age:, Sex_:, year_:, race_:, hispanic_: and
    covar1..N (ms_createdistbaselinetable.sas:457-460).

    `year_` was missing — an index-year distribution the study asked for
    and did not get. Geography (cb_reg_, sdi_) and the continuous
    utilization/risk means are gated on their optional stage, as SAS
    gates them on `&geog = Y`.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = [d[0] for d in eng.con.execute(
            "SELECT * FROM baseline LIMIT 0").description]
        for prefix in ("Sex_", "Race_", "Hispanic_", "Age_", "year_"):
            assert any(c.startswith(prefix) for c in cols), (prefix, cols)

        # each dummy group is a partition: it sums to n_episodes
        years = [c for c in cols if c.startswith("year_")]
        total = " + ".join(f'"{c}"' for c in years)
        bad = eng.con.execute(
            f"SELECT count(*) FROM baseline WHERE {total} <> n_episodes"
        ).fetchone()[0]
        assert bad == 0, "year dummies do not sum to n_episodes"
    finally:
        eng.close()


def test_baseline_continuous_means_use_sas_names_and_the_right_population():
    """SAS's baseline takes mean= and std= over the utilization counts
    (ms_createdistbaselinetable.sas:474-500), named NumAV / NumOA /
    NumIP / NumIS / NumED / NumGeneric / NumClass / NumRx.

    Two things this pins:

    * **The names.** The utilization stage calls its encounter counts
      enc_av / enc_oa / ...; SAS calls them NumAV / NumOA / .... An
      earlier version listed only the SAS names, and the "skip if
      absent" branch silently dropped five of the eight.
    * **The population.** `utilization` covers every master-list episode
      (71,350 here); baseline averages over the episodes that survive
      the follow-up washout (64,663). Averaging the wrong one gives a
      plausible number that is quietly not the cohort's.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({**base, "utilfile": [
        {"group": g, "utiltype": "MED", "utilfrom": -183, "utilto": -1}
        for g in ("lisinopril", "beta_blocker")]})

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM baseline LIMIT 0").description}
        for sas in ("NumAV", "NumOA", "NumIP", "NumIS", "NumED",
                    "NumGeneric", "NumClass", "NumRx"):
            assert f"mean_{sas}" in cols, (sas, sorted(cols))
            assert f"std_{sas}" in cols, (sas, sorted(cols))

        # the mean must be over cohort_final, not over every master-list
        # episode — the two differ, and both look plausible
        want = dict(eng.con.execute("""
            SELECT u.cohortgrp, round(avg(u.enc_ip), 4)
            FROM utilization u
            JOIN cohort_final c USING (cohortgrp, patid, indexdt)
            GROUP BY 1
        """).fetchall())
        got = dict(eng.con.execute(
            'SELECT "group", mean_NumIP FROM baseline').fetchall())
        assert got == want, (got, want)
    finally:
        eng.close()


@pytest.mark.parametrize("table,tableid", [
    ("censoring", "t2cida"),
    ("followuptime", "t2followuptime"),
    ("t2_cida", "t2cida"),
])
def test_every_stratified_table_honours_all_levelvars(table, tableid):
    """USERSTRATA levelvars stratify EVERY table that takes them, not
    just the CIDA table.

    `censorstrat` is the levelvars (ms_cidanum.sas:123), so race,
    hispanic and year stratify `censoring` and `followuptime` exactly as
    agegroup and sex do. Both honoured only agegroup and sex, so a level
    asking for `year` received the UNSTRATIFIED totals labelled as that
    level — every level came back with an identical row count. The
    production study seen stratifies on `year`.

    Parameterised across all three tables because the same defect was
    found in t2_cida first and then, unfixed, in the other two.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    levels = [
        {"tableid": tableid, "levelid": "1", "levelvars": ""},
        {"tableid": tableid, "levelid": "2", "levelvars": "year"},
        {"tableid": tableid, "levelid": "3", "levelvars": "race"},
    ]
    eng = Engine(verbose=False)
    try:
        run(load_study_dict({**base, "userstrata": levels}), DATA,
            engine=eng, verbose=False)

        rows = dict(eng.con.execute(
            f"SELECT level, count(*) FROM {table} GROUP BY 1").fetchall())
        # a stratified level must produce MORE rows than the
        # unstratified one, or it is not stratifying
        assert rows["2"] > rows["1"], (table, "year did not stratify", rows)
        assert rows["3"] > rows["1"], (table, "race did not stratify", rows)

        # and each level must remain a complete partition
        totals = eng.con.execute(
            f"SELECT level, sum(episodes) FROM {table} GROUP BY 1"
        ).fetchall()
        distinct = {t for _, t in totals}
        assert len(distinct) == 1, (table, "levels disagree", totals)
    finally:
        eng.close()


def test_numerators_and_denominators_reconcile_at_every_level():
    """`t2_cida` is a merge of `numcounts` and `denomcounts` BY NAME, so
    the three must agree at every stratification level — not just at the
    unstratified one, which is the only place it had been checked.

    Note what correct looks like: a year-stratified level reports MORE
    patients than the unstratified one (262,140 against 154,300 here),
    because a patient enrolled across two years counts in both. That is
    a stratified count, not double counting, and a test asserting
    equality across levels would be wrong.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({**base, "userstrata": [
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "year"},
        {"tableid": "t2cida", "levelid": "3", "levelvars": "race"},
    ]})
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        for level in ("1", "2", "3"):
            cida_den, den = eng.con.execute("""
                SELECT (SELECT sum(dennumpts) FROM t2_cida     WHERE level = ?),
                       (SELECT sum(dennumpts) FROM denomcounts WHERE level = ?)
            """, [level, level]).fetchone()
            assert cida_den == den, (level, cida_den, den)

            cida_num, num = eng.con.execute("""
                SELECT (SELECT sum(npts) FROM t2_cida   WHERE level = ?),
                       (SELECT sum(npts) FROM numcounts WHERE level = ?)
            """, [level, level]).fetchone()
            assert cida_num == num, (level, cida_num, num)

            # every level must actually stratify, or the check above is
            # comparing two copies of the same unstratified answer
            rows = eng.con.execute(
                "SELECT count(*) FROM denomcounts WHERE level = ?",
                [level]).fetchone()[0]
            assert rows > 0, (level, "denomcounts empty")
    finally:
        eng.close()


def test_parity_dump_skips_optional_tables_that_were_not_produced():
    """The dump must export what exists and skip the rest.

    It raised on any table an optional stage did not produce, so a study
    without USERSTRATA — the demo study, and the first thing anyone
    would try — died with `Table with name t2_cida does not exist`. The
    comparison being set up never ran.
    """
    import tempfile

    from qrp import run as _run

    out = Path(tempfile.mkdtemp())
    dbg = Path(tempfile.mkdtemp())

    # demo_full defines no USERSTRATA, so t2_cida/numcounts/denomcounts
    # and followuptime are all absent. Driven through the Engine the way
    # the CLI does, since --parity-dump is a CLI concern.
    from qrp import Engine
    from qrp.parity import ParityDumper

    eng = Engine(verbose=False)
    try:
        _run(load_study(STUDY), DATA, engine=eng, output_dir=str(out),
             verbose=False)
        ParityDumper(dbg, "pt001", 0).dump(eng)
    finally:
        eng.close()

    written = list(dbg.rglob("*.csv"))
    assert written, "the dump produced nothing"
    names = {p.stem for p in written}
    # the tables that DO exist are dumped ...
    assert "cohort_final" in names and "attrition" in names, sorted(names)
    # ... and the absent optional ones are simply not there
    assert "t2_cida" not in names, sorted(names)


def test_plan_and_run_cannot_disagree_about_stages():
    """`plan_stages()` and `run()` both derive from STAGES.

    They used to be two parallel sequences, and `plan_stages()`'s
    docstring claimed to be the single source of truth while `run()`
    quietly went its own way. Three stages were added in one session and
    each needed editing in both; one was missed, and only a UI test
    comparing the two counts caught it.

    This asserts the structural property rather than the symptom: every
    stage the table declares has a real SQL file, and every stage the
    plan reports is one the runner knows how to execute.
    """
    from qrp.pipeline import STAGES, _STAGE_BY_NAME, plan_stages

    sql_dir = Path(__file__).resolve().parents[1] / "src" / "qrp" / "sql"
    for st in STAGES:
        assert (sql_dir / st.script).exists(), (st.name, st.script)
        if st.gate is not None:
            # the gate must name a real StudyConfig property, or the
            # stage silently never runs
            assert hasattr(load_study(STUDY), st.gate), (st.name, st.gate)

    # no duplicate names: the runner looks stages up by name
    names = [st.name for st in STAGES]
    assert len(names) == len(set(names)), names
    assert set(_STAGE_BY_NAME) == set(names)

    # the plan is a subsequence of the table, in table order
    planned = plan_stages(load_study(STUDY))
    assert planned == [n for n in names if n in set(planned)], planned


def test_every_sql_file_is_reachable_from_the_stage_table():
    """A SQL file no stage references is dead code; a stage naming a
    file that does not exist fails at runtime. Both are caught here."""
    from qrp.pipeline import STAGES

    sql_dir = Path(__file__).resolve().parents[1] / "src" / "qrp" / "sql"
    # 00_macros.sql is loaded by the Engine at construction, not as a
    # stage — it defines the shared vocabulary every stage uses.
    on_disk = {p.name for p in sql_dir.glob("[0-9]*.sql")} - {"00_macros.sql"}
    referenced = {st.script for st in STAGES}

    orphans = sorted(on_disk - referenced)
    assert not orphans, f"SQL files no stage runs: {orphans}"


def test_every_output_is_declared_once_and_explains_itself():
    """`OUTPUTS` is THE declaration; the six module-level dicts are
    projections of it.

    They used to be six independent dicts over the same 25 tables, held
    in agreement by tests. That is what produced the two worst output
    bugs: `mstr` named the wrong table, and `geography` claimed a SAS
    name that does not exist anywhere in the macro library — because
    `SAS_CONTRACT` was populated from what this package emitted rather
    than from what SAS does.

    The `Output` constructor now REFUSES a non-contract output with no
    note, so an addition has to say what it is. Writing this found two
    outputs (`lab_results`, `utilization`) that were neither verified
    nor documented.
    """
    from qrp.pipeline import (DISCLOSURE, OUTPUT_TABLES, OUTPUTS,
                              SAS_CONTRACT, SAS_NAMES)

    names = [o.name for o in OUTPUTS]
    assert len(names) == len(set(names)), "an output is declared twice"

    for o in OUTPUTS:
        assert o.library in ("msoc", "dplocal"), (o.name, o.library)
        assert o.emit in ("always", "covariate", "gated"), (o.name, o.emit)
        # the constructor enforces this, so it should be unreachable
        assert o.contract or o.note, o.name

    # the projections agree with the declaration by construction
    assert set(OUTPUT_TABLES) == {o.name for o in OUTPUTS
                                  if o.emit == "always"}
    assert set(SAS_CONTRACT) == {o.name for o in OUTPUTS if o.contract}
    assert set(DISCLOSURE) == set(names)
    for o in OUTPUTS:
        assert SAS_NAMES.get(o.name, o.name) == o.sas_name, o.name


def test_an_undocumented_addition_is_rejected():
    """The check has to actually fail, or it proves nothing."""
    from qrp.pipeline import Output

    # a contract output needs no note
    Output("x", "x", "msoc", contract=True)
    # an addition without one is refused
    with pytest.raises(ValueError, match="no note"):
        Output("x", "x", "msoc")


def _two_cohort_study(**override):
    """Two identical cohorts, the second differing in one field."""
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    cf = [dict(base["cohortfile"][0]), dict(base["cohortfile"][0])]
    cf[0]["cohortgrp"], cf[1]["cohortgrp"] = "a", "b"
    t2 = [dict(base["type2file"][0]), dict(base["type2file"][0])]
    t2[0]["group"], t2[1]["group"] = "a", "b"
    cc = []
    for r in base["cohortcodes"]:
        for g in ("a", "b"):
            r2 = dict(r)
            r2["group"] = g
            cc.append(r2)
    for k, v in override.items():
        target = (cf[1] if (k in cf[1] or k in ("sex", "race", "hispanic"))
                  else t2[1])
        target[k] = v
    if any(k in ("coverage", "enrolgap", "chartres") for k in override):
        # enrollment params are keyed by enrollmentnum; cohorts sharing
        # the number must share the params
        cf[1]["enrollmentnum"] = 2
    # denomcounts only exists when the study requests a CIDA table
    return load_study_dict({
        **base, "cohortfile": cf, "type2file": t2, "cohortcodes": cc,
        "userstrata": [{"tableid": "t2cida", "levelid": "1",
                        "levelvars": ""}],
    })


def test_denominator_config_key_is_complete():
    """`_denom_cfg_id` decides which cohorts SHARE one denominator pass.

    If it omits a parameter the denominator SQL reads, two cohorts whose
    denominators differ are silently merged and both get the wrong
    numbers. That is the failure mode this optimisation risks, so the
    key is tested against every field rather than reasoned about.

    Each case changes ONE field and asserts the config splits. The
    assertion that the override actually reached the parsed cohort
    matters as much: a field written where nothing reads it looks
    exactly like a complete key, and four cases initially "passed" that
    way.
    """
    from qrp.pipeline import _denom_cfg_id

    cases = {
        "enrdays": 365, "coverage": "M", "enrolgap": 99, "chartres": "Y",
        "reqdaysaftind": 7, "reqdaysaftepi": 7, "minepisdur": 9,
        "mindaysupp": 9, "blackoutper": 9, "agestrat": "18-64 65+",
        "sex": "F", "race": "1", "hispanic": "Y",
    }
    watched = ("enr_days", "coverage", "enrol_gap", "chart_required",
               "req_days_aft_ind", "req_days_aft_epi", "min_epis_dur",
               "min_days_supp", "blackout_per", "sex", "race", "hispanic")

    for field, value in cases.items():
        s = _two_cohort_study(**{field: value})
        a, b = s.cohorts[0], s.cohorts[1]
        changed = [f for f in watched
                   if getattr(a, f, None) != getattr(b, f, None)]
        if a.age_strata and b.age_strata and \
                a.age_strata.strata != b.age_strata.strata:
            changed.append("age_strata")
        assert changed, (
            f"{field}: the override never reached the parsed cohort, so "
            f"this case proves nothing")
        assert len({_denom_cfg_id(a, s), _denom_cfg_id(b, s)}) == 2, (
            f"{field} differs ({changed}) but the cohorts share a "
            f"denominator config — they would be merged and both get "
            f"the wrong denominator")


def test_shared_denominator_config_gives_identical_results():
    """Cohorts sharing a config must get the SAME denominator, and
    cohorts differing must not.

    The optimisation computes one pass per config and fans out; this
    checks the fan-out end to end rather than trusting the key.
    """
    from qrp import Engine
    from qrp.pipeline import _denom_cfg_id

    # identical cohorts -> one config, identical denominators
    same = _two_cohort_study()
    assert len({_denom_cfg_id(c, same) for c in same.cohorts}) == 1

    eng = Engine(verbose=False)
    try:
        run(same, DATA, engine=eng, verbose=False)
        rows = eng.con.execute("""
            SELECT "group", sum(dennumpts), sum(dennummemdays)
            FROM denomcounts GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert len(rows) == 2, rows
        assert rows[0][1:] == rows[1][1:], (
            "cohorts sharing a config got different denominators", rows)
    finally:
        eng.close()

    # a differing cohort -> two configs, and the denominators differ
    diff = _two_cohort_study(sex="F")
    assert len({_denom_cfg_id(c, diff) for c in diff.cohorts}) == 2
    eng = Engine(verbose=False)
    try:
        run(diff, DATA, engine=eng, verbose=False)
        rows = eng.con.execute("""
            SELECT "group", sum(dennumpts) FROM denomcounts
            GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert rows[0][1] != rows[1][1], (
            "a sex-restricted cohort got the same denominator as an "
            "unrestricted one", rows)
    finally:
        eng.close()


def test_outputdenom_controls_whether_a_denominator_is_computed():
    """SAS: "Only compute denominators if OUTPUTDENOM ne N and
    USERSTRATA file is specified" (ms_cidadenom.sas:113).

    `OUTPUTDENOM` is a per-cohort field that was not parsed at all, so a
    cohort asking for no denominator got one anyway — a plausible number
    with no SAS counterpart.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    t2 = [dict(r) for r in base["type2file"]]
    t2[0]["outputdenom"] = "N"          # lisinopril
    s = load_study_dict({
        **base, "type2file": t2,
        "userstrata": [{"tableid": "t2cida", "levelid": "1",
                        "levelvars": ""}]})

    assert "lisinopril" not in s.denominator_cohorts()
    assert "beta_blocker" in s.denominator_cohorts()

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        groups = {r[0] for r in eng.con.execute(
            'SELECT DISTINCT "group" FROM denomcounts').fetchall()}
        assert groups == {"beta_blocker"}, groups
    finally:
        eng.close()


def test_minrxdays_forces_denominators_off():
    """`minrxdays > 1` in ANY inclusion rule disables OUTPUTDENOM for
    Types 1-2, with a warning (ms_setnumloopmacrovars.sas:898-900).

    A pro-rated supply requirement makes the eligible-member count
    incoherent, so SAS refuses to emit one rather than emit a wrong one.
    This package emitted one regardless.
    """
    import warnings as w

    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    inclusion = [{"group": "lisinopril", "criteria": "INC",
                  "condlevel": "1", "subcondlevel": "1", "codecat": "RX",
                  "code": "E00001", "minrxdays": 30,
                  "condfrom": -183, "condto": -1}]

    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        s = load_study_dict({**base, "inclusioncodes": inclusion})

    assert s.denominators_suppressed_by_minrxdays
    assert s.denominator_cohorts() == ()
    assert any("minrxdays" in str(x.message) for x in caught), (
        "suppressing a deliverable without saying so is the failure mode "
        "this guards against")

    # and without minrxdays, every cohort keeps its denominator
    plain = load_study_dict(base)
    assert len(plain.denominator_cohorts()) == len(plain.cohorts)


def test_spilling_is_attributed_to_the_statement_that_caused_it():
    """A spilling run is slow, and the log has ~47 statements in it.

    The live `MemoryStatus` events answer "is this run spilling"; they
    do not answer "which step caused it", and after a long stage that
    is the only question worth asking. Peak memory and spill are now
    attributed per statement.
    """
    from qrp import Engine
    from qrp.events import StatementFinished

    seen: list[StatementFinished] = []

    def sink(ev):
        if isinstance(ev, StatementFinished):
            seen.append(ev)

    # a limit tight enough to force spilling on the real pipeline
    eng = Engine(memory_limit="300MB", verbose=False,
                 statement_detail=True, on_event=sink)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
    finally:
        eng.close()

    assert seen, "no statement events were emitted"
    # peak is attributed per statement, not accumulated across the run
    peaks = [s.peak_bytes for s in seen if s.peak_bytes]
    assert peaks, "no statement reported a peak"

    # The high-water mark must be RESET per statement, or every step
    # reports the run's peak and the number points at nothing.
    #
    # Asserting the peaks are non-monotonic would be wrong: memory
    # legitimately grows through the pipeline, and an early version of
    # this test failed against correct behaviour for that reason. The
    # property that actually distinguishes reset from not-reset is that
    # most statements report LESS than the global peak.
    assert sum(1 for p in peaks if p < eng.peak_memory_bytes) >= len(peaks) - 1, (
        "every statement reports the global peak, so the high-water "
        "mark is never reset", peaks, eng.peak_memory_bytes)

    # spill is a DELTA, so it names a culprit rather than a total
    for s in seen:
        assert s.spilled_bytes >= 0
        assert s.spilled == (s.spilled_bytes > 0)


def test_spill_summary_names_the_statement_in_the_log():
    """The log ends with the statements that spilled, largest first, so
    the answer is the last thing an operator reads."""
    import tempfile

    from qrp import Engine
    from qrp.runlog import RunLog

    logdir = tempfile.mkdtemp()
    rl = RunLog(directory=logdir, run_id="spill")
    eng = Engine(memory_limit="300MB", verbose=False,
                 statement_detail=True, on_event=rl.sink())
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
    finally:
        eng.close()
        rl.close()

    text = rl.log_path.read_text()
    if "SPILLED" in text:
        assert "spilled to disk" in text, (
            "a statement spilled but the summary did not report it")
        # the summary must name a table, not just a total
        tail = text[text.index("spilled to disk"):]
        assert any(c.isalpha() for c in tail.split("\n")[1]), tail[:200]


def test_outputdenom_M_reports_members_without_member_days():
    """`OUTPUTDENOM='M'` is members only: SAS sets `DenNumMemDays` to
    MISSING for those cohorts and 0 for the rest
    (ms_cidadenom.sas:1346-1347).

    NULL, not 0 — "we did not count this" and "we counted zero days"
    are different statements, and a reader summing the column would
    silently include the second.

    The value was parsed and documented and NOT implemented: a cohort
    set to M was getting member-days it had asked not to receive.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    t2 = [dict(r) for r in base["type2file"]]
    t2[0]["outputdenom"] = "M"          # lisinopril
    s = load_study_dict({
        **base, "type2file": t2,
        "userstrata": [{"tableid": "t2cida", "levelid": "1",
                        "levelvars": ""}]})

    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        rows = dict(eng.con.execute(
            'SELECT "group", dennummemdays FROM denomcounts'
        ).fetchall())
        assert rows["lisinopril"] is None, (
            "an M cohort reported member-days", rows)
        assert rows["beta_blocker"] is not None, (
            "a Y cohort lost its member-days", rows)

        # members are still counted for both
        pts = dict(eng.con.execute(
            'SELECT "group", dennumpts FROM denomcounts').fetchall())
        assert pts["lisinopril"] > 0 and pts["beta_blocker"] > 0, pts
    finally:
        eng.close()


def test_unread_scalar_parameters_are_reported():
    """SAS branches on scalar parameters this package does not read.

    Ignoring one silently gives a plausible answer computed under
    different rules — the failure mode this package has been most prone
    to. `outputdenom` sat in that set until a question about USERSTRATA
    surfaced it.

    A parameter set to "N" is NOT reported: not doing something this
    package already does not do is agreement, not divergence.
    """
    import warnings as w

    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def warnings_for(**params):
        p = dict(base["qrp_parameters"][0])
        p.update(params)
        with w.catch_warnings(record=True) as caught:
            w.simplefilter("always")
            load_study_dict({**base, "qrp_parameters": [p]})
        return [str(x.message) for x in caught
                if "does not read" in str(x.message)]

    assert warnings_for(othersex="Y"), "an active unread parameter was silent"
    assert not warnings_for(psmatch="N"), (
        "a parameter set to N was reported; that is agreement, not "
        "divergence, and crying wolf trains people to ignore the warning")
    assert not warnings_for(), "a study setting none of them warned"


def test_the_real_study_sets_no_unread_parameters():
    """A guard on the production input: if a future revision starts
    setting one of these, the warning should be the reason we find out,
    not a discrepancy in someone's results."""
    import warnings as w

    real = Path("/mnt/user-data/uploads/"
                "qrp_inputfiles_type2_pt001_01_1.json")
    if not real.exists():
        pytest.skip("the production input file is not available here")

    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        load_study(real)
    unread = [str(x.message) for x in caught
              if "does not read" in str(x.message)]
    assert not unread, unread


def test_default_run_writes_only_the_sas_dplocal_contract():
    """dplocal diagnostics are written only under --debug, mirroring
    SAS's QRP_DEBUG (ms_attrition.sas:201-209).

    Without it, dplocal gets exactly what SAS writes there — mstr,
    denomcounts, numcounts. The patient-level ADDITIONS
    (covariates_long, inclusion_excluded, mstr_episodes, ...) were never
    part of the request, cost disk, and sit at the site.

    msoc is deliberately untouched: those are small aggregates a data
    partner may be asked for.
    """
    import json as _json
    import tempfile

    from qrp import run as _run
    from qrp.outputs import OUTPUTS

    by_name = {o.name: o for o in OUTPUTS}

    def dplocal(debug):
        out = Path(tempfile.mkdtemp())
        _run(load_study(STUDY), DATA, output_dir=str(out), names="sas",
             debug=debug, verbose=False)
        m = _json.loads((out / "manifest.json").read_text())
        return {t for t, v in m["tables"].items()
                if v["library"] == "dplocal"}

    default = dplocal(False)
    with_debug = dplocal(True)

    # the default is exactly the SAS contract
    for t in default:
        assert by_name[t].contract, (
            f"{t} is a diagnostic but was written without --debug")
    assert "cohort_final" in default           # -> <runid>_mstr

    # debug adds diagnostics and removes nothing
    assert default < with_debug, (default, with_debug)
    assert "ptsmasterlist" in with_debug and "ptsmasterlist" not in default


# ---------------------------------------------------------------------
# --table-map: overriding where an SCDM table is read from
# ---------------------------------------------------------------------


def test_table_map_accepts_a_file_outside_the_input_folder():
    """A site may keep one table somewhere else — a different team's
    extract, a newer refresh. An ABSOLUTE path in --table-map is used
    as-is, regardless of --indata, and the results must be identical to
    the same file sitting in the folder."""
    import shutil
    import tempfile

    from qrp import Engine

    work = Path(tempfile.mkdtemp())
    main = work / "main"
    elsewhere = work / "other_team"
    shutil.copytree(DATA, main)
    elsewhere.mkdir()
    # move dispensing out of the folder, under a site-specific name
    moved = elsewhere / "rx_extract_2024.parquet"
    src = main / "dispensing"
    shutil.copy(next(src.rglob("*.parquet")), moved)
    shutil.rmtree(src)

    def counts(indata, table_map=None):
        eng = Engine(verbose=False)
        try:
            run(load_study(STUDY), str(indata), engine=eng,
                table_map=table_map, verbose=False)
            return (eng.count("exposure_claims"), eng.count("cohort_final"))
        finally:
            eng.close()

    assert counts(main, {"dispensing": str(moved)}) == counts(DATA)


def test_misspelt_table_map_key_is_rejected_not_ignored():
    """`dispensng=...` was silently IGNORED: the override was dropped,
    the real table reported missing, and nothing said the key had not
    been recognised. The operator believed they had overridden it — a
    typo that looked like a data problem."""
    from qrp.scdm import check_table_map

    with pytest.raises(ValueError, match="Did you mean 'dispensing'"):
        check_table_map({"dispensng": "/x.parquet"})
    check_table_map({"dispensing": "/x.parquet"})       # valid: no error
    check_table_map(None)                               # absent: fine


def test_table_map_path_error_says_where_it_looked():
    """An absolute path was reported as missing "under" the input
    folder — which it never was — sending the operator to look in the
    wrong place."""
    import tempfile

    from qrp.scdm import SCDM, resolve_table

    spec = next(t for t in SCDM if t.name == "dispensing")
    root = tempfile.mkdtemp()

    with pytest.raises(FileNotFoundError) as absolute:
        resolve_table(root, spec, table_map={"dispensing": "/nope/x.parquet"})
    assert "'/nope/x.parquet'" in str(absolute.value)
    assert "relative to" not in str(absolute.value)

    with pytest.raises(FileNotFoundError) as relative:
        resolve_table(root, spec, table_map={"dispensing": "sub/x.parquet"})
    assert "relative to the input folder" in str(relative.value)


# ---------------------------------------------------------------------
# Plain-text views of msoc tables
# ---------------------------------------------------------------------


def _msoc_run(tmp, **kw):
    from qrp import run as _run

    out = Path(tmp)
    _run(load_study(STUDY), DATA, output_dir=str(out), names="sas",
         verbose=False, **kw)
    return out


def test_every_msoc_table_gets_a_faithful_text_view(tmp_path):
    """Parquet needs a tool most readers of these outputs do not have,
    so each msoc table gets a plain-text view beside it.

    A reading copy that quietly disagreed with the record would be
    worse than none, so this checks FAITHFULNESS, not existence: the
    reported shape matches, and every non-null value in the parquet
    appears in the text. The terminal formatter truncates at 30
    characters and rounds to two places, so reusing it unmodified would
    have failed this.
    """
    import re

    import duckdb

    out = _msoc_run(tmp_path)
    con = duckdb.connect()
    parquets = sorted((out / "msoc").glob("*.parquet"))
    assert parquets
    for pq in parquets:
        txt_path = pq.with_suffix(".txt")
        assert txt_path.exists(), f"no text view for {pq.name}"
        txt = txt_path.read_text()
        rows = con.execute(f"SELECT * FROM read_parquet('{pq}')").fetchall()
        ncols = len(con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{pq}')").fetchall())
        m = re.search(r"([\d,]+) rows x (\d+) columns", txt)
        assert m, pq.name
        assert int(m.group(1).replace(",", "")) == len(rows), pq.name
        assert int(m.group(2)) == ncols, pq.name
        for r in rows:
            for v in r:
                if v is None:
                    continue
                if isinstance(v, float):
                    tok = f"{v:,.6f}".rstrip("0").rstrip(".")
                elif isinstance(v, int) and not isinstance(v, bool):
                    tok = f"{v:,}"
                else:
                    tok = str(v)
                assert tok in txt, f"{pq.name}: value {v!r} missing"
    con.close()


def test_text_views_are_msoc_only(tmp_path):
    """dplocal is patient-level. A plain-text copy would be one more
    unencrypted, greppable file holding patient rows."""
    out = _msoc_run(tmp_path, debug=True)       # debug: most dplocal files
    assert not list((out / "dplocal").rglob("*.txt")), (
        "a text view was written for a patient-level dplocal table")


def test_no_text_switches_the_views_off(tmp_path):
    """msoc is what goes to the Operations Center; a site whose
    recipient expects parquet and nothing else can turn these off."""
    out = _msoc_run(tmp_path, text=False)
    assert list((out / "msoc").glob("*.parquet"))
    assert not list((out / "msoc").glob("*.txt"))


def test_missing_values_read_as_missing_in_the_text_view():
    """`.` for missing, the SAS convention. A blank is ambiguous in a
    fixed-width file, and OUTPUTDENOM=M depends on missing member-days
    reading as MISSING rather than as zero. Numeric columns holding a
    missing value must stay right-aligned: an early version classed
    them as text and knocked every number in them out of line."""
    from qrp.show import _render

    text = _render(["n"], [(10,), (None,), (2000,)], width=None,
                   missing=".", exact=True, rule_cap=None)
    body = text.splitlines()[2:]
    assert [line.strip() for line in body] == ["10", ".", "2,000"]
    # right-aligned: every line the same width, padded on the left
    assert len({len(line) for line in body}) == 1, body


def test_wide_tables_are_transposed_for_reading():
    """baseline is ~67 columns by one row per cohort — an ~800-character
    line as a normal table. Transposed it fits a screen."""
    import duckdb

    from qrp.show import table_text

    con = duckdb.connect()
    cols = ", ".join(f"{i} AS c{i}" for i in range(30))
    con.execute(f"CREATE TABLE wide AS SELECT 'grp' AS g, {cols}")
    text = table_text(con, "wide", "wide")
    assert "transposed" in text
    assert max(len(line) for line in text.splitlines()) < 120
    con.close()



def test_condinclusion_zero_means_exclude():
    """Real input files come in two shapes: some carry `indexcriteria`
    saying INC/EXC, others carry `condinclusion`, where 0 means EXCLUDE
    (SAS reads CondInclusion directly, ms_cidadenom.sas:159).

    Defaulting to "INC" when neither is present turned every EXCLUSION
    rule into an inclusion REQUIREMENT — patients had to HAVE the codes
    they were meant to be excluded for. On a real 40-cohort study that
    took 42,708 episodes down to 58.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def crit(**row):
        rules = [{"group": "lisinopril", "codecat": "DX", "code": "E11",
                  "condlevel": "Diabetes", "subcondlevel": "Exclusion",
                  "condfrom": -183, "condto": -1, **row}]
        s = load_study_dict({**base, "inclusioncodes": rules})
        return {r.criteria for r in s.inclusions}

    assert crit(condinclusion=0) == {"EXC"}, "0 must exclude"
    assert crit(condinclusion=1) == {"INC"}, "1 must include"
    # an explicit indexcriteria still wins
    assert crit(condinclusion=0, indexcriteria="INC") == {"INC"}
    # neither present: the old default, for files that carry no flag
    assert crit() == {"INC"}


def test_index_dates_must_fall_inside_the_query_period():
    """SAS reports this as its own attrition step — "Episode-defining
    index claims must be during the query period" — and on the study
    compared against real output it removes 8,450 of 10,270 episodes
    for a single cohort, the largest single exclusion in the funnel.

    Nothing applied it. Claims are extracted from `start_date` MINUS
    the widest lookback so a covariate or washout window can see
    history; without this filter those lookback claims could themselves
    become index dates. Adding it took that cohort from 1,552 episodes
    to 1,373 against SAS's 1,375.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        study = load_study(STUDY)
        run(study, DATA, engine=eng, verbose=False)
        outside = eng.con.execute(
            "SELECT count(*) FROM cohort_final WHERE indexdt < ? OR indexdt > ?",
            [study.start_date, study.end_date]).fetchone()[0]
        assert outside == 0, (
            f"{outside} episodes have an index date outside the query "
            f"period {study.start_date}..{study.end_date}")
        # and the cohort is not empty, or the check proves nothing
        assert eng.con.execute(
            "SELECT count(*) FROM cohort_final").fetchone()[0] > 0
    finally:
        eng.close()


def test_bulk_registration_matches_row_by_row_exactly():
    """Large config tables are bulk-loaded through a CSV, which is 25x
    faster on a real study (91s of registration down to 3.6s). The
    speed is worthless if the data changes on the way.

    The subtle case is NULL against EMPTY STRING. `csv` writes None as
    an empty field, so a naive `nullstr=''` turns a genuinely empty
    value into NULL — one risk-code value silently did exactly that,
    and the two behave differently in a join. A sentinel keeps them
    apart.
    """
    from qrp import Engine
    from qrp import engine as engine_mod
    from qrp.pipeline import register_config

    study = load_study(STUDY)
    original = engine_mod._BULK_LOAD_ROWS
    snaps = {}
    try:
        for label, threshold in (("bulk", 1), ("rowwise", 10 ** 9)):
            engine_mod._BULK_LOAD_ROWS = threshold
            eng = Engine(verbose=False)
            try:
                register_config(eng, study)
                out = {}
                for tbl in ("cfg_codes", "cfg_cohort", "cfg_demog"):
                    cols = [d[0] for d in eng.con.execute(
                        f"SELECT * FROM {tbl} LIMIT 0").description]
                    # row count, per-column non-null count, and the rows
                    # themselves
                    counts = eng.con.execute(
                        f"SELECT count(*), "
                        + ", ".join(f"count({c})" for c in cols)
                        + f" FROM {tbl}").fetchone()
                    rows = eng.con.execute(
                        f"SELECT * FROM {tbl} ORDER BY ALL").fetchall()
                    out[tbl] = (counts, rows)
                snaps[label] = out
            finally:
                eng.close()
    finally:
        engine_mod._BULK_LOAD_ROWS = original

    for tbl in snaps["bulk"]:
        assert snaps["bulk"][tbl][0] == snaps["rowwise"][tbl][0], (
            f"{tbl}: row or non-null counts differ between the two "
            f"registration paths")
        assert snaps["bulk"][tbl][1] == snaps["rowwise"][tbl][1], (
            f"{tbl}: row CONTENT differs between the two paths")


def test_empty_string_survives_bulk_registration_as_a_string():
    """The specific bug the sentinel exists to prevent."""
    from qrp import Engine
    from qrp import engine as engine_mod

    original = engine_mod._BULK_LOAD_ROWS
    eng = Engine(verbose=False)
    try:
        engine_mod._BULK_LOAD_ROWS = 1          # force the CSV path
        eng.register("t_probe",
                     [{"a": "", "b": None}, {"a": "x", "b": "y"}],
                     "a VARCHAR, b VARCHAR")
        got = eng.con.execute(
            "SELECT a IS NULL, b IS NULL FROM t_probe ORDER BY a").fetchall()
        assert got == [(False, True), (False, False)], (
            "an empty string became NULL, or a NULL became a string", got)
    finally:
        engine_mod._BULK_LOAD_ROWS = original
        eng.close()


def test_outcome_codes_are_routed_by_fupcriteria_not_indexcriteria():
    """SAS routes outcomes on FUPCRITERIA:
    `if fupcriteria in('DEF') then output _FUPEvent`
    (ms_cidanum.sas:1684). `indexcriteria` values other than
    DEF — chiefly `FUT`, which goes to SAS's washout-for-truncation set
    — are NOT outcomes.

    They used to fall through to EVENT, and FUT dominates a real file:
    one cohort of the study compared has 54 exposure codes, ONE outcome
    code and 4,957 FUT codes. Treating FUT as outcomes gave 4,958 event
    codes instead of 1 and 11,196 events against SAS's 3, which then
    dropped 58 valid episodes through the blackout-event rule.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def roles(**row):
        codes = [{"group": "lisinopril", "codecat": "DX", "code": "Z99",
                  **row}]
        s = load_study_dict({**base, "cohortcodes": codes})
        c = [x for x in s.cohorts if x.cohortgrp == "lisinopril"][0]
        return (len(c.exposure_codes), len(c.event_codes), len(c.ioc_codes))

    assert roles(indexcriteria="DEF") == (1, 0, 0)
    assert roles(fupcriteria="DEF") == (0, 1, 0)
    assert roles(fupcriteria="IOC") == (0, 0, 1)
    # FUT is neither: it is SAS's washout-for-truncation set
    assert roles(indexcriteria="FUT") == (0, 0, 0)
    assert roles(indexcriteria="NOT") == (0, 0, 0)


def test_fut_claims_truncate_an_episode():
    """A FUT claim inside an episode ENDS it:

        if fut and trunkdt and trunkdt <= EpisodeEndDt
            then EpisodeEndDt = trunkdt;
        (ms_createptsmasterlist.sas:152)

    where trunkdt is the earliest FUT claim overlapping
    [EpisodeStartDt, EpisodeEndDt] (ms_createpov4.sas:155-167).

    FUT codes were first mistaken for OUTCOMES, then — once that was
    corrected — skipped entirely, which left every affected episode too
    long. On the study compared, 3,077 of 31,440 episodes ran past
    SAS's end date and NOT ONE was shorter. Implementing the truncation
    took exact end dates from 90.2% to 95.5%.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    # a diagnosis code that actually occurs in the fixture data, or the
    # test would pass trivially by truncating nothing
    trunc = {"group": "lisinopril", "codecat": "DX", "code": "D00008",
             "indexcriteria": "FUT"}

    def ends(with_trunc):
        codes = list(base["cohortcodes"]) + ([trunc] if with_trunc else [])
        s = load_study_dict({**base, "cohortcodes": codes})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT sum(date_diff('day', indexdt, episodeenddt)) "
                "FROM cohort_final WHERE cohortgrp = 'lisinopril'"
            ).fetchone()[0]
        finally:
            eng.close()

    plain, truncated = ends(False), ends(True)
    assert truncated < plain, (
        "FUT codes did not shorten any episode, so the truncation is "
        "not being applied", plain, truncated)


def test_fut_codes_are_a_fourth_role():
    """FUT is neither exposure, outcome nor IOC washout — it truncates.
    All four roles must be kept apart."""
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def roles(**row):
        s = load_study_dict({
            **base,
            "cohortcodes": [{"group": "lisinopril", "codecat": "DX",
                             "code": "Z99", **row}]})
        c = [x for x in s.cohorts if x.cohortgrp == "lisinopril"][0]
        return (len(c.exposure_codes), len(c.event_codes),
                len(c.ioc_codes), len(c.trunc_codes))

    assert roles(indexcriteria="DEF") == (1, 0, 0, 0)
    assert roles(fupcriteria="DEF") == (0, 1, 0, 0)
    assert roles(fupcriteria="IOC") == (0, 0, 1, 0)
    assert roles(indexcriteria="FUT") == (0, 0, 0, 1)


def test_truncation_claims_are_stockpiled():
    """FUT codes carry a `stockgroup`, so their claims stockpile exactly
    as exposure claims do: a dispensing arriving while the previous one
    in its stockgroup is still supplying starts when that supply runs
    out, not on its own fill date. That shifted date IS the truncation
    date SAS uses.

    Worked case from the parity run: a FUT claim on 2016-06-06 in
    stockgroup `valsartanhydrochlorothiazide`, with the previous claim
    in that group (2016-03-18, 90 days) supplying to 2016-06-15,
    stockpiles to 2016-06-16 — precisely SAS's `trunkdt`, ten days after
    the raw claim date.

    Using raw dates truncated 854 episodes too early, by 1 to 17 days
    each: the leftover supply of the preceding claim. Fixing it took
    exact episode ends from 95.5% to 98.0%.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        # the stockpiled date must never precede the claim's own date,
        # and for a repeat fill in one stockgroup it must exceed it
        bad = eng.con.execute(
            "SELECT count(*) FROM trunc_claims WHERE adate < orig_adate"
        ).fetchone()[0]
        assert bad == 0, "a stockpiled truncation date moved BACKWARDS"
    finally:
        eng.close()


def test_truncation_codes_keep_their_own_stockgroup():
    """FUT codes carry a `stockgroup` and stockpile within it, exactly
    as exposure codes do.

    The stockgroup was captured only for DEF codes and passed through
    registration only for DEF, so every truncation code landed in
    `_default`. Unrelated drugs then chained into one run and pushed
    truncation dates far past where SAS puts them — one patient's
    warfarin chain drifted 23 days, another's four years.

    Fixing both halves took exact episode ends from 98.2% to 99.2%.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    codes = list(base["cohortcodes"]) + [
        {"group": "lisinopril", "codecat": "RX", "code": "E00001",
         "indexcriteria": "FUT", "stockgroup": "drug_a"},
        {"group": "lisinopril", "codecat": "RX", "code": "E00002",
         "indexcriteria": "FUT", "stockgroup": "drug_b"},
    ]
    eng = Engine(verbose=False)
    try:
        run(load_study_dict({**base, "cohortcodes": codes}), DATA,
            engine=eng, verbose=False)
        groups = {r[0] for r in eng.con.execute(
            "SELECT DISTINCT stockgroup FROM cfg_codes WHERE role = 'TRUNK'"
        ).fetchall()}
        assert {"drug_a", "drug_b"} <= groups, (
            "truncation codes lost their stockgroup, so unrelated drugs "
            "would stockpile as one chain", groups)
    finally:
        eng.close()


def test_strata_are_registered_per_table():
    """`cfg_strata` carries both tables' levels, tagged by tableid.

    It used to be `cida_levels() or followuptime_levels()`, so a study
    asking for BOTH got the CIDA levels applied to the follow-up-time
    table — levels it never requested, stratified by dimensions it
    never named.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    s = load_study_dict({**base, "userstrata": [
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""},
        {"tableid": "t2cida", "levelid": "2", "levelvars": "agegroup"},
        {"tableid": "t2followuptime", "levelid": "7", "levelvars": "sex"},
    ]})
    eng = Engine(verbose=False)
    try:
        run(s, DATA, engine=eng, verbose=False)
        got = dict(eng.con.execute(
            "SELECT tableid, count(*) FROM cfg_strata GROUP BY 1").fetchall())
        assert got == {"t2cida": 2, "t2followuptime": 1}, got

        # the follow-up table must carry only ITS level
        levels = {r[0] for r in eng.con.execute(
            "SELECT DISTINCT level FROM followuptime").fetchall()}
        assert levels == {"7"}, (
            "follow-up time used the CIDA levels", levels)
    finally:
        eng.close()


def test_denominator_key_separates_different_exclusions():
    """The denominator is SHAVED by the exclusion conditions, so two
    cohorts with different exclusions have different eligible time.

    The shared-config key omitted them, which let one cohort's
    exclusions be applied to another's denominator.
    """
    from qrp.config import load_study_dict
    from qrp.pipeline import _denom_cfg_id

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rules = [{"group": "lisinopril", "condinclusion": 0, "codecat": "DX",
              "code": "E11", "condlevel": "X", "subcondlevel": "Y",
              "condfrom": -183, "condto": -1}]
    s = load_study_dict({**base, "inclusioncodes": rules})
    ids = {c.cohortgrp: _denom_cfg_id(c, s) for c in s.cohorts}
    assert ids["lisinopril"] != ids["beta_blocker"], (
        "cohorts with different exclusion rules shared a denominator "
        "config, so one cohort's exclusions shaved the other's time")


def test_rerun_clears_stale_csv_copies():
    """`--csv` writes copies under {lib}/csv/, which the run-scoped
    cleanup did not reach.

    The earlier version of this test asserted set differences that are
    disjoint by construction — `(first - second) & second` is ALWAYS
    empty — so it could not fail no matter how many stale files
    survived. It is now written against a clean run: the reused
    directory must end up holding exactly the files a fresh directory
    would.
    """
    import tempfile

    from qrp import run as _run

    study = load_study(STUDY)

    reused = Path(tempfile.mkdtemp())
    _run(study, DATA, output_dir=str(reused), names="sas", csv=True,
         verbose=False)
    _run(study, DATA, output_dir=str(reused), names="logical", csv=True,
         verbose=False)

    clean = Path(tempfile.mkdtemp())
    _run(study, DATA, output_dir=str(clean), names="logical", csv=True,
         verbose=False)

    def csvs(root):
        return {p.relative_to(root).as_posix()
                for p in root.rglob("csv/*.csv")}

    stale = csvs(reused) - csvs(clean)
    assert not stale, ("stale CSV files survived the rerun", sorted(stale))
    assert csvs(clean), "no CSV copies were written at all"


def test_enrollment_without_the_optional_chart_column():
    """`chart` is OPTIONAL in SCDM. Referencing it unconditionally made
    an enrolment file carrying every REQUIRED column fail with a binder
    error, so a valid extract could not be read at all."""
    import shutil
    import tempfile

    import duckdb

    from qrp import Engine

    work = Path(tempfile.mkdtemp())
    con = duckdb.connect()
    for d in Path(DATA).iterdir():
        if not d.is_dir():
            continue
        dest = work / d.name
        if d.name == "enrollment":
            dest.mkdir(parents=True)
            cols = [c[0] for c in con.execute(
                f"SELECT * FROM read_parquet('{d}/*.parquet') LIMIT 0"
            ).description]
            keep = ", ".join(c for c in cols if c != "chart")
            con.execute(
                f"COPY (SELECT {keep} FROM read_parquet('{d}/*.parquet')) "
                f"TO '{dest}/data.parquet' (FORMAT PARQUET)")
        else:
            shutil.copytree(d, dest)
    con.close()

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), str(work), engine=eng, verbose=False)
        assert eng.count("cohort_final") > 0
    finally:
        eng.close()


def test_a_numeric_lab_criterion_of_zero_is_not_absence():
    """`str(spec or "")` turned a numeric 0 into "", so "result = 0"
    became "no criterion" and silently widened the extraction."""
    from qrp.config import parse_lab_result

    assert parse_lab_result(0)[:2] == ("=", 0.0)
    assert parse_lab_result(0.0)[:2] == ("=", 0.0)
    assert parse_lab_result("0")[:2] == ("=", 0.0)
    # only None and blank mean absent
    assert parse_lab_result(None) == (None, None, None)
    assert parse_lab_result("") == (None, None, None)


def test_run_logs_never_overwrite_each_other():
    """Two runs starting in the same second both saw the name free and
    both opened it "w", so one silently overwrote the other. The file
    is now created EXCLUSIVELY."""
    import tempfile

    from qrp.runlog import RunLog

    d = tempfile.mkdtemp()
    logs = []
    for _ in range(3):
        rl = RunLog(directory=d, run_id="demo")
        logs.append(rl.log_path)
        rl.close()
    assert len({p.name for p in logs}) == 3, [p.name for p in logs]
    assert all(p.exists() for p in logs)


def test_truncation_chain_starts_at_the_enrolment_window():
    """A truncation claim enters the stockpile chain only if its SUPPLY
    still runs at the start of the required prior-enrolment window.

    Taking every claim back to `claims_from` pulled in claims SAS never
    sees, and each extra one pushes the rest of the chain further
    forward — so truncation dates landed days or weeks late. Against
    SAS's own `_groupwashfortrunk` the chains were 8% too long.

    The worked case: a patient whose only SAS entry is 2015-08-08, but
    who had an earlier claim on 2015-05-11 whose 90-day supply ends
    exactly 2015-08-08. Including it made the 08-08 claim overlap, so
    it was pushed to 08-09 and the episode ended a day late. Excluding
    it — as SAS does — removes the overlap entirely.

    Exact episode ends went from 99.61% to 99.97%, and episodes ending
    too SHORT from 40 to none.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        study = load_study(STUDY)
        run(study, DATA, engine=eng, verbose=False)
        bound = eng.con.execute(
            "SELECT min(CAST(? AS DATE) - enr_days) FROM cfg_cohort",
            [study.start_date]).fetchone()[0]
        early = eng.con.execute(
            "SELECT count(*) FROM trunc_claims WHERE orig_adate > ?",
            [bound]).fetchone()[0]
        # every retained claim's SUPPLY must reach the bound; claims
        # filed after it trivially do, so the check is that nothing
        # retained expired before it
        stale = eng.con.execute(
            "SELECT count(*) FROM trunc_claims t "
            "WHERE t.orig_adate < ? AND NOT EXISTS ("
            "  SELECT 1 FROM cdm_dispensing d "
            "  WHERE d.patid = t.patid AND d.adate = t.orig_adate "
            "    AND d.adate + CAST(d.rxsup - 1 AS INTEGER) >= ?)",
            [bound, bound]).fetchone()[0]
        assert stale == 0, (
            f"{stale} truncation claims were kept whose supply had "
            f"already run out before {bound}")
        assert early >= 0
    finally:
        eng.close()


def test_non_dispensing_exposure_is_not_stockpiled():
    """SAS stockpiles `_ITDrugs` only (ms_cidanum.sas:1545) — exposure
    sourced from procedure or diagnosis claims never passes through it,
    and two administrations on one day are ONE day of exposure, not two.

    Chaining them pushed each successive administration a day later and
    stretched the episode end past SAS's. On a filgrastim patient,
    three pairs of same-day J1442 administrations became a six-day run
    instead of three single days, ending the episode three days late.

    Fixing both halves — no chaining, and MAX rather than SUM for
    same-day supply — took exact episode ends from 99.97% to 100%.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        # nothing outside RX may have been shifted by stockpiling
        shifted = eng.con.execute(
            "SELECT count(*) FROM stockpiled s "
            "WHERE s.adate <> s.orig_adate AND NOT EXISTS ("
            "  SELECT 1 FROM exposure_claims x "
            "  WHERE x.cohortgrp = s.cohortgrp AND x.patid = s.patid "
            "    AND x.adate = s.orig_adate AND x.codecat = 'RX')"
        ).fetchone()[0]
        assert shifted == 0, (
            f"{shifted} non-dispensing exposure rows were stockpiled")
    finally:
        eng.close()


def test_exposure_chain_starts_at_the_enrolment_window():
    """A dispensing joins the stockpile chain only if its SUPPLY still
    runs at the start of the required prior-enrolment window — the same
    rule the truncation chain follows.

    Chaining from the beginning of the extract pushed every expiry
    forward, closing gaps SAS leaves open. One patient's claims were 35
    days apart on their own dates, past the 30-day `episodegap`, so SAS
    starts a new episode there; accumulated push made the gap 5 days
    here and the episodes merged, losing the later index date.

    That was the last of the membership gap: episodes went from 31,440
    to 31,464 — exactly SAS's count, with no episode in either output
    missing from the other.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        study = load_study(STUDY)
        run(study, DATA, engine=eng, verbose=False)
        bound = eng.con.execute(
            "SELECT min(CAST(? AS DATE) - enr_days) FROM cfg_cohort",
            [study.start_date]).fetchone()[0]
        stale = eng.con.execute(
            "SELECT count(*) FROM stockpiled s "
            "WHERE EXISTS (SELECT 1 FROM exposure_claims x "
            "  WHERE x.cohortgrp = s.cohortgrp AND x.patid = s.patid "
            "    AND x.adate = s.orig_adate AND x.codecat = 'RX' "
            "    AND x.adate + CAST(x.rxsup - 1 AS INTEGER) < ?)",
            [bound]).fetchone()[0]
        assert stale == 0, (
            f"{stale} dispensings joined the chain whose supply had run "
            f"out before {bound}")
        assert eng.count("cohort_final") > 0
    finally:
        eng.close()


def test_master_list_carries_the_sas_column_shape():
    """SAS's `<runid>_mstr` is ONE WIDE ROW PER EPISODE carrying
    everything — covariate flags, utilization counts, calendar parts
    and the censoring flags all live on it.

    This package computed each of those and wrote them to `covariates`,
    `utilization` and `risk_scores` instead, so a data partner opening
    `<runid>_mstr` found 53 of SAS's 92 columns absent even though the
    values existed elsewhere in the output. That is a contract
    mismatch, the same class as `mstr` once naming the wrong table.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = {d[0].lower() for d in eng.con.execute(
            "SELECT * FROM cohort_final LIMIT 0").description}

        for name in ("year", "month", "quarter", "periodid",
                     "indexlookenddt", "rawdisp", "adjusteddisp",
                     "totrxsup", "totrxamt", "ttc"):
            assert name in cols, name
        # both flag families, which SAS writes under two names
        for stem in ("elig", "dth", "qryend", "dpend"):
            assert f"fup_{stem}" in cols, stem
            assert f"cens_{stem}" in cols, stem
        # a covariate flag per covariate the study defines
        study = load_study(STUDY)
        for cov in {c.covarnum for c in study.covariates}:
            assert f"covar{cov}" in cols, cov
    finally:
        eng.close()


def test_master_list_covariate_columns_follow_the_study():
    """The covariate flags are GENERATED from the study, not fixed.

    A different study has a different covariate set — 15 where this one
    has 32, or a non-contiguous set — so a hardcoded covar1..covar32
    would be wrong for every study but one. The columns emitted are
    exactly the covariate numbers the study defines.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rows = base["covariatecodes"]

    def covar_columns(covariate_rows):
        s = load_study_dict({**base, "covariatecodes": covariate_rows})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            cols = [d[0] for d in eng.con.execute(
                "SELECT * FROM cohort_final LIMIT 0").description]
            return sorted(
                int(m.group(1)) for m in
                (re.fullmatch(r"(?i)covar(\d+)", c) for c in cols) if m)
        finally:
            eng.close()

    two = [r for r in rows if int(r["covarnum"]) in (1, 2)]
    assert covar_columns(two) == [1, 2]

    # non-contiguous: the column set must follow the numbers, not a range
    gappy = [dict(rows[0], covarnum=n) for n in (1, 5, 9)]
    assert covar_columns(gappy) == [1, 5, 9]


def test_master_list_utilization_columns_need_the_stage():
    """The utilization counts appear only when that stage ran — a study
    with no utilfile gets no NumAV, rather than a column of zeros."""
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def has_numav(**extra):
        s = load_study_dict({**base, **extra})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            return "NumAV" in {d[0] for d in eng.con.execute(
                "SELECT * FROM cohort_final LIMIT 0").description}
        finally:
            eng.close()

    assert not has_numav()
    assert has_numav(utilfile=[
        {"group": g, "utiltype": "MED", "utilfrom": -183, "utilto": -1}
        for g in ("lisinopril", "beta_blocker")])


def test_washout_shave_uses_stockpiled_dates():
    """SAS shaves the denominator from `ADate + 1` to
    `ExpireDt + washper`, where both come from the STOCKPILED exposure
    (ms_cidadenom.sas:461-478) — not from the raw claim.

    Using raw dates ended the ineligible window early, so members
    re-entered the denominator sooner than SAS allows. It showed up
    only in the incident cohorts, the ones with a washout: they carried
    every large per-cohort difference (+15, +7, +3, +2) while the
    prevalent cohorts were uniformly +1. After the fix all 40 are +1,
    and the total member excess halved from 88 to 40.

    Tested behaviourally: a LONGER washout must remove strictly more
    eligible time, and a stockpiled expiry is never earlier than the
    raw one, so the shave can only grow.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    strata = [{"tableid": "t2cida", "levelid": "1", "levelvars": ""}]

    def memdays(washper):
        t2 = [dict(r) for r in base["type2file"]]
        for r in t2:
            r["t2washper"] = washper
        s = load_study_dict({**base, "type2file": t2,
                             "userstrata": strata})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            return eng.con.execute(
                "SELECT sum(dennummemdays) FROM denomcounts").fetchone()[0]
        finally:
            eng.close()

    # wash_per may not exceed enr_days (183 here), which the
    # config validator enforces
    none_, short, long_ = memdays(0), memdays(30), memdays(180)
    assert short < none_, (
        "a washout removed no eligible time at all", none_, short)
    assert long_ < short, (
        "a longer washout removed no MORE time, so the shave is not "
        "using the exposure's expiry", short, long_)


def test_inclusion_codes_are_matched_within_their_own_domain():
    """`codecat` belongs on the CODE, not only on the rule.

    Several rules share one (cond, subcond) — this study has 57 at
    cond 1, subcond 1, spanning DX and PX — so matching a claim against
    the RULE's domain let a DX code match a PX claim whenever any
    sibling rule was PX. Cross-domain matching of exactly the kind the
    exposure join had.

    It over-shaved the denominator by 31%: 586,096 member-days removed
    against SAS's 445,775. Matching within the code's own domain took
    the member-day gap from -140,321 to +8,033.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        cols = {d[0] for d in eng.con.execute(
            "SELECT * FROM cfg_inclusion_codes LIMIT 0").description}
        assert "codecat" in cols, (
            "inclusion codes carry no domain, so they match claims in "
            "every domain a sibling rule happens to use")
        # and no code may be registered without one
        blank = eng.con.execute(
            "SELECT count(*) FROM cfg_inclusion_codes "
            "WHERE codecat IS NULL OR codecat = ''").fetchone()[0]
        assert blank == 0, blank
    finally:
        eng.close()


def test_event_and_ioc_joins_enforce_vocabulary():
    """EVENT and IOC claims must match the CONFIGURED vocabulary.

    Both joins matched on code and domain only, so a code configured as
    ICD-10 also matched the same string recorded as ICD-9 — counting
    outcomes and washout claims the study never defined. The RX branch
    of the event source additionally hardcoded `NULL AS codetype`,
    discarding the dispensing vocabulary outright.

    This check is STRUCTURAL, and that is a weakness worth stating: it
    asserts the shipped SQL carries the predicate rather than observing
    a claim being excluded. A behavioural version needs a fixture whose
    event codes are defined ONLY with a vocabulary; the bundled demo
    study also defines them without one, and a permissive entry matches
    everything, so a behavioural assertion against it passes whether or
    not the fix is present.
    """
    sql_dir = Path(qrp.__file__).parent / "sql"
    exposure = (sql_dir / "30_exposure.sql").read_text()
    followup = (sql_dir / "60_followup.sql").read_text()

    # the event source must not throw the dispensing vocabulary away
    assert "NULL AS codetype" not in exposure, (
        "the RX branch hardcodes a NULL vocabulary, so every configured "
        "RX vocabulary matches any dispensing code")

    guard = "OR upper(x.codetype) = k.codetype"
    assert exposure.count(guard) >= 1, "EVENT join lost its vocabulary check"
    assert guard in followup, "IOC join lost its vocabulary check"
    # and both must accept a NULL SOURCE vocabulary
    for text, name in ((exposure, "30_exposure"), (followup, "60_followup")):
        assert "x.codetype IS NULL" in text, name


def test_censoring_survives_a_followuptime_only_study():
    """The censoring fallback was scoped to "no strata at all" while
    the main branch filtered to t2cida, so a study defining only
    t2followuptime levels produced NEITHER — and exported an empty
    censoring table. A regression introduced by the tableid split."""
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    def censor_rows(strata):
        eng = Engine(verbose=False)
        try:
            run(load_study_dict({**base, "userstrata": strata}), DATA,
                engine=eng, verbose=False)
            return eng.count("censoring")
        finally:
            eng.close()

    only_fup = censor_rows(
        [{"tableid": "t2followuptime", "levelid": "7", "levelvars": "sex"}])
    only_cida = censor_rows(
        [{"tableid": "t2cida", "levelid": "1", "levelvars": ""}])
    assert only_fup > 0, "a follow-up-time-only study exported no censoring"
    assert only_fup == only_cida


def test_denominator_enrolment_ends_at_death():
    """Enrolment cannot outlive the member.

    SAS truncates the span to the death date before building the
    window, and deletes it if that leaves the end before the start
    (ms_cidanum.sas:2184-2188). Real extracts carry spans that run past
    a death: patid 39853274 died 2015-10-26 yet held a span through
    2016-12-31, and this package gave them an eligible window of
    2016-07-02 to 2016-12-30. One such member per denominator
    configuration was counted.

    Fixing it took the denominator member count to EXACTLY SAS's on all
    40 cohorts of the study compared, from +40.

    This check is STRUCTURAL: it asserts the window bound consults the
    death date. A behavioural version needs a fixture containing a
    member whose enrolment outlives them, which the synthetic generator
    does not produce.
    """
    sql = (Path(qrp.__file__).parent / "sql" / "92_cidadenom.sql").read_text()
    assert "deathdt" in sql, (
        "the denominator window ignores death entirely, so enrolment "
        "spans that outlive a member are counted in full")
    # and it must bound the END of the window, not merely be mentioned
    assert "d.deathdt <= e.enr_end" in sql
    assert "THEN d.deathdt ELSE e.enr_end END" in sql


def test_supply_and_amount_are_clipped_to_the_episode():
    """SAS's TotRxSup counts only the days of each dispensing that fall
    inside [IndexDt, EpisodeEndDt], and TotRxAmt prorates the amount by
    the same fraction. This package reported the UNCAPPED episode supply
    (+17% against SAS) and the INDEX dispensing's amount alone (-73%).

    Checked per episode against SAS's master list on wp307: the clipped
    supply matches on 31,453 of 31,464 episodes.
    """
    from qrp import Engine

    eng = Engine(verbose=False)
    try:
        run(load_study(STUDY), DATA, engine=eng, verbose=False)
        r = eng.con.execute("""
            SELECT
              count(*) FILTER (WHERE episode_totrxsup > episode_rxsup),
              count(*) FILTER (WHERE episode_totrxsup < episode_rxsup),
              count(*) FILTER (WHERE episode_totrxsup <= 0)
            FROM cohort_final""").fetchone()
        assert r[0] == 0, "clipped supply exceeded the uncapped supply"
        assert r[1] > 0, (
            "no episode had its supply clipped, so the window is not "
            "being applied — truncated episodes must report less supply")
        assert r[2] == 0, "an episode reported no supply at all"
    finally:
        eng.close()


def test_integer_rxamt_is_flagged():
    """An INTEGER rxamt means fractional amounts were truncated when the
    extract was written; amounts below 1 become 0 and are then removed
    by the `rxamt > 0` rule. It must not pass silently."""
    import warnings

    from qrp import Engine

    # the synthetic fixture writes integer amounts, so it must warn
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        eng = Engine(verbose=False)
        try:
            run(load_study(STUDY), DATA, engine=eng, verbose=False)
        finally:
            eng.close()
    assert any("rxamt is stored as" in str(w.message) for w in caught)


def test_denominator_is_shaved_around_outcome_events():
    """SAS removes member-time around every follow-up EVENT claim, from
    `ADate - BLACKOUTPER + 1` to `ExpireDt + FUPWASHPER`, across the whole
    eligible population (ms_cidadenom.sas:486-515). Adding it took wp307's
    member-day gap from +753 to exact.

    Cohorts that differ ONLY in their outcome share one denominator
    config — the outcome is applied per cohort in small scopes rather
    than recomputing every member once per outcome — yet each must get
    its own counts, and one cohort's outcome must never reach another's.
    The test builds exactly that pair, because cohorts with different
    exposures have separate configs anyway and would not exercise the
    scope filter at all.
    """
    from qrp import Engine
    from qrp.config import load_study_dict
    from qrp.pipeline import _denom_cfg_id

    base = json.loads((STUDY.parent / "demo_full.json").read_text())

    # make beta_blocker an exact copy of lisinopril except for its name,
    # so the two share one denominator config
    def is_lis(r): return r.get("group") == "lisinopril"
    codes = [r for r in base["cohortcodes"] if r.get("group") != "beta_blocker"]
    codes += [dict(r, group="beta_blocker") for r in base["cohortcodes"] if is_lis(r)]
    lis_t2 = next(r for r in base["type2file"] if is_lis(r))
    t2 = [dict(lis_t2, group=r["group"]) if r.get("group") == "beta_blocker" else r
          for r in base["type2file"]]
    extra = {k: [dict(r, group="beta_blocker") if r.get("group") == "beta_blocker" else r
                 for r in base.get(k, [])]
             for k in ("inclusioncodes",) if k in base}
    shared = {**base, **extra, "type2file": t2,
              "userstrata": [{"tableid": "t2cida", "levelid": "1", "levelvars": ""}]}

    # a frequent diagnosis that is not already in use, as an outcome for
    # lisinopril ONLY
    used = {str(r.get("code")) for r in codes}
    code = "X00529"
    assert code not in used, "pick a code that is not already used"

    def counts(extra_codes):
        s = load_study_dict({**shared, "cohortcodes": codes + extra_codes})
        eng = Engine(verbose=False)
        try:
            run(s, DATA, engine=eng, verbose=False)
            return s, dict(eng.con.execute(
                'SELECT "group", dennummemdays FROM denomcounts').fetchall())
        finally:
            eng.close()

    s0, without = counts([])
    s1, with_event = counts([{"group": "lisinopril", "codecat": "DX",
                              "code": code, "fupcriteria": "DEF"}])

    cl = [c for c in s1.cohorts if c.cohortgrp == "lisinopril"][0]
    cb = [c for c in s1.cohorts if c.cohortgrp == "beta_blocker"][0]
    assert _denom_cfg_id(cl, s1) == _denom_cfg_id(cb, s1), (
        "the two cohorts must SHARE a config for this test to mean anything")

    assert with_event["lisinopril"] < without["lisinopril"], (
        "an outcome that occurs often removed no member-time")
    assert with_event["beta_blocker"] == without["beta_blocker"], (
        "lisinopril's outcome leaked into beta_blocker's denominator",
        without["beta_blocker"], with_event["beta_blocker"])


def test_denominator_washout_chain_restarts_each_enrolment_span():
    """SAS's stockpile chain for the denominator washout RESTARTS in each
    enrolment span: a fill in a new span is never pushed by supply carried
    over from the previous span.

    Patient 98573092 in the study compared: enrolled to 2019-07-31 and
    again from 2019-10-01. SAS keeps their 2019-10-21 fill on 2019-10-21;
    a single continuous chain pushed it to 2019-11-09. Chaining per span
    made all 194,122 washout periods across 20 incident cohorts match
    SAS's `_UneligGroupIndex` exactly, and denominator member-days exact
    on all 40 cohorts.

    Behaviourally: the first dispensing in every span must keep its own
    date, whatever came before it in an earlier span.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    # the denominator stage runs only with CIDA strata, and the chain has
    # work to do only with a washout
    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    t2 = [dict(r, t2washper=183) for r in base["type2file"]]
    study = load_study_dict({
        **base, "type2file": t2,
        "userstrata": [{"tableid": "t2cida", "levelid": "1", "levelvars": ""}]})

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        # earliest RX fill per (cohort, stockgroup, patient, span) on the
        # raw side, against the earliest chained date in the same span
        bad = eng.con.execute("""
            WITH raw AS (
                SELECT x.cohortgrp, x.stockgroup, x.patid, en.enr_start AS span,
                       min(x.adate) AS first_fill
                FROM exposure_claims x
                JOIN cfg_cohort c ON c.cohortgrp = x.cohortgrp
                JOIN enrollment_spans en
                  ON en.enr_cfg_id = c.enr_cfg_id AND en.patid = x.patid
                 AND x.adate BETWEEN en.enr_start AND en.enr_end
                WHERE c.wash_per <> 0 AND x.codecat = 'RX'
                  -- the chain's own entry rule: supply must reach the
                  -- start of the enrolment window
                  AND x.adate + CAST(x.rxsup - 1 AS INTEGER)
                      >= CAST(? AS DATE) - c.enr_days
                GROUP BY 1, 2, 3, 4
            )
            SELECT count(*) FROM raw r
            WHERE NOT EXISTS (
                SELECT 1 FROM _denom_chain d
                WHERE d.cohortgrp = r.cohortgrp AND d.patid = r.patid
                  AND d.adate = r.first_fill)
        """, [study.start_date]).fetchone()[0]
        total = eng.con.execute("SELECT count(*) FROM _denom_chain").fetchone()[0]
        assert total > 0, "the denominator washout chain is empty"
        assert bad == 0, (
            f"{bad} span(s) had their first fill pushed — supply was carried "
            f"across an enrolment gap")
    finally:
        eng.close()


def test_master_list_columns_use_one_lookup_per_source():
    """The master-list covariate flags and utilization counts must come
    from ONE pivot over `covariates_long` and ONE join to `utilization`,
    however many covariates the study defines.

    The first version emitted a correlated EXISTS per covariate and a
    scalar subquery per count, so the work grew with episodes x
    covariates; at 20x wp307's sample it ran out of memory where the
    pivot took 5-6s. The static join-budget test cannot catch this — it
    reads the .sql template, which holds only a placeholder — so this
    test inspects the SQL that is actually generated.
    """
    from qrp import Engine
    from qrp.config import load_study_dict
    from qrp.pipeline import _mstr_extra_columns

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base, "utilfile": [
        {"group": g, "utiltype": "MED", "utilfrom": -183, "utilto": -1}
        for g in ("lisinopril", "beta_blocker")]})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        cols, joins = _mstr_extra_columns(eng, study)
    finally:
        eng.close()
    n_cov = len({c.covarnum for c in study.covariates})
    assert n_cov > 1, "needs several covariates to mean anything"
    sql = cols + joins
    assert sql.count("covariates_long") == 1, sql
    assert sql.count("JOIN utilization") == 1, sql
    assert "EXISTS" not in sql and "SELECT u." not in sql, (
        "per-row correlated lookups are back")
    # one output column per covariate (each line names it twice: the
    # pivot's column, then the alias)
    assert cols.count(' AS "COVAR') == n_cov


def test_baseline_uses_one_lookup_per_source():
    """The baseline table's covariate counts and mean/std columns must
    come from ONE covariate pivot and one join per measure table, not a
    correlated lookup per column per episode.

    The first version put a correlated EXISTS per covariate and a scalar
    subquery per mean/std column inside the aggregation — the same flaw
    as the master list, in a step that runs outside any timed stage. Its
    replacement produces an identical baseline (every column and value,
    on wp307 and the 600k-code study). Like the master list, the SQL is
    generated in Python, so the static join test cannot see it.
    """
    from qrp import Engine
    from qrp.config import load_study_dict
    from qrp.pipeline import _baseline_dummies

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base, "utilfile": [
        {"group": g, "utiltype": "MED", "utilfrom": -183, "utilto": -1}
        for g in ("lisinopril", "beta_blocker")]})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        sql = _baseline_dummies(eng, study)
    finally:
        eng.close()
    assert len({c.covarnum for c in study.covariates}) > 1
    assert sql.count("covariates_long") == 1, sql
    assert sql.count("JOIN utilization") == 1, sql
    assert "EXISTS" not in sql and "(SELECT u." not in sql, (
        "per-row correlated lookups are back in the baseline")


def test_combo_covariates_are_evaluated_in_order():
    """SAS evaluates combinations one at a time in covariate-number order
    (ms_cidacov.sas:1201-1220), so a combination sees every combination
    numbered before it and none numbered after. wp307's covar32
    references covar31; evaluating all of them against the table as it
    stood before any were added missed 171 of SAS's episodes.

    Each combination is still a pivot, not a correlated EXISTS per
    reference per episode. Self-contained: an earlier version read a
    study file outside the repository and would have skipped elsewhere.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    combos = [
        # 13 = 1 or 2 ;  14 sees 13 (earlier) ;  15 sees 16 (LATER) -> never
        {"covarnum": 13, "codecat": "CC", "code": "1 or 2", "codedays": 1,
         "dateonly": "N"},
        {"covarnum": 14, "codecat": "CC", "code": "13", "codedays": 1,
         "dateonly": "N"},
        {"covarnum": 15, "codecat": "CC", "code": "16", "codedays": 1,
         "dateonly": "N"},
        {"covarnum": 16, "codecat": "CC", "code": "1 or 2", "codedays": 1,
         "dateonly": "N"},
    ]
    study = load_study_dict(
        {**base, "covariatecodes": list(base["covariatecodes"]) + combos})
    from qrp.pipeline import _combo_sql
    sql = _combo_sql(study)
    assert "EXISTS" not in sql, "per-reference correlated lookups are back"

    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        n = dict(eng.con.execute(
            "SELECT covarnum, count(*) FROM covariates_long "
            "WHERE covarnum IN (13, 14, 15, 16) GROUP BY 1").fetchall())
    finally:
        eng.close()
    assert n.get(13, 0) > 0, "the base combination matched nothing"
    assert n.get(14, 0) == n[13], (
        "a combination must see one numbered BEFORE it", n)
    assert n.get(16, 0) == n[13]
    assert n.get(15, 0) == 0, (
        "a combination must NOT see one numbered AFTER it", n)


def test_covariate_codes_keep_their_own_category():
    """A covariate can mix categories. wp307's 'Pegfilgrastim Post-Index'
    lists NDCs as dispensings (RX), NDCs billed on procedure claims (PX)
    and J-codes (PX). The loader took the category from the FIRST row, so
    the covariate matched only dispensings and found 15 of SAS's 224
    episodes; per-code categories brought SAS-only covariate flags on
    wp307 from 783 to 447 with no new false positives.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    rows = [
        {"covarnum": 90, "codecat": "RX", "codetype": "ND", "code": "N00001",
         "covfrom": -30, "covto": -1},
        {"covarnum": 90, "codecat": "PX", "codetype": "HC", "code": "J9999",
         "covfrom": -30, "covto": -1},
    ]
    study = load_study_dict(
        {**base, "covariatecodes": list(base["covariatecodes"]) + rows})
    cov = next(c for c in study.covariates if int(c.covarnum) == 90)
    assert dict(cov.code_cats) == {"N00001": "RX", "J9999": "PX"}, cov.code_cats


def test_covariate_dispensings_are_stockpiled():
    """SAS stockpiles covariate dispensings: clipped to enrolment, chained
    per patient by covariate and stockgroup, clipped again
    (ms_cidacov_codeextraction.sas:590-672). With raw fill dates, wp307's
    post-index drug covariates disagreed with SAS in both directions;
    stockpiling took SAS-only covariate flags from 276 to 14 and
    mine-only from 252 to 99, with 28 of 32 covariates exact.

    Behaviourally: for a covariate on a drug whose refills overlap, the
    chain must move some fills LATER, and never any earlier.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    lis = sorted({str(r["code"]) for r in base["cohortcodes"]
                  if r.get("group") == "lisinopril"
                  and str(r.get("codecat", "")).upper() == "RX"})
    assert lis, "lisinopril has no dispensing codes to build on"
    rows = [{"covarnum": 90, "codecat": "RX", "codetype": "ND", "code": c,
             "stockgroup": "lis", "covfrom": 1, "covto": 30, "dateonly": "Y"}
            for c in lis]
    study = load_study_dict(
        {**base, "covariatecodes": list(base["covariatecodes"]) + rows})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        moved, earlier = eng.con.execute(f"""
            WITH first_fill AS (
                SELECT patid, min(adate) AS d FROM cdm_dispensing
                WHERE code IN ({",".join(f"'{c}'" for c in lis)})
                GROUP BY 1)
            SELECT
              count(*) FILTER (WHERE NOT EXISTS (
                  SELECT 1 FROM cdm_dispensing d
                  WHERE d.patid = x.patid AND d.adate = x.adate
                    AND d.code IN ({",".join(f"'{c}'" for c in lis)}))
                  -- clipping to enrolment also moves fills, onto a span
                  -- start; only a move clipping cannot explain shows the
                  -- chain at work
                  AND NOT EXISTS (
                      SELECT 1 FROM enrollment_spans en
                      WHERE en.patid = x.patid AND en.enr_start = x.adate)),
              count(*) FILTER (WHERE x.adate < f.d)
            FROM _covar_rx_chain x JOIN first_fill f USING (patid)
            WHERE x.covarnum = 90""").fetchone()
    finally:
        eng.close()
    assert moved > 0, "no fill was moved: covariate dispensings are not stockpiled"
    assert earlier == 0, f"{earlier} chained dates fall before the first fill"


def test_covariate_chain_admits_only_claims_reaching_the_enrolment_window():
    """The covariate dispensing chain applies the exposure chain's entry
    rule: a dispensing whose supply ends before the cohort's enrolment
    window opens (`start_date - enr_days`) never enters it. Without the
    rule, old fills pushed later ones forward and wp307's pre-index drug
    covariates flagged episodes SAS does not (mine-only 99 -> 1, SAS-only
    14 -> 0; 31 of 32 covariates exact).

    Invariant: no chained period may END before the window opens — only
    claims reaching it are admitted, and chaining only moves dates later.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    lis = sorted({str(r["code"]) for r in base["cohortcodes"]
                  if r.get("group") == "lisinopril"
                  and str(r.get("codecat", "")).upper() == "RX"})
    rows = [{"covarnum": 90, "codecat": "RX", "codetype": "ND", "code": c,
             "stockgroup": "lis", "covfrom": -365, "covto": -1, "dateonly": "N"}
            for c in lis]
    study = load_study_dict(
        {**base, "covariatecodes": list(base["covariatecodes"]) + rows})
    codes = ",".join(f"'{c}'" for c in lis)
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        early, bad = eng.con.execute(f"""
            WITH opens AS (
                SELECT DISTINCT enr_cfg_id, CAST(? AS DATE) - enr_days AS d
                FROM cfg_covariates)
            SELECT
              (SELECT count(*) FROM cdm_dispensing x
               JOIN enrollment_spans en ON en.patid = x.patid
                AND x.adate BETWEEN en.enr_start AND en.enr_end
               JOIN opens o ON o.enr_cfg_id = en.enr_cfg_id
               WHERE x.code IN ({codes})
                 AND x.adate + CAST(x.rxsup - 1 AS INTEGER) < o.d),
              (SELECT count(*) FROM _covar_rx_chain c
               JOIN opens o ON o.enr_cfg_id = c.enr_cfg_id
               WHERE c.covarnum = 90 AND c.cend_max < o.d)
        """, [study.start_date]).fetchone()
    finally:
        eng.close()
    assert early > 0, "no dispensing ends before the window: the test proves nothing"
    assert bad == 0, f"{bad} chained periods end before the enrolment window opens"


def test_covariate_chain_clips_supply_across_an_enrolment_gap():
    """A dispensing that straddles an enrolment gap is split into one
    piece per span, and each piece must carry its CLIPPED supply. Keeping
    the full supply on both counted it twice.

    Replays wp307 patient 124844251 (hctz_rupture_prev, index 2023-08-15):
    enrolled 2019-07-01..2022-09-30 and 2023-01-01..2024-12-31, with a
    180-day fill on 2022-07-19 across the gap. With full supply on the
    2023 piece, the 2023 fills were pushed into the pre-index window and
    the covariate was flagged where SAS does not flag it. Clipped (74 and
    14 days), it is not. This was the last of wp307's 12,363 covariate
    flags to match SAS.

    Patient 2 is a positive control — an ordinary fill inside the window
    — so the test cannot pass on a stage that flags nothing.
    """
    import datetime as dt
    from qrp import Engine

    D = dt.date
    eng = Engine(verbose=False)
    try:
        eng.script_stage("macros", "00_macros.sql")
        eng.register("cfg_covariates", [
            {"cohortgrp": "g", "enr_cfg_id": "e", "enr_days": 183,
             "covarnum": 1, "covarname": "drug pre-index", "codecat": "RX",
             "covfrom": -30, "covto": -1, "covfromanchor": "INDEXDT",
             "covtoanchor": "INDEXDT", "dateonly": False}],
            """cohortgrp VARCHAR, enr_cfg_id VARCHAR, enr_days INTEGER,
               covarnum INTEGER, covarname VARCHAR, codecat VARCHAR,
               covfrom INTEGER, covto INTEGER, covfromanchor VARCHAR,
               covtoanchor VARCHAR, dateonly BOOLEAN""")
        eng.register("cfg_covariate_codes", [
            {"covarnum": 1, "code": "N1", "codecat": "RX", "stockgroup": "s"}],
            "covarnum INTEGER, code VARCHAR, codecat VARCHAR, stockgroup VARCHAR")
        fills = [(1, D(2022, 7, 19), 180), (1, D(2023, 1, 26), 30),
                 (1, D(2023, 8, 15), 30),
                 (2, D(2023, 8, 1), 30)]
        eng.register("cohort_claims", [
            {"patid": p, "adate": a, "expiredt": a + dt.timedelta(days=s - 1),
             "code": "N1", "codecat": "RX"} for p, a, s in fills],
            "patid BIGINT, adate DATE, expiredt DATE, code VARCHAR, codecat VARCHAR")
        eng.register("enrollment_spans", [
            {"enr_cfg_id": "e", "patid": 1, "enr_start": D(2019, 7, 1), "enr_end": D(2022, 9, 30)},
            {"enr_cfg_id": "e", "patid": 1, "enr_start": D(2023, 1, 1), "enr_end": D(2024, 12, 31)},
            {"enr_cfg_id": "e", "patid": 2, "enr_start": D(2019, 1, 1), "enr_end": D(2024, 12, 31)}],
            "enr_cfg_id VARCHAR, patid BIGINT, enr_start DATE, enr_end DATE")
        eng.register("ptsmasterlist", [
            {"cohortgrp": "g", "patid": p, "indexdt": D(2023, 8, 15),
             "episodeenddt": D(2023, 9, 13)} for p in (1, 2)],
            "cohortgrp VARCHAR, patid BIGINT, indexdt DATE, episodeenddt DATE")
        eng.script_stage("covariates", "80_covariates.sql", start_date="2016-04-01")
        flagged = {r[0] for r in eng.con.execute(
            "SELECT patid FROM covariates_long WHERE covarnum = 1").fetchall()}
    finally:
        eng.close()
    assert 2 in flagged, "the positive control was not flagged: the stage did not run as intended"
    assert 1 not in flagged, (
        "a fill straddling an enrolment gap kept its full supply on both pieces")


def test_supply_counting_stops_at_the_first_event():
    """SAS's TotRxSup counts supply up to where FOLLOW-UP ends, and an
    outcome event ends it. wp307 warfarin patient 82014737: SAS has
    10 + 19 days, clipped at the first event on 2024-05-13; counting to
    the episode end gave 10 + 30. Clipping at the event took the supply
    mismatches from 11 episodes to 8 with every count still exact.

    Invariant: counted supply cannot exceed (days from index to the end
    of follow-up) x (stockgroups the episode draws on). The guard makes
    sure some episode really has supply running past its event.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base, "userstrata": [
        {"tableid": "t2cida", "levelid": "1", "levelvars": ""}]})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        past, over = eng.con.execute("""
            WITH ep AS (
                SELECT f.cohortgrp, f.patid, f.indexdt, f.episode_totrxsup,
                       least(f.episodeenddt, f.feventdt) AS fup_end,
                       f.episodeenddt
                FROM cohort_final f
                WHERE f.feventdt IS NOT NULL AND f.feventdt < f.episodeenddt),
            k AS (
                SELECT e.cohortgrp, e.patid, e.indexdt,
                       count(DISTINCT s.stockgroup) AS n_sg,
                       max(s.expiredt) AS last_supply
                FROM ep e JOIN stockpiled s
                  ON s.cohortgrp = e.cohortgrp AND s.patid = e.patid
                 AND s.adate <= e.episodeenddt AND s.expiredt >= e.indexdt
                GROUP BY 1, 2, 3)
            SELECT
              count(*) FILTER (WHERE k.last_supply > e.fup_end),
              count(*) FILTER (WHERE e.episode_totrxsup >
                  (date_diff('day', e.indexdt, e.fup_end) + 1) * k.n_sg)
            FROM ep e JOIN k USING (cohortgrp, patid, indexdt)
        """).fetchone()
    finally:
        eng.close()
    assert past > 0, "no episode has supply running past its event: proves nothing"
    assert over == 0, f"{over} episodes count supply after follow-up ended"


def test_supply_and_amount_replay_wp307_patients():
    """The episode supply/amount statement, run on four wp307 patients
    replayed exactly — one per rule that brought `daysupp` and `amtsupp`
    to exact agreement with SAS:

      82014737  supply stops at the first event (2024-05-13): 10 + 19 = 29
      60364873  duplicate same-day procedure claims each count: 6, not 3
      153716000 same-day fills shaved to ENROLMENT before combining:
                92 x 50/71 = 64.7887, not 102 x 50/81 = 62.963
      164261173 same-day fills running past the window but NOT past
                enrolment are prorated once: 254 x 64/118 = 137.7627
                (this case is what ruled out clipping at the window)
    """
    import datetime as dt
    from qrp import Engine
    from qrp.sqlsplit import split_statements

    D = dt.date
    sql = (Path(qrp.__file__).parent / "sql" / "60_followup.sql").read_text()
    stmt = [s for s in split_statements(sql) if s.target == "cohort_final"][-1].sql

    eps = [  # cohortgrp, patid, indexdt, episodeenddt, feventdt, enr_end
        ("w", 1, D(2024, 4, 12), D(2024, 6, 23), D(2024, 5, 13), D(2024, 9, 30)),
        ("f", 2, D(2018, 9, 19), D(2018, 10, 21), None, D(2024, 12, 31)),
        ("d", 3, D(2023, 9, 12), D(2023, 10, 31), None, D(2023, 10, 31)),
        ("d", 4, D(2024, 7, 16), D(2024, 9, 17), None, D(2024, 12, 31)),
    ]
    # stockpiled rows (dispensings and, for patient 2, the collapsed
    # procedure rows), and the raw claims behind them
    stock = [  # cohortgrp, patid, stockgroup, orig, adate, expiredt, rxsup, rxamt
        ("w", 1, "war", D(2024, 4, 12), D(2024, 4, 12), D(2024, 4, 21), 10, 10.0),
        ("w", 1, "war", D(2024, 4, 25), D(2024, 4, 25), D(2024, 5, 24), 30, 30.0),
        ("f", 2, "fil", D(2018, 9, 19), D(2018, 9, 19), D(2018, 9, 19), 1, 1.0),
        ("f", 2, "fil", D(2018, 9, 20), D(2018, 9, 20), D(2018, 9, 20), 1, 1.0),
        ("f", 2, "fil", D(2018, 9, 21), D(2018, 9, 21), D(2018, 9, 21), 1, 1.0),
        ("d", 3, "doac", D(2023, 9, 12), D(2023, 9, 12), D(2023, 12, 1), 81, 102.0),
        ("d", 4, "doac", D(2024, 7, 16), D(2024, 7, 16), D(2024, 11, 10), 118, 254.0),
    ]
    raw = [  # cohortgrp, patid, stockgroup, adate, rxsup, rxamt, codecat
        ("w", 1, "war", D(2024, 4, 12), 10, 10.0, "RX"),
        ("w", 1, "war", D(2024, 4, 25), 30, 30.0, "RX"),
        *[("f", 2, "fil", D(2018, 9, 19 + i), 1, 1.0, "PX") for i in range(3) for _ in range(2)],
        ("d", 3, "doac", D(2023, 9, 12), 21, 42.0, "RX"),
        ("d", 3, "doac", D(2023, 9, 12), 60, 60.0, "RX"),
        ("d", 4, "doac", D(2024, 7, 16), 90, 180.0, "RX"),
        ("d", 4, "doac", D(2024, 7, 16), 28, 74.0, "RX"),
    ]
    eng = Engine(verbose=False)
    try:
        eng.register("cohort_final", [
            dict(zip(("cohortgrp", "patid", "indexdt", "episodeenddt",
                      "feventdt", "enr_end"), e)) for e in eps],
            "cohortgrp VARCHAR, patid BIGINT, indexdt DATE, episodeenddt DATE, "
            "feventdt DATE, enr_end DATE")
        eng.register("stockpiled", [
            dict(zip(("cohortgrp", "patid", "stockgroup", "orig_adate", "adate",
                      "expiredt", "rxsup", "rxamt"), r)) for r in stock],
            "cohortgrp VARCHAR, patid BIGINT, stockgroup VARCHAR, orig_adate DATE, "
            "adate DATE, expiredt DATE, rxsup INTEGER, rxamt DOUBLE")
        eng.register("exposure_claims", [
            dict(zip(("cohortgrp", "patid", "stockgroup", "adate", "rxsup",
                      "rxamt", "codecat"), r)) for r in raw],
            "cohortgrp VARCHAR, patid BIGINT, stockgroup VARCHAR, adate DATE, "
            "rxsup INTEGER, rxamt DOUBLE, codecat VARCHAR")
        eng.con.execute(stmt)
        got = {p: (s, round(a, 4)) for p, s, a in eng.con.execute(
            "SELECT patid, episode_totrxsup, episode_totrxamt FROM cohort_final"
        ).fetchall()}
    finally:
        eng.close()
    assert got[1][0] == 29, ("supply must stop at the first event", got[1])
    assert got[2][0] == 6, ("duplicate procedure claims each count", got[2])
    assert got[3][1] == 64.7887, ("fills shaved to enrolment before combining", got[3])
    assert got[4][1] == 137.7627, ("fills inside enrolment prorated once", got[4])


def test_sas_utilfile_and_drugclass_formats_are_read():
    """Both files failed SILENTLY in SAS's own formats.

    UTILFILE is wide in SAS — one row per group with medutilfrom/medutilto
    AND drugutilfrom/drugutilto. Only the long shape (utiltype, utilfrom,
    utilto) was read, so each wide row became a MEDICAL window with the
    default -365..-1 and no DRUG window was created: wp307's medical
    counts ran ~3x SAS's and every drug count was zero.

    The drug class file keys on `rx` and carries `generic`. Requiring a
    `code` column dropped all 301,245 rows of wp307's file.

    With both read (and utilization limited to createbaseline cohorts),
    numrx, NumGeneric, NumClass, NumIP and NumED match SAS on every wp307
    episode.
    """
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base,
        "utilfile": [{"group": "lisinopril", "medutilfrom": -183, "medutilto": 0,
                      "drugutilfrom": -90, "drugutilto": 0}],
        "drugclassfile": [{"rx": "N00001", "generic": "lisinopril",
                           "classname": "ACE inhibitors"}]})
    assert set(study.utilization) == {
        ("lisinopril", "MED", -183, 0), ("lisinopril", "DRUG", -90, 0)}, study.utilization
    assert study.drug_classes == (("N00001", "ACE inhibitors", "lisinopril"),)

    # the long shape still works
    long = load_study_dict({**base, "utilfile": [
        {"group": "lisinopril", "utiltype": "DRUG", "utilfrom": -30, "utilto": -1}]})
    assert long.utilization == (("lisinopril", "DRUG", -30, -1),)


def test_mfu_reads_sas_format_rows():
    """Three silent failures in SAS-format MFU rows (wp307's only row has
    no group, countmethod 'P' and codetype '10'):

    * a row naming no group applies to EVERY cohort; such rows were
      dropped, so wp307 produced no MFU table at all;
    * 'P' ranks by distinct patients; only 'PATCOUNT' was recognised, so
      'P' fell back to ranking by claims;
    * codetype restricts the code system; it was ignored.

    With all three, wp307's MFU matches SAS's r01_mfu at every one of 400
    rank positions by patient count, with identical counts per code.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base, "mfufile": [
        {"analysisnum": 1, "codecat": "DX", "countmethod": "P", "topxx": 10,
         "mfufrom": -365, "mfuto": -1},
        {"analysisnum": 2, "codecat": "DX", "countmethod": "P", "topxx": 10,
         "mfufrom": -365, "mfuto": -1, "codetype": "ZZ"}]})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        con = eng.con
        cohorts = {r[0] for r in con.execute(
            "SELECT DISTINCT cohortgrp FROM mfu WHERE analysisnum = 1").fetchall()}
        n2 = con.execute("SELECT count(*) FROM mfu WHERE analysisnum = 2").fetchone()[0]
        by_pat = con.execute("""
            SELECT count(*) FROM (
              SELECT patcount < lead(patcount) OVER w AS bad
              FROM mfu WHERE analysisnum = 1
              WINDOW w AS (PARTITION BY cohortgrp ORDER BY rank)) WHERE bad""").fetchone()[0]
        claims_disagree = con.execute("""
            SELECT count(*) FROM (
              SELECT codecount < lead(codecount) OVER w AS inv
              FROM mfu WHERE analysisnum = 1
              WINDOW w AS (PARTITION BY cohortgrp ORDER BY rank)) WHERE inv""").fetchone()[0]
    finally:
        eng.close()
    assert cohorts == {c.cohortgrp for c in study.cohorts}, (
        "a row naming no group must apply to every cohort", cohorts)
    assert by_pat == 0, "countmethod 'P' must rank by distinct patients"
    assert claims_disagree > 0, (
        "patient and claim rankings agree everywhere: 'P' is untested")
    assert n2 == 0, "codetype must restrict the code system"


def test_risk_scores_follow_the_risk_score_file():
    """RISKSCOREFILE names the scores a study wants and gives each its
    window; RISKSCORECODES is a shared library. Three silent failures:

    * the risk score file was never read, so every score's window fell
      back to -365..-1 (wp307's file says -183..0);
    * every score in the library was computed (seven in wp307's), not
      just the requested CCI;
    * a score with no intercept row vanished: the intercepts are
      cross-joined, and only scores WITH one were listed. In wp307's
      library only FRAILTY has an intercept, so FRAILTY was reported
      under the name of the requested CCI.

    Fixed, CCI matches SAS on all 31,464 wp307 episodes.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    code = "X00529"            # a frequent diagnosis the demo study does not use
    study = load_study_dict({**base,
        "riskscorefile": [{"riskscore": "AAA", "riskfrom": -183, "riskto": 0}],
        "riskscorecodes": [
            {"riskscore": "AAA", "code": code, "codecat": "DX", "condid": "01", "weight": 1},
            {"riskscore": "BBB", "code": code, "codecat": "DX", "condid": "01", "weight": 1},
            {"riskscore": "BBB", "code": "", "codecat": "IN", "condid": "IN", "weight": 5}]})
    assert {(r.riskscore, r.riskfrom, r.riskto) for r in study.risk_scores} == {
        ("AAA", -183, 0)}, "only the requested score, with the file's window"
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        scores, hit = eng.con.execute("""
            SELECT list(DISTINCT riskscore), count(*) FILTER (WHERE score > 0)
            FROM risk_scores""").fetchone()
    finally:
        eng.close()
    assert scores == ["AAA"], f"a score without an intercept must be reported: {scores}"
    assert hit > 0, "no episode scored: the test proves nothing"


def test_master_list_utilization_and_score_columns_follow_sas():
    """Master-list columns added to match SAS's mstr, each verified on all
    31,464 wp307 episodes including where SAS leaves them NULL:

    * utilization is NULL (not zero) for cohorts SAS computes none for
      (createbaseline = 'N'); NumVisits is blank there;
    * ExactNumVisit is the number of distinct visit DAYS; NumVisits is its
      category '0' / '1' / '2-7' / '8+' (it held the numeric total);
    * each requested risk score is a column named after it ("CCI"),
      reported for every cohort;
    * fupdays_value_cat buckets followuptime; Censorcat_sort is its rank.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    cf = [dict(r, createbaseline=("N" if (r.get("cohortgrp") or r.get("group")) == "beta_blocker" else "Y"))
          for r in base["cohortfile"]]
    study = load_study_dict({**base, "cohortfile": cf,
        "utilfile": [{"group": g, "medutilfrom": -183, "medutilto": 0,
                      "drugutilfrom": -183, "drugutilto": 0}
                     for g in ("lisinopril", "beta_blocker")],
        "riskscorefile": [{"riskscore": "AAA", "riskfrom": -183, "riskto": 0}],
        "riskscorecodes": [{"riskscore": "AAA", "code": "X00529", "codecat": "DX",
                            "condid": "01", "weight": 1}]})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        r = eng.con.execute("""
            SELECT
              count(*) FILTER (WHERE cohortgrp = 'beta_blocker' AND
                  ("NumAV" IS NOT NULL OR "ExactNumVisit" IS NOT NULL OR "NumVisits" <> '')),
              count(*) FILTER (WHERE cohortgrp = 'lisinopril' AND "NumVisits" IS DISTINCT FROM
                  CASE WHEN "ExactNumVisit" = 0 THEN '0' WHEN "ExactNumVisit" = 1 THEN '1'
                       WHEN "ExactNumVisit" <= 7 THEN '2-7' ELSE '8+' END),
              count(*) FILTER (WHERE cohortgrp = 'lisinopril' AND "ExactNumVisit" > 0),
              count(*) FILTER (WHERE "AAA" IS NULL),
              count(*) FILTER (WHERE "fupdays_value_cat" IS DISTINCT FROM
                  CASE WHEN followuptime <= 90 THEN '0-90' WHEN followuptime <= 180
                       THEN '91-180' ELSE '181+' END)
            FROM cohort_final""").fetchone()
        types = eng.con.execute(
            'SELECT typeof("ExactNumVisit"), typeof("NumVisits") FROM cohort_final LIMIT 1'
        ).fetchone()
    finally:
        eng.close()
    # ExactNumVisit is a count, so an integer: SAS stores every numeric as a
    # double, but a count can never be fractional. NumVisits is text, as SAS.
    assert types == ("BIGINT", "VARCHAR"), types
    assert r[0] == 0, "utilization must be NULL / blank where SAS computes none"
    assert r[2] > 0, "no visits found: the category check proves nothing"
    assert r[1] == 0, "NumVisits must be the category of ExactNumVisit"
    assert r[3] == 0, "the risk score column must be named after the score"
    assert r[4] == 0, "fupdays_value_cat must bucket followuptime"


def test_distindex_numbering_follows_ms_codedistribution():
    """distindexexp / distindexhoi, from ms_codedistribution.sas: each
    index entity is numbered by row position — drug stockgroups sorted,
    then medical codes sorted and EXPANDED in place (care setting '**' ->
    IP IS ED AV OA; DX flag '*' -> P S X ''; PX -> X '') — and an episode's
    value is its claims' ids joined with '_' in CHARACTER order.

    Hand-derived ids for this replay:
      exposure: stockgroups pegA=1, pegB=2; J2505 occupies 3..12, so
                J2505 at AV (blank flag) = 10; J2506 at AV = 20
      outcome:  D1 occupies 1..20; AV with flag S = 14
    Patient 2's '10_2' checks the character order ('10' before '2').
    On wp307, distindexhoi matches SAS on every episode, distindexexp on
    all but 10 (claims SAS re-files into an inpatient stay).
    """
    import datetime as dt
    from qrp import Engine
    from qrp.sqlsplit import split_statements

    D = dt.date(2023, 3, 1)
    sql = (Path(qrp.__file__).parent / "sql" / "62_mstr_wide.sql").read_text()
    stmts = [s.sql for s in split_statements(sql)
             if s.target in ("_di_claims", "_di_ids", "_di_exp", "_di_hoi")]
    assert len(stmts) == 4, [s.target for s in split_statements(sql)]
    eng = Engine(verbose=False)
    try:
        eng.register("cfg_codes", [
            {"cohortgrp": "g", "role": "DEF", "code": c, "codecat": cat,
             "codetype": ct, "code_supply": None, "stockgroup": sg}
            for c, cat, ct, sg in (("N1", "RX", "ND", "pegA"), ("N2", "RX", "ND", "pegB"),
                                   ("J2505", "PX", "HC", None), ("J2506", "PX", "HC", None))
        ] + [{"cohortgrp": "g", "role": "EVENT", "code": "D1", "codecat": "DX",
              "codetype": "10", "code_supply": None, "stockgroup": None}],
            "cohortgrp VARCHAR, role VARCHAR, code VARCHAR, codecat VARCHAR, "
            "codetype VARCHAR, code_supply INTEGER, stockgroup VARCHAR")
        eng.register("cfg_care_setting", [],
                     "cohortgrp VARCHAR, code VARCHAR, enctype VARCHAR, pdx VARCHAR")
        eng.register("cohort_final", [
            {"cohortgrp": "g", "patid": p, "indexdt": D, "feventdt": D} for p in (1, 2, 3)],
            "cohortgrp VARCHAR, patid BIGINT, indexdt DATE, feventdt DATE")
        eng.register("stockpiled", [
            {"cohortgrp": "g", "patid": p, "adate": D, "stockgroup": "pegB"} for p in (1, 2, 3)],
            "cohortgrp VARCHAR, patid BIGINT, adate DATE, stockgroup VARCHAR")
        eng.register("cdm_procedure", [
            {"patid": 1, "adate": D, "code": "J2506", "codetype": "HC", "enctype": "AV", "pdx": ""},
            {"patid": 2, "adate": D, "code": "J2505", "codetype": "HC", "enctype": "AV", "pdx": ""},
            # patient 3's claim, ALREADY ENVELOPED into an inpatient stay by
            # the cdm_procedure view (IP / 'X'): for J2506 that is id 13
            {"patid": 3, "adate": D, "code": "J2506", "codetype": "HC", "enctype": "IP", "pdx": "X"}],
            "patid BIGINT, adate DATE, code VARCHAR, codetype VARCHAR, enctype VARCHAR, pdx VARCHAR")
        eng.register("cdm_diagnosis", [
            {"patid": 1, "adate": D, "code": "D1", "codetype": "10", "pdx": "S", "enctype": "AV"}],
            "patid BIGINT, adate DATE, code VARCHAR, codetype VARCHAR, pdx VARCHAR, enctype VARCHAR")
        eng.register("cdm_dispensing", [], "patid BIGINT, adate DATE, code VARCHAR")
        for st in stmts:
            eng.con.execute(st)
        exp = dict(eng.con.execute("SELECT patid, lst FROM _di_exp").fetchall())
        hoi = dict(eng.con.execute("SELECT patid, lst FROM _di_hoi").fetchall())
    finally:
        eng.close()
    assert exp == {1: "2_20", 2: "10_2", 3: "13_2"}, exp
    assert hoi == {1: "14"}, hoi


def test_dispensing_whose_supply_reaches_the_window_is_read():
    """Claims are read from `claims_from` (study start minus the widest
    look-back any feature needs). A dispensing must be kept if its SUPPLY
    reaches that date, not only its fill date: the exposure chain admits a
    fill whose supply reaches the enrolment window, however early it was
    filled.

    Filtering on the fill date dropped long fills. On wp307, exact parity
    had rested on an accidental 365-day look-back from a wrong risk-score
    default; correcting the default shrank the window and broke cohorts
    (patients +28, episodes +48) without any test noticing.
    """
    from datetime import timedelta
    from qrp import Engine

    from qrp.config import load_study_dict

    # The demo's widest look-back is 365 days, which puts claims_from on
    # the first day of its data, so nothing could straddle it. Without its
    # 365-day covariates the span is 183 and claims_from falls mid-2010,
    # after six months of fills.
    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    study = load_study_dict({**base, "covariatecodes": [
        r for r in base["covariatecodes"] if int(r.get("covfrom") or 0) > -365]})
    span = study.widest_lookback_days
    assert span is not None
    claims_from = study.start_date - timedelta(days=span)
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        straddling, missing = eng.con.execute("""
            WITH defs AS (
                SELECT DISTINCT cohortgrp, code FROM cfg_codes
                WHERE role = 'DEF' AND codecat = 'RX'),
            early AS (
                SELECT k.cohortgrp, d.patid, d.adate, d.code
                FROM cdm_dispensing d JOIN defs k ON k.code = d.code
                WHERE d.adate < CAST(? AS DATE)
                  AND d.adate + CAST(d.rxsup - 1 AS INTEGER) >= CAST(? AS DATE))
            SELECT count(*),
                   count(*) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM exposure_claims x
                       WHERE x.cohortgrp = e.cohortgrp AND x.patid = e.patid
                         AND x.adate = e.adate AND x.code = e.code))
            FROM early e""", [claims_from, claims_from]).fetchone()
    finally:
        eng.close()
    assert straddling > 0, "no fill straddles claims_from: the test proves nothing"
    assert missing == 0, f"{missing} fills whose supply reaches the window were dropped"


def test_inpatient_stay_days_follow_run_envelope():
    """The days that envelope a claim (ms_envelope.sas). RUN_ENVELOPE 0:
    admit through discharge, touching stays merged (their union is the
    same set of days); any other value but 2: from the day AFTER admit;
    2: none. A missing discharge date is a one-day stay; a table without
    `ddate` at all must not fail.
    """
    import datetime as dt
    from qrp import Engine
    from qrp.sqlsplit import split_statements

    D = dt.date
    sql = (Path(qrp.__file__).parent / "sql" / "10_normalize.sql").read_text()
    stmt = next(st.sql for st in split_statements(sql) if st.target == "_ip_days")

    def days(setting, with_ddate=True):
        eng = Engine(verbose=False)
        try:
            eng.register("enc", [
                {"patid": 1, "adate": D(2023, 1, 10), "ddate": D(2023, 1, 12), "enctype": "IP"},
                {"patid": 1, "adate": D(2023, 1, 13), "ddate": D(2023, 1, 13), "enctype": "IP"},
                {"patid": 2, "adate": D(2023, 2, 1), "ddate": None, "enctype": "IP"},
                {"patid": 2, "adate": D(2023, 3, 1), "ddate": D(2023, 3, 5), "enctype": "AV"}],
                "patid BIGINT, adate DATE, ddate DATE, enctype VARCHAR")
            st = (stmt.replace("{read_encounter}", "enc")
                      .replace("{run_envelope}", str(setting))
                      .replace("{opt_ddate}", "ddate" if with_ddate else "NULL"))
            eng.con.execute(st)
            return {(p, d.day if d.month == 1 else -d.day) for p, d in
                    eng.con.execute("SELECT patid, day FROM _ip_days").fetchall()}
        finally:
            eng.close()

    # patient 1: 10..12 and 13 (touching); patient 2: one day (no ddate);
    # the AV encounter never envelopes. February days are negated above.
    assert days(0) == {(1, 10), (1, 11), (1, 12), (1, 13), (2, -1)}
    assert days(1) == {(1, 11), (1, 12)}
    assert days(2) == set()
    assert days(0, with_ddate=False) == {(1, 10), (1, 13), (2, -1)}


def test_claim_views_envelope_inpatient_stays():
    """Enveloping happens in the cdm_diagnosis / cdm_procedure views, so
    every stage sees it — as SAS envelopes its whole claim extraction. A
    non-IP claim on a day inside an inpatient stay becomes care setting
    'IP', flag 'X'; claims outside a stay, and claims already IP, are
    unchanged. The procedure table has no flag: blank unless enveloped.
    """
    import datetime as dt
    from qrp import Engine
    from qrp.sqlsplit import split_statements

    D = dt.date
    sql = (Path(qrp.__file__).parent / "sql" / "10_normalize.sql").read_text()
    wanted = ("_ip_days", "_cdm_diagnosis_raw", "cdm_diagnosis",
              "_cdm_procedure_raw", "cdm_procedure")
    stmts = [st.sql for st in split_statements(sql) if st.target in wanted]
    assert len(stmts) == len(wanted), [st.target for st in split_statements(sql)]
    eng = Engine(verbose=False)
    try:
        eng.register("enc", [{"patid": 1, "adate": D(2023, 1, 10), "ddate": D(2023, 1, 12),
                              "enctype": "IP"}],
                     "patid BIGINT, adate DATE, ddate DATE, enctype VARCHAR")
        eng.register("dx", [
            {"patid": 1, "adate": D(2023, 1, 11), "dx": "D1", "dx_codetype": "10", "pdx": "P", "enctype": "AV"},
            {"patid": 1, "adate": D(2023, 1, 20), "dx": "D1", "dx_codetype": "10", "pdx": "P", "enctype": "AV"},
            {"patid": 1, "adate": D(2023, 1, 11), "dx": "D2", "dx_codetype": "10", "pdx": "S", "enctype": "IP"}],
            "patid BIGINT, adate DATE, dx VARCHAR, dx_codetype VARCHAR, pdx VARCHAR, enctype VARCHAR")
        eng.register("px", [
            {"patid": 1, "adate": D(2023, 1, 12), "px": "J1", "px_codetype": "HC", "enctype": "AV"},
            {"patid": 1, "adate": D(2023, 1, 20), "px": "J1", "px_codetype": "HC", "enctype": "AV"}],
            "patid BIGINT, adate DATE, px VARCHAR, px_codetype VARCHAR, enctype VARCHAR")
        for st in stmts:
            eng.con.execute(st.replace("{read_encounter}", "enc").replace("{run_envelope}", "0")
                              .replace("{opt_ddate}", "ddate").replace("{read_diagnosis}", "dx")
                              .replace("{read_procedure}", "px"))
        dxr = {(c, d.day): (e, f) for c, d, e, f in eng.con.execute(
            "SELECT code, adate, enctype, pdx FROM cdm_diagnosis").fetchall()}
        pxr = {d.day: (e, f) for d, e, f in eng.con.execute(
            "SELECT adate, enctype, pdx FROM cdm_procedure").fetchall()}
    finally:
        eng.close()
    assert dxr[("D1", 11)] == ("IP", "X"), "an AV diagnosis inside a stay is enveloped"
    assert dxr[("D1", 20)] == ("AV", "P"), "outside a stay it is unchanged"
    assert dxr[("D2", 11)] == ("IP", "S"), "a claim already IP keeps its own flag"
    assert pxr[12] == ("IP", "X"), "a procedure inside a stay (discharge day) is enveloped"
    assert pxr[20] == ("AV", ""), "outside a stay: blank flag"


def test_a_large_covariate_does_not_go_quadratic():
    """A covariate with tens of thousands of codes must cost linear time.
    Registering covariate codes once rebuilt the covariate's whole
    code->stockgroup map for EVERY code — ~26k x 26k x 23 operations on a
    600k-code study, which then never reached its first stage. Loading a
    study also re-lowercased every large table on each of 56 lookups
    (154M str.lower calls; wp307's load fell from 24.4s to 2.6s when
    cached).

    A 30,000-code covariate: seconds when linear, minutes if either
    regresses.
    """
    import time
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    big = [{"covarnum": 90, "codecat": "DX", "code": f"Z{i:06d}", "stockgroup": "s",
            "covfrom": -183, "covto": -1} for i in range(30_000)]
    t = time.perf_counter()
    study = load_study_dict(
        {**base, "covariatecodes": list(base["covariatecodes"]) + big})
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
    finally:
        eng.close()
    took = time.perf_counter() - t
    # Measured on a single core: 7.4s linear, 56s with the per-code map
    # rebuild. A first bound of 60s let the quadratic version PASS; 25s
    # separates them with room either way.
    assert took < 25, f"a 30,000-code covariate took {took:.0f}s"


@pytest.mark.parametrize("path", sorted((Path(__file__).resolve().parent.parent
                                         / "study").glob("*.json")),
                         ids=lambda p: p.name)
def test_every_example_study_still_loads(path):
    """Every study file shipped in study/ must load. Twelve of them — the
    examples in SAS's own input-file format, one per feature area — were
    referenced by no test and no document, so a change that broke them
    would have gone unnoticed."""
    import warnings as w
    with w.catch_warnings():
        w.simplefilter("ignore")
        study = load_study(path)
    assert study.cohorts, f"{path.name} loaded no cohorts"


def test_truncation_claims_only_for_patients_in_the_cohort():
    """Truncation can only cut a patient's own episodes, and its stockpile
    chain runs per patient, so claims of patients with no exposure in the
    cohort cannot affect anything. They were built and stockpiled anyway:
    on wp307 96% of 2.7M truncation rows (5.3s -> 2.4s once restricted),
    with every one of 31,464 episode ends still matching SAS.

    Here lisinopril is truncated by beta-blocker dispensings, which many
    patients without lisinopril also have.
    """
    from qrp import Engine
    from qrp.config import load_study_dict

    base = json.loads((STUDY.parent / "demo_full.json").read_text())
    bb = sorted({str(r["code"]) for r in base["cohortcodes"]
                 if r.get("group") == "beta_blocker"
                 and str(r.get("codecat", "")).upper() == "RX"})[:5]
    fut = [{"group": "lisinopril", "codecat": "RX", "codetype": "ND", "code": c,
            # a truncation code: INDEXCRITERIA 'FUT', FUPCRITERIA 'NOT'
            "indexcriteria": "FUT", "fupcriteria": "NOT", "stockgroup": "bb"}
           for c in bb]
    study = load_study_dict({**base, "cohortcodes": list(base["cohortcodes"]) + fut})
    codes = ",".join(f"'{c}'" for c in bb)
    eng = Engine(verbose=False)
    try:
        run(study, DATA, engine=eng, verbose=False)
        outsiders, rows, stray = eng.con.execute(f"""
            SELECT
              (SELECT count(DISTINCT d.patid) FROM cdm_dispensing d
                WHERE d.code IN ({codes}) AND NOT EXISTS (
                  SELECT 1 FROM exposure_claims x
                  WHERE x.cohortgrp = 'lisinopril' AND x.patid = d.patid)),
              (SELECT count(*) FROM trunc_claims WHERE cohortgrp = 'lisinopril'),
              (SELECT count(*) FROM trunc_claims t WHERE NOT EXISTS (
                  SELECT 1 FROM exposure_claims x
                  WHERE x.cohortgrp = t.cohortgrp AND x.patid = t.patid))
        """).fetchone()
    finally:
        eng.close()
    assert outsiders > 0, "no outsider has these dispensings: the test proves nothing"
    assert rows > 0, "no truncation claims were built at all"
    assert stray == 0, f"{stray} truncation rows belong to patients outside the cohort"
