"""
A blank claim Pdx stays blank until enveloping recodes it.

SAS keeps blank Pdx blank: PX claims are stacked with PDX=" "
(ms_cidanum.sas:670) and a DX claim's own blank passes through
extraction untouched. The care-setting match compares the claim's Pdx
to the parameter token as-is — blank equals blank, and the wildcard
branches leave the claim side unconstrained (ms_loopmeds.sas:56-59) —
and the code distribution merges on the exact key, whose parameter side
carries a blank member next to P, S and X
(ms_expand_enctype_pdx.sas:59-67, ms_codedistribution.sas:154-165).
The only blank->'X' recode is the inpatient-stay enveloping
(ms_envelope.sas:113-116), which fires on non-IP claims inside a stay.

The port did `COALESCE(pdx, 'X')` at normalization, before any consumer
ran: a blank-Pdx DX claim read as inpatient-principal everywhere, so
the config grammar's explicit blank token (written '_' in
caresettingprincipal) and the distribution's blank member could never
match it. Every pdx consumer on this base already COALESCEs the
normalized value to '' (event_claims' claim stack and care-setting
join, covar_source for risk scores, the code distribution's claim
stack), so removing the relabel at the source makes each consumer
correct. None needed adapting.

Hand fixtures: the synthetic extract gives every DX claim a real P/S,
so blank-Pdx DX claims never occurred there.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import duckdb
import pytest

from qrp import Engine, load_study, run

# Claims of one EVENT code (D00001, DX) with every Pdx shape, and one
# IP stay for the enveloping member. Everyone has the exposure
# dispensing, so each member gets an episode.
DX_CLAIMS = [
    # patid, adate, pdx, enctype
    (1, "2013-07-01", None, "AV"),   # NULL Pdx: the fix's target shape
    (2, "2013-07-01", "", "AV"),     # blank-string Pdx: same treatment
    (3, "2013-07-01", "X", "AV"),    # real data value: not blank
    (4, "2013-06-20", "P", "IP"),    # real inpatient-principal claim
    (5, "2013-07-10", None, "AV"),   # inside the IP stay: enveloped
]

ENCOUNTERS = [(5, "2013-07-10", "IP", "2013-07-15")]

MEMBERS = [p for p, _, _, _ in DX_CLAIMS]


def _write_extract(data_dir: Path) -> None:
    for t in ("demographic", "enrollment", "dispensing", "diagnosis",
              "death", "encounter"):
        (data_dir / t).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    demog = ", ".join(
        f"({p}, DATE '1980-05-10', 'F', '1', 'N')" for p in MEMBERS
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {demog})
              t(patid, birth_date, sex, race, hispanic))
        TO '{data_dir / "demographic" / "data.parquet"}' (FORMAT PARQUET)
    """)
    enr = ", ".join(
        f"({p}, DATE '2012-01-01', DATE '2015-01-01', 'Y', 'Y')"
        for p in MEMBERS
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {enr})
              t(patid, enr_start, enr_end, medcov, drugcov))
        TO '{data_dir / "enrollment" / "data.parquet"}' (FORMAT PARQUET)
    """)
    disp = ", ".join(
        f"({p}, DATE '2013-06-15', 'E00001', 'ND', 30, 1.0)"
        for p in MEMBERS
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {disp})
              t(patid, rxdate, rx, rx_codetype, rxsup, rxamt))
        TO '{data_dir / "dispensing" / "data.parquet"}' (FORMAT PARQUET)
    """)
    dx = ", ".join(
        f"({p}, DATE '{a}', 'D00001', '09', "
        + ("NULL" if pdx is None else f"'{pdx}'")
        + f", '{enc}')"
        for p, a, pdx, enc in DX_CLAIMS
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {dx})
              t(patid, adate, dx, dx_codetype, pdx, enctype))
        TO '{data_dir / "diagnosis" / "data.parquet"}' (FORMAT PARQUET)
    """)
    enc_rows = ", ".join(
        f"({p}, DATE '{a}', '{e}', DATE '{d}')"
        for p, a, e, d in ENCOUNTERS
    )
    con.execute(f"""
        COPY (SELECT * FROM (VALUES {enc_rows})
              t(patid, adate, enctype, ddate))
        TO '{data_dir / "encounter" / "data.parquet"}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (SELECT * FROM (VALUES (1, DATE '2014-06-01'))
              t(patid, deathdt))
        TO '{data_dir / "death" / "data.parquet"}' (FORMAT PARQUET)
    """)
    con.close()


def _study_file(tmp_path: Path, care_setting: str | None) -> Path:
    codes = [
        {"group": "g1", "indexcriteria": "DEF", "code": "E00001",
         "codecat": "RX"},
        {"group": "g1", "indexcriteria": "EVENT", "code": "D00001",
         "codecat": "DX"},
    ]
    if care_setting is not None:
        # SAS's token grammar: 'AV_' = EncType AV, Pdx missing
        # (ms_caresettingprincipal.sas:65-66 recodes '_' to blank).
        codes[1]["caresettingprincipal"] = care_setting
    study = {
        "qrp_parameters": [{
            "type": 2, "runid": "db8", "startdate": "2012-01-01",
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
        "cohortcodes": codes,
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(study))
    return path


def _run(tmp_path: Path, care_setting: str | None) -> Engine:
    _write_extract(tmp_path / "indata")
    eng = Engine(verbose=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run(load_study(_study_file(tmp_path, care_setting)),
            tmp_path / "indata", engine=eng, verbose=False)
    return eng


@pytest.fixture()
def token(tmp_path):
    eng = _run(tmp_path, "AV_")
    yield eng
    eng.close()


@pytest.fixture()
def untokened(tmp_path):
    eng = _run(tmp_path, None)
    yield eng
    eng.close()


def _dx(eng: Engine, patid: int) -> tuple[str, str]:
    return eng.con.execute(
        "SELECT pdx, enctype FROM cdm_diagnosis WHERE patid = ?", [patid]
    ).fetchone()


def test_blank_pdx_survives_normalize(token):
    """NULL and blank Pdx normalize to '', a real 'X' stays 'X', a real
    inpatient-principal claim stays P/IP — none of the four shapes is
    relabeled at the source."""
    assert _dx(token, 1) == ("", "AV")
    assert _dx(token, 2) == ("", "AV")
    assert _dx(token, 3) == ("X", "AV")
    assert _dx(token, 4) == ("P", "IP")


def test_enveloping_still_recodes_to_x_ip(token):
    """The inpatient-stay enveloping is %ms_envelope parity
    (ms_envelope.sas:113-116) and must survive the fix: the non-IP claim
    inside member 5's stay becomes X/IP."""
    assert _dx(token, 5) == ("X", "IP")


