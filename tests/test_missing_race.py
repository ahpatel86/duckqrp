"""
A missing race excludes the member; it never becomes 'M'.

SAS's enrollment-span join requires `upcase(Dem.Race) in (&DemogRace.)`
(ms_create_enrollment_spans.sas:142) against the study's race list, or
the default "0" "1" "2" "3" "4" "5" "M" (ms_create_enrollment_spans.sas:
89-95). A missing or blank race matches no list, so the member keeps no
spans and produces no POV1 rows (attrition marks MeetDemogs='N' the
same way, ms_attrition_enrollment.sas:186-189); POV1 passes a surviving
member's RACE through unchanged (ms_createpov1.sas:90).

The port did `COALESCE(race, 'M')`: every NULL race — and every member
of an extract whose demographic file has no race column at all, since
the optional-column machinery resolves it to NULL — was evaluated as
Multi-racial in the demographic-condition join and labeled Multi-racial
in stratified outputs.

Hand fixtures: the synthetic extract always carries a populated race,
so none of these shapes occur there.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import duckdb
import pytest

from qrp import Engine, load_study, run

# All members share a valid birth date, sex 'F', enrollment and a
# dispensing of the exposure code — race is the only difference.
MEMBERS = [
    # patid, race
    (1, "1"),    # legal value: kept
    (2, None),   # NULL race: excluded
    (3, ""),     # blank-string race: excluded
    (4, "0"),    # Unknown: kept under the default conditioning
    (5, "M"),    # real Multi-racial data value: kept, labeled 'M'
]


def _write_extract(data_dir: Path, members: list[tuple],
                   include_race: bool = True) -> None:
    for t in ("demographic", "enrollment", "dispensing", "diagnosis",
              "death"):
        (data_dir / t).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    if include_race:
        demog = ", ".join(
            f"({p}, DATE '1980-05-10', 'F', "
            + ("NULL" if r is None else f"'{r}'")
            + ", 'N')"
            for p, r in members
        )
        cols = "t(patid, birth_date, sex, race, hispanic)"
    else:
        demog = ", ".join(
            f"({p}, DATE '1980-05-10', 'F', 'N')" for p, _ in members
        )
        cols = "t(patid, birth_date, sex, hispanic)"
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {demog}) {cols})
        TO '{data_dir / "demographic" / "data.parquet"}' (FORMAT PARQUET)
    """)
    enr = ", ".join(
        f"({p}, DATE '2012-01-01', DATE '2015-01-01', 'Y', 'Y')"
        for p, _ in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {enr})
              t(patid, enr_start, enr_end, medcov, drugcov))
        TO '{data_dir / "enrollment" / "data.parquet"}' (FORMAT PARQUET)
    """)
    disp = ", ".join(
        f"({p}, DATE '2013-06-15', 'E00001', 'ND', 30, 1.0)"
        for p, _ in members
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {disp})
              t(patid, rxdate, rx, rx_codetype, rxsup, rxamt))
        TO '{data_dir / "dispensing" / "data.parquet"}' (FORMAT PARQUET)
    """)
    dx = ", ".join(
        f"({p}, DATE '2013-06-20', 'D00001', '09', 'P', 'AV')"
        for p, _ in members
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
    # No demographic conditions in the study: the defaults apply —
    # sex F/M/U/A, race 0-5/M, hispanic Y/N/U (config.py CohortConfig),
    # mirroring SAS's default &DemogRace. list.
    study = {
        "qrp_parameters": [{
            "type": 2, "runid": "db7", "startdate": "2012-01-01",
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


def _run(tmp_path: Path, include_race: bool = True) -> Engine:
    _write_extract(tmp_path / "indata", MEMBERS, include_race)
    eng = Engine(verbose=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run(load_study(_study_file(tmp_path)), tmp_path / "indata",
            engine=eng, verbose=False)
    return eng


@pytest.fixture()
def mixed(tmp_path):
    eng = _run(tmp_path)
    yield eng
    eng.close()


@pytest.fixture()
def raceless(tmp_path):
    eng = _run(tmp_path, include_race=False)
    yield eng
    eng.close()


def test_missing_race_collapses_to_empty(mixed):
    """NULL and blank race normalize to '', not 'M'; real values are
    untouched."""
    rows = dict(mixed.con.execute(
        "SELECT patid, race FROM demographics ORDER BY patid"
    ).fetchall())
    assert rows == {1: "1", 2: "", 3: "", 4: "0", 5: "M"}


def test_missing_race_member_excluded_from_pov1(mixed):
    """SAS excludes the member; it does not ride through as Multi-racial.
    Unknown ('0') and a real 'M' data value stay, under the default
    conditioning."""
    kept = dict(mixed.con.execute(
        "SELECT patid, race FROM pov1 ORDER BY patid"
    ).fetchall())
    assert kept == {1: "1", 4: "0", 5: "M"}


def test_race_label_is_the_data_value(mixed):
    """The stratified outputs label race from the member's own value:
    the kept Multi-racial member is labeled 'M' by its data, not by a
    defaulting rule."""
    labeled = [r for r in mixed.con.execute(
        "SELECT patid, race FROM pov1 WHERE patid = 5"
    ).fetchall()]
    assert labeled == [(5, "M")]


def test_absent_race_column_excludes_every_member(raceless):
    """An extract with NO race column previously admitted every member
    as 'M'; now it excludes every member. That IS the SAS-consistent
    reading: SAS datasets always carry Race, so a race-less extract is
    non-conformant, and SAS's blank-fails-the-in-list rule excludes
    what a missing column resolves to. Pinned explicitly."""
    rows = raceless.con.execute(
        "SELECT patid, race FROM demographics ORDER BY patid"
    ).fetchall()
    assert rows == [(p, "") for p, _ in MEMBERS]
    assert raceless.con.execute(
        "SELECT count(*) FROM pov1").fetchone()[0] == 0


def test_level1_ignores_race(mixed):
    """Attrition level 1 counts non-missing birth date/sex only
    (ms_attrition.sas:184-185); race never enters it, before or after
    the fix — pinned so the fix cannot leak into the funnel's first
    level."""
    row = mixed.con.execute(
        "SELECT remaining FROM attrition WHERE level = 1.0"
    ).fetchone()
    assert row[0] == len(MEMBERS)
