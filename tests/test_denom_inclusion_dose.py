"""
DB-2(a): the denominator's inclusion-window and dose-denominator
restrictions (ms_cidadenom.sas / ms_dose_denom.sas).

Every expected value below is derived BY HAND from the SAS rules, on
members enrolled 2021-01-01..2021-12-31 (365 days, no leap day), with
ENRDAYS 0 and no washout, so the unrestricted denominator window is
exactly [2021-01-01, 2021-12-31]:

    EligStart = ADate - CondTo
    EligEnd   = Expiredt - CondFrom      (dateonly = 'N')
              = ADate     - CondFrom     (dateonly = 'Y')
                                              ms_cidadenom.sas:212-219

The fixtures are tiny parquet SCDM extracts built in tmp_path, so the
assertions are exact and the file has no dependency on the shared
synthetic extract.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import duckdb

from qrp import Engine, load_study_dict, run

START = "2021-01-01"
END = "2021-12-31"
YEAR_DAYS = 365
BIRTH = "1981-06-15"  # age 39 inside 2021 -> stratum 18-44
ENR = ("2021-01-01", "2021-12-31")


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------


def _write_table(
    root: Path, name: str, aliases: str, casts: str, rows: list[str]
) -> None:
    con = duckdb.connect()
    try:
        n = len(aliases.split(","))
        values = (
            ", ".join(f"({r})" for r in rows) or "(" + ", ".join(["NULL"] * n) + ")"
        )
        con.execute(
            f"COPY (SELECT {casts} FROM (VALUES {values}) t({aliases}) "
            f"{'LIMIT 0' if not rows else ''}) "
            f"TO '{root / f'{name}.parquet'}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def _scdm(
    root: Path,
    dispensings: list[tuple[int, str, str, int, float]],
    diagnoses: list[tuple[int, str, str]] = (),
) -> None:
    """A minimal SCDM extract: one member (patid 1 unless stated).

    `dispensings` rows are (patid, rxdate, code, rxsup, rxamt);
    `diagnoses` rows are (patid, adate, dx).
    """
    root.mkdir(parents=True, exist_ok=True)
    members = sorted({p for p, *_ in dispensings} | {p for p, *_ in diagnoses})
    _write_table(
        root,
        "enrollment",
        "a, b, c, d, e",
        "CAST(a AS BIGINT) AS patid, CAST(b AS DATE) AS enr_start, "
        "CAST(c AS DATE) AS enr_end, CAST(d AS VARCHAR) AS medcov, "
        "CAST(e AS VARCHAR) AS drugcov",
        [f"{p}, DATE '{ENR[0]}', DATE '{ENR[1]}', 'Y', 'Y'" for p in members],
    )
    _write_table(
        root,
        "demographic",
        "a, b, c, d, e",
        "CAST(a AS BIGINT) AS patid, CAST(b AS DATE) AS birth_date, "
        "CAST(c AS VARCHAR) AS sex, CAST(d AS VARCHAR) AS race, "
        "CAST(e AS VARCHAR) AS hispanic",
        [f"{p}, DATE '{BIRTH}', 'F', '0', 'N'" for p in members],
    )
    if dispensings:
        _write_table(
            root,
            "dispensing",
            "a, b, c, d, e, f",
            "CAST(a AS BIGINT) AS patid, CAST(b AS DATE) AS rxdate, "
            "CAST(c AS VARCHAR) AS rx, CAST(d AS INTEGER) AS rxsup, "
            "CAST(e AS DOUBLE) AS rxamt, CAST(f AS VARCHAR) AS rx_codetype",
            [
                f"{p}, DATE '{d}', '{c}', {s}, {a}, 'ND'"
                for p, d, c, s, a in dispensings
            ],
        )
    _write_table(
        root,
        "diagnosis",
        "a, b, c, d, e, f",
        "CAST(a AS BIGINT) AS patid, CAST(b AS DATE) AS adate, "
        "CAST(c AS VARCHAR) AS dx, CAST(d AS VARCHAR) AS dx_codetype, "
        "CAST(e AS VARCHAR) AS pdx, CAST(f AS VARCHAR) AS enctype",
        [f"{p}, DATE '{d}', '{c}', '09', '', 'OA'" for p, d, c in diagnoses],
    )


def _study(rules: list[dict], strength: list[tuple[str, float]] = ()) -> dict:
    """A one-cohort Type 2 study asking for the CIDA denominator table."""
    return {
        "qrp_parameters_scalars": {
            "type": 2,
            "runid": "db2",
            "startdate": START,
            "enddate": END,
        },
        "cohortfile": [
            {
                "cohortgrp": "c1",
                "enrollmentnum": 1,
                "coverage": "MD",
                "enrolgap": 0,
                "chartres": "N",
                "enrdays": 0,
                "agestrat": "18-44 45-64 65-74 75+",
            }
        ],
        "type2file": [
            {
                "group": "c1",
                "t2washper": 0,
                "point": "N",
                "episodegap": 15,
                "episodegaptype": "F",
                "expextper": 0,
                "minepisdur": 1,
                "maxepisdur": 365,
                "t2fupwashper": 0,
                "eventcount": 1,
                "censor_dth": "Y",
                "t2atriskstart": 0,
                "blackoutper": 0,
                "reqdaysaftepi": 0,
            }
        ],
        "cohortcodes": [
            {"group": "c1", "indexcriteria": "DEF", "code": "DEF1", "codecat": "RX"},
        ],
        "codestrength": [{"code": c, "strength": s} for c, s in strength],
        "userstrata": [{"tableid": "t2cida", "levelid": "1", "levelvars": ""}],
        "inclusioncodes": rules,
    }


def _denom(root: Path, study: dict) -> list[tuple]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        eng = Engine(verbose=False)
        try:
            run(load_study_dict(study), str(root), engine=eng, verbose=False)
            return eng.con.execute(
                'SELECT "group", dennumpts, dennummemdays FROM denomcounts'
            ).fetchall()
        finally:
            eng.close()


# ---------------------------------------------------------------------
# inclusion windows
# ---------------------------------------------------------------------


def test_inclusion_window_anchors_both_ends(tmp_path):
    """CondTo sets the EARLIEST index, CondFrom the latest, and an RX
    supply pushes the latest further out (ms_cidadenom.sas:212-219).

    Dispensing 2021-06-01, supply 10 (Expiredt 06-10), window
    [-30, -10]: EligStart = 06-01 + 10 = 06-11,
    EligEnd = 06-10 + 30 = 07-10 -> 30 member-days.
    """
    _scdm(tmp_path, [(1, "2021-06-01", "INC1", 10, 10.0)])
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "INC1",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 30)]


def test_dateonly_y_anchors_the_end_on_the_claim_date(tmp_path):
    """dateonly='Y' measures EligEnd from ADate, ignoring the supply:
    [06-11, 06-01 + 30] = 21 member-days (ms_cidadenom.sas:213-218).
    """
    _scdm(tmp_path, [(1, "2021-06-01", "INC1", 10, 10.0)])
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "INC1",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                    "dateonly": "Y",
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 21)]


def test_codedays_overlaps_distinct_dispensing_dates(tmp_path):
    """CODEDAYS windows from DISTINCT dates must overlap
    (ms_cidadenom.sas:220-247).

    Dispensings 05-10 (supply 5, Expiredt 05-14) and 05-20 (05-24),
    window [-30, -10]:
      w1 = [05-20, 06-13], w2 = [05-30, 06-23] -> overlap 15 days.
    A third dispensing 07-01 overlaps neither, so pairs (1,3) and (2,3)
    contribute nothing, and a member with a single dispensing has no
    qualifying day at all.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-05-10", "INC2", 5, 5.0),
            (1, "2021-05-20", "INC2", 5, 5.0),
            (1, "2021-07-01", "INC2", 5, 5.0),
            (2, "2021-05-10", "INC2", 5, 5.0),
        ],
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "INC2",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                    "codedays": 2,
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 15)]