def test_blank_pdx_matches_blank_care_setting_token(token):
    """Under a study restricting the event code to AV with a missing
    principal (token 'AV_'), the blank-Pdx claims match and the real-X
    claim does not, because SAS matches the claim's own value and X
    is not blank."""
    fired = {r[0] for r in token.con.execute(
        "SELECT DISTINCT patid FROM event_claims").fetchall()}
    assert fired == {1, 2}


def test_blank_pdx_attributed_to_blank_distindex_entity(token):
    """The blank-Pdx claim's hoi list names the distribution entity
    whose pdx is the blank member (ms_expand_enctype_pdx.sas:59-67),
    not the 'X' entity."""
    entities = dict(token.con.execute(
        "SELECT pdx, distindexid FROM distindexmap "
        "WHERE codecat = 'DX' AND code = 'D00001' AND enctype = 'AV'"
    ).fetchall())
    # The blank token pins the parameter's Pdx, so the map carries the
    # blank entity alone — no 'X' entity exists to be wrongly matched.
    assert set(entities) == {""}
    lst = token.con.execute(
        "SELECT distindexhoi FROM distindex_episodes WHERE patid = 2"
    ).fetchone()[0]
    assert str(entities[""]) in lst.split("_")


def test_no_token_study_unchanged(untokened):
    """Zero behavior change for studies whose codes carry no
    caresettingprincipal: every claim matches the wildcard, and a claim
    with a real Pdx value is attributed to its own entity exactly as
    before. (A blank-Pdx claim moves from the 'X' entity to the ''
    entity — that reattribution is the fix, matching SAS's blank
    expansion row.)"""
    fired = {r[0] for r in untokened.con.execute(
        "SELECT DISTINCT patid FROM event_claims").fetchall()}
    assert fired == set(MEMBERS)
    entities = dict(untokened.con.execute(
        "SELECT pdx, distindexid FROM distindexmap "
        "WHERE codecat = 'DX' AND code = 'D00001' AND enctype = 'AV'"
    ).fetchall())
    assert set(entities) == {"P", "S", "X", ""}
    x_lst = untokened.con.execute(
        "SELECT distindexhoi FROM distindex_episodes WHERE patid = 3"
    ).fetchone()[0]
    assert str(entities["X"]) in x_lst.split("_")
    blank_lst = untokened.con.execute(
        "SELECT distindexhoi FROM distindex_episodes WHERE patid = 1"
    ).fetchone()[0]
    assert str(entities[""]) in blank_lst.split("_")
    assert str(entities["X"]) not in blank_lst.split("_")
