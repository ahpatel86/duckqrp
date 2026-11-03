"""
Blank-string demographics must be missing, and attrition level 1 must
count what pov1 counts.

SAS tests birth date and sex with the missing() function
(ms_create_enrollment_spans.sas:187-192), and on a character variable
missing() is true for the blank string too, because SAS has no NULL.
SAS drops those members from _keep before the level-1 count
(ms_attrition_enrollment.sas:89-95). A blank sex never reaches POV1
because it fails the demographic in-list
(ms_create_enrollment_spans.sas:141).

The port tested `sex IS NULL` only, and the collapsed `sex` column never
yields NULL, so the level-1 count included a blank-string sex member
that POV1 excluded. These tests pin level 1 and POV1 to the same member
set.

Hand fixtures, not the synthetic extract: the defect shows only when the
extract contains a blank-string sex, which real SCDM data can contain
and the generator never produced.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import duckdb
import pytest

from qrp import Engine, load_study, run

# One legal member and three defective shapes: blank-string sex, NULL
# sex, NULL birth date — each with enrollment and a dispensing of the
# exposure code, so only the demographics can exclude them.
MIXED = [
    # patid, birth_date, sex
    (1, "1980-05-10", "F"),      # legal
    (2, "1975-03-03", ""),       # blank-string sex: SAS missing(), not NULL
    (3, "1970-01-01", None),     # NULL sex
    (4, None, "M"),              # NULL birth date
]

# A conformant extract: sex is always F/M/U/A, never blank, never NULL.
LEGAL = [
    (5, "1980-05-10", "M"),
    (6, "1985-05-10", "U"),
    (7, "1990-05-10", "A"),
]


def _write_extract(data_dir: Path, members: list[tuple]) -> None:
    for t in ("demographic", "enrollment", "dispensing", "diagnosis",
              "death"):
        (data_dir / t).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    demog = ", ".join(
        f"({p}, "
        + (f"DATE '{b}'" if b is not None else "NULL") + ", "
        + (f"'{s}'" if s is not None else "NULL") + ", '1', 'N')"
        for p, b, s in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {demog})
              t(patid, birth_date, sex, race, hispanic))
        TO '{data_dir / "demographic" / "data.parquet"}' (FORMAT PARQUET)
    """)
    enr = ", ".join(
        f"({p}, DATE '2012-01-01', DATE '2015-01-01', 'Y', 'Y')"
        for p, _, _ in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {enr})
              t(patid, enr_start, enr_end, medcov, drugcov))
        TO '{data_dir / "enrollment" / "data.parquet"}' (FORMAT PARQUET)
    """)
    disp = ", ".join(
        f"({p}, DATE '2013-06-15', 'E00001', 'ND', 30, 1.0)"
        for p, _, _ in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {disp})
              t(patid, rxdate, rx, rx_codetype, rxsup, rxamt))
        TO '{data_dir / "dispensing" / "data.parquet"}' (FORMAT PARQUET)
    """)
    dx = ", ".join(
        f"({p}, DATE '2013-06-20', 'D00001', '09', 'P', 'AV')"
        for p, _, _ in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {dx})
              t(patid, adate, dx, dx_codetype, pdx, enctype))
        TO '{data_dir / "diagnosis" / "data.parquet"}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (SELECT * FROM (VALUES ({members[0][0]}, DATE '2013-07-01'))
              t(patid, deathdt))
        TO '{data_dir / "death" / "data.parquet"}' (FORMAT PARQUET)
    """)
    con.close()


def _study_file(tmp_path: Path) -> Path:
    study = {
        "qrp_parameters": [{
            "type": 2, "runid": "db6", "startdate": "2012-01-01",
            "enddate": "2015-06-30", "censordate": "2015-06-30",
        }],
        "cohortfile": [{
            "cohortgrp": "g1", "enrollmentnum": 1, "coverage": "MD",
            "enrolgap": 45, "chartres": "N", "enrdays": 183,
            "agestrat": "18-44 45-64 65-74 75+", "reqdaysaftind": 0,
        }],
        "type2file": [{
            "group": "g1", "t2washper": 183, "point": "N",
            "episodegap": 15, "episodegaptype": "F", "expextper": 0,
            "minepisdur": 1, "maxepisdur": 0, "t2fupwashper": 183,
            "eventcount": 1, "censor_dth": "Y", "t2atriskstart": 0,
            "blackoutper": 0, "reqdaysaftepi": 0,
        }],
        "cohortcodes": [
            {"group": "g1", "indexcriteria": "DEF", "code": "E00001",
             "codecat": "RX"},
        ],
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(study))
    return path


def _run(tmp_path: Path, members: list[tuple]) -> Engine:
    _write_extract(tmp_path / "indata", members)
    eng = Engine(verbose=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run(load_study(_study_file(tmp_path)), tmp_path / "indata",
            engine=eng, verbose=False)
    return eng


def _level1(eng: Engine) -> int:
    row = eng.con.execute(
        "SELECT remaining FROM attrition WHERE level = 1.0"
    ).fetchone()
    return row[0]


@pytest.fixture()
def mixed(tmp_path):
    eng = _run(tmp_path, MIXED)
    yield eng
    eng.close()


@pytest.fixture()
def legal(tmp_path):
    eng = _run(tmp_path, LEGAL)
    yield eng
    eng.close()


def test_blank_sex_is_missing(mixed):
    """A blank-string sex is SAS-missing: demographics_missing flags it.
    """
    flagged = {r[0] for r in mixed.con.execute(
        "SELECT patid FROM demographics_missing").fetchall()}
    assert flagged == {2, 3, 4}


def test_blank_sex_member_not_counted_at_level1(mixed):
    """SAS counts level 1 from _keep (ms_attrition.sas:184-185).
    ms_attrition_enrollment.sas:89-95 drops the missing-demographics
    members first, so level 1 is the legal member alone."""
    assert _level1(mixed) == 1


def test_level1_matches_pov1(mixed):
    """The level-1 population and POV1 admit exactly the same member.
    """
    att = {r[0] for r in mixed.con.execute(
        "SELECT patid FROM _att_dem").fetchall()}
    pov = {r[0] for r in mixed.con.execute(
        "SELECT patid FROM pov1").fetchall()}
    assert att == {1}
    assert pov == {1}


def test_null_birth_member_excluded(mixed):
    """A NULL birth date is missing under both the old and the new
    predicate — pinned so the predicate change cannot weaken it."""
    assert {r[0] for r in mixed.con.execute(
        "SELECT patid FROM pov1").fetchall()} == {1}


def test_legal_extract_unchanged(legal):
    """Zero behavior change for conformant extracts: every sex is
    F/M/U/A, so level 1 counts all members and POV1 keeps them all."""
    assert _level1(legal) == 3
    assert {r[0] for r in legal.con.execute(
        "SELECT patid FROM pov1").fetchall()} == {5, 6, 7}