def test_minrxdays_suppresses_the_denominator_like_sas(tmp_path):
    """SAS refuses to emit a denominator at all when minrxdays > 1
    appears in the inclusion criteria — "Outputdenom set to N for
    <group> because minrxdays is used in inclusion/exclusion criteria"
    (ms_setnumloopmacrovars.sas:898-900): a pro-rated supply requirement
    makes the eligible-member count incoherent. minrxdays = 1 is the
    default and computes normally; nothing in the denominator's window
    arithmetic reads minrxdays (it gates the NUMERATOR's index dates,
    ms_createpov3.sas:374-393).
    """
    _scdm(tmp_path, [(1, "2021-06-01", "INC1", 10, 10.0)])
    rule = {
        "group": "c1",
        "indexcriteria": "INC",
        "codecat": "RX",
        "code": "INC1",
        "condlevel": "1",
        "subcondlevel": "1",
        "condfrom": -30,
        "condto": -10,
    }
    satisfiable = _denom(tmp_path, _study([{**rule, "minrxdays": 1}]))
    assert satisfiable == [("c1", 1, 30)]
    suppressed = _denom(tmp_path, _study([{**rule, "minrxdays": 2}]))
    assert suppressed == [], "minrxdays > 1 must suppress the denominator, not clip it"


def test_subconditions_intersect_within_a_condition(tmp_path):
    """Both subconditions of a condition must hold at once: SAS clips
    the same enrollment copy successively (ms_cidadenom.sas:253-261).

    Subcond 1 (code A, [-30, -10]): A on 06-01 -> [06-11, 07-01].
    Subcond 2 (code B, [-20, -5]):  B on 06-20 -> [06-25, 07-10].
    Intersection [06-25, 07-01] = 7 member-days; a member meeting only
    subcond 1 keeps none.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-06-01", "A", 1, 1.0),
            (1, "2021-06-20", "B", 1, 1.0),
            (2, "2021-06-01", "A", 1, 1.0),
        ],
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "A",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                },
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "B",
                    "condlevel": "1",
                    "subcondlevel": "2",
                    "condfrom": -20,
                    "condto": -5,
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 7)]


def test_episode_end_anchor_does_not_raise_a_conditions_need(tmp_path):
    """win_need counts only the subconditions that produce denominator
    windows. The claim stage skips EPISODEENDDT-anchored rules (an
    episode end does not exist in the denominator's enrolled time), so
    `keys` must skip them too: counting one made a condition mixing a
    plain subcond and an anchored subcond require 2 covering windows
    while at most 1 could ever exist, zeroing the cohort out. SAS
    builds the inclusion table from the group's rules and intersects
    the windows it actually derived (ms_cidadenom.sas:220-261).

    Subcond 1 (code A 2021-06-01, window [-30, -24]) alone covers
    [06-25, 07-01] = 7 member-days. Subcond 2 (code B 2021-06-20,
    window [-20, -5], condtoanchor EPISODEENDDT) produces no window
    and must not raise the requirement.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-06-01", "A", 1, 1.0),
            (1, "2021-06-20", "B", 1, 1.0),
        ],
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "A",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -24,
                },
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "B",
                    "condlevel": "1",
                    "subcondlevel": "2",
                    "condfrom": -20,
                    "condto": -5,
                    "condtoanchor": "EPISODEENDDT",
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 7)]


def test_conditions_union_across(tmp_path):
    """Meeting ANY condition's inclusions makes time eligible: the
    merged STACK_INC windows union (ms_cidadenom.sas:406).

    Cond 1: A on 06-01 -> [06-11, 07-01] (21 days).
    Cond 2: B on 08-01 -> [08-06, 08-11] (6 days).
    Disjoint windows union to 27 member-days; an intersection would
    leave the member with none.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-06-01", "A", 1, 1.0),
            (1, "2021-08-01", "B", 1, 1.0),
        ],
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "A",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                },
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "B",
                    "condlevel": "2",
                    "subcondlevel": "1",
                    "condfrom": -10,
                    "condto": -5,
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 27)]


def test_exclusion_shaves_after_inclusion(tmp_path):
    """INC first, EXC second (ms_cidadenom.sas:400-449): inclusion
    keeps [06-11, 07-01]; an exclusion code on 06-20 with window
    [-5, -5] disqualifies the index day 06-25 only (an index at T sees
    the code at T-5), leaving 21 - 1 = 20 member-days.
    """
    _scdm(
        tmp_path, [(1, "2021-06-01", "A", 1, 1.0)], diagnoses=[(1, "2021-06-20", "E")]
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "A",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                },
                {
                    "group": "c1",
                    "indexcriteria": "EXC",
                    "codecat": "DX",
                    "code": "E",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -5,
                    "condto": -5,
                },
            ]
        ),
    )
    assert rows == [("c1", 1, 20)]


# ---------------------------------------------------------------------
# dose-denominator restriction (%ms_dose_denom)
# ---------------------------------------------------------------------
#
# One dispensing: 2021-06-01, supply 10 (Expiredt 06-10), rxamt 100,
# strength 1 -> full cumdose 100, cfdd 10.  Window [-30, -10], so the
# candidate days are [06-11, 07-10] and a day d sees the supply
# [d-30, d-10] overlap the claim [06-01, 06-10] by
#   od(d) = min(06-10, d-10) - max(06-01, d-30) + 1,
# credited cumdose = 100 * od / 10 = 10 * od.  round(cumdose,1) >= 50
# needs od >= 5, which holds exactly for d in [06-15, 07-06] (22 days,
# one contiguous span); >= 40 needs od >= 4 -> [06-14, 07-07] (24 days).


def _dose_rule(thresholds: dict) -> dict:
    return {
        "group": "c1",
        "indexcriteria": "INC",
        "codecat": "RX",
        "code": "D",
        "condlevel": "1",
        "subcondlevel": "1",
        "condfrom": -30,
        "condto": -10,
        **thresholds,
    }


def test_dose_threshold_keeps_only_prorated_qualifying_days(tmp_path):
    _scdm(tmp_path, [(1, "2021-06-01", "D", 10, 100.0)])
    rows = _denom(
        tmp_path, _study([_dose_rule({"mincumdose": 50})], strength=[("D", 1.0)])
    )
    assert rows == [("c1", 1, 22)]


def test_dose_proration_moves_the_qualifying_edges(tmp_path):
    _scdm(tmp_path, [(1, "2021-06-01", "D", 10, 100.0)])
    rows = _denom(
        tmp_path, _study([_dose_rule({"mincumdose": 40})], strength=[("D", 1.0)])
    )
    assert rows == [("c1", 1, 24)]


def test_dateonly_dose_crediting_ignores_the_supply_tail(tmp_path):
    """A dateonly claim's crediting period is its dispensing date
    alone — never the supply tail:
    `(claim.dateonly='N' ... period2=claim.ADate claim.Expiredt) |
     (claim.dateonly='Y' ... period2=claim.ADate)`
    (ms_dose_denom.sas:148-151).

    Rule code D, window [-30, -10], dateonly, mincumdose 15,
    strength 1:
      claim 1: D 2021-06-01, supply 10, rxamt 100
      claim 2: D 2021-06-11, supply 1,  rxamt 10

    Each enumerated day d looks back over [d-30, d-10]. Claim 1
    credits only when 06-01 falls in that lookback (d in
    [06-11, 07-01]), worth 100 * 1/10 = 10; claim 2 only when 06-11
    does (d in [06-21, 07-11]), worth 10 * 1/1 = 10. Only the 11 days
    seeing BOTH dispensings ([06-21, 07-01]) reach cumdose 20 >= 15.
    Crediting claim 1's supply tail 06-02..06-10 (overlap up to 10
    days -> cumdose 100) instead would qualify every day in
    [06-12, 07-10] and score 29 member-days.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-06-01", "D", 10, 100.0),
            (1, "2021-06-11", "D", 1, 10.0),
        ],
    )
    rows = _denom(
        tmp_path,
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "D",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                    "dateonly": "Y",
                    "mincumdose": 15,
                },
            ],
            strength=[("D", 1.0)],
        ),
    )
    assert rows == [("c1", 1, 11)]


def test_dose_afdd_bounds_qualify_without_mincumdose(tmp_path):
    """afdd = round(sum(cfdd) / numdispensing, 1) = 10 on every
    candidate day, so [8, 12] keeps all 30 enumerated days
    (ms_dose_denom.sas:173-182); 5 or 15 excludes every day and the
    member keeps no eligible time.
    """
    _scdm(tmp_path, [(1, "2021-06-01", "D", 10, 100.0)])
    keep = _denom(
        tmp_path,
        _study([_dose_rule({"minafdd": 8, "maxafdd": 12})], strength=[("D", 1.0)]),
    )
    assert keep == [("c1", 1, 30)]

    too_high = _denom(
        tmp_path, _study([_dose_rule({"maxafdd": 5})], strength=[("D", 1.0)])
    )
    assert too_high == []

    too_low = _denom(
        tmp_path, _study([_dose_rule({"minafdd": 15})], strength=[("D", 1.0)])
    )
    assert too_low == []


def test_exclusion_dose_removes_its_qualifying_days(tmp_path):
    """The exclusion side shaves %ms_dose_denom's spans OUT
    (ms_cidadenom.sas:366-376): meeting the dose profile is what
    disqualifies. The EXC window removes [06-11, 07-01] and the dose
    spans [06-15, 07-06]; together [06-11, 07-06], 26 days, leaving
    365 - 26 = 339.
    """
    _scdm(tmp_path, [(1, "2021-06-01", "D", 10, 100.0)])
    rule = _dose_rule({"mincumdose": 50})
    rule["indexcriteria"] = "EXC"
    rows = _denom(tmp_path, _study([rule], strength=[("D", 1.0)]))
    assert rows == [("c1", 1, YEAR_DAYS - 26)]


# ---------------------------------------------------------------------
# the excl_incl = N path must be untouched
# ---------------------------------------------------------------------


def test_no_inclusion_codes_keep_the_full_window(tmp_path):
    """No INCLUSIONCODES: SAS never clips (excl_incl=N), so the
    denominator is exactly the enrollment window, 365 member-days for
    the one enrolled member.
    """
    _scdm(tmp_path, [(1, "2021-06-01", "DEF1", 10, 10.0)])
    rows = _denom(tmp_path, _study([]))
    assert rows == [("c1", 1, YEAR_DAYS)]


def test_inclusion_scoping_is_per_cohort_group_in_mixed_studies(tmp_path):
    """A cohort group without INC rules is never inclusion-clipped,
    even when OTHER groups in the study have them: SAS builds
    _IT&inclusioncodes. per group (`where lowcase(group)="&milgrp."`,
    ms_createmicohorts.sas:1009-1015), sets excl_incl=Y only when THAT
    group has rules (:1020-1021), and the stack shave runs only
    `%IF %SYSFUNC(exist(_Denom&Group._STACK_INC))=1`
    (ms_cidadenom.sas:399). Scoping the pass-through guard on the
    study as a whole dropped every window of the INC-less cohort.

    Cohort c1 carries an INC rule: dispensing 2021-06-01, supply 10
    (Expiredt 06-10), window [-30, -10] -> eligible [06-11, 07-01],
    30 member-days. Cohort c2 carries none and keeps the full
    365-day enrollment window.
    """
    _scdm(
        tmp_path,
        [
            (1, "2021-06-01", "INC1", 10, 10.0),
            (1, "2021-06-01", "DEF1", 10, 10.0),
        ],
    )
    study = _study(
        [
            {
                "group": "c1",
                "indexcriteria": "INC",
                "codecat": "RX",
                "code": "INC1",
                "condlevel": "1",
                "subcondlevel": "1",
                "condfrom": -30,
                "condto": -10,
            },
        ]
    )
    study["cohortfile"].append(dict(study["cohortfile"][0], cohortgrp="c2"))
    study["type2file"].append(dict(study["type2file"][0], group="c2"))
    study["cohortcodes"].append(
        {"group": "c2", "indexcriteria": "DEF", "code": "DEF1", "codecat": "RX"}
    )
    rows = _denom(tmp_path, study)
    assert sorted(rows) == [("c1", 1, 30), ("c2", 1, YEAR_DAYS)]


def test_denominator_config_key_separates_inclusion_rules():
    """The denominator is clipped by the INCLUSION rules, so cohorts
    with different ones must not share a denominator pass — sharing
    would apply one cohort's inclusions to the other's denominator.
    """
    from qrp.pipeline import _denom_cfg_id

    rules = [
        {
            "group": "c2",
            "indexcriteria": "INC",
            "codecat": "RX",
            "code": "INC1",
            "condlevel": "1",
            "subcondlevel": "1",
            "condfrom": -30,
            "condto": -10,
        }
    ]
    study = _study(rules)
    study["cohortfile"].append(dict(study["cohortfile"][0], cohortgrp="c2"))
    study["type2file"].append(dict(study["type2file"][0], group="c2"))
    study["cohortcodes"].append(
        {"group": "c2", "indexcriteria": "DEF", "code": "DEF1", "codecat": "RX"}
    )
    s = load_study_dict(study)
    ids = {c.cohortgrp: _denom_cfg_id(c, s) for c in s.cohorts}
    assert len(ids) == 2, (
        "cohorts with different inclusion rules shared a denominator "
        "config: " + str(ids)
    )


def test_dateonly_reaches_the_parsed_rule():
    """The dateonly column must survive parsing into cfg_inclusion —
    a field written where nothing reads it looks exactly like a
    supported variant."""
    s = load_study_dict(
        _study(
            [
                {
                    "group": "c1",
                    "indexcriteria": "INC",
                    "codecat": "RX",
                    "code": "INC1",
                    "condlevel": "1",
                    "subcondlevel": "1",
                    "condfrom": -30,
                    "condto": -10,
                    "dateonly": "Y",
                },
            ]
        )
    )
    assert s.inclusions[0].dateonly is True
