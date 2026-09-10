"""
Correctness tests for the two non-obvious rewrites.

Both replace a construct the PySpark port implemented literally (a Python
UDF-driven sequential loop, and a range self-join) with an equivalent
window-function formulation. Equivalence is not self-evident, so it is
tested against a brute-force reference implementation on random data.

These are the tests that would gate the rewrite in review. The real
parity suite compares against SAS output; these prove the algebra.
"""

from __future__ import annotations

import itertools
import random

import pytest

from qrp.engine import Engine


# ---------------------------------------------------------------------
# Reference implementations (deliberately naive, one row at a time)
# ---------------------------------------------------------------------


def stockpile_sequential(claims):
    """SAS stockpiling algorithm 1, written the obvious way.

    claims: list of (adate_daynum, rxsup) sorted by adate.
    Returns list of (start, end) day numbers.
    """
    out = []
    prev_end = None
    for adate, rxsup in claims:
        start = adate if prev_end is None else max(adate, prev_end + 1)
        end = start + rxsup - 1
        out.append((start, end))
        prev_end = end
    return out


def findgap_bruteforce(claims, gap):
    """%ms_findgap washout, as a literal O(n^2) scan.

    claims: list of (adate, expiredt) day numbers, deduplicated by adate.
    Returns the set of adates that qualify as index dates.

    gap == 0 means "no washout": SAS returns the deduplicated frame
    before ever reaching the overlap join, so every claim qualifies.
    """
    if gap == 0:
        return {a for a, _ in claims}
    keep = set()
    for a, _ in claims:
        blocked = any(
            other_a != a
            and other_e >= a - gap
            and other_a <= a - 1
            for other_a, other_e in claims
        )
        if not blocked:
            keep.add(a)
    return keep


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def eng():
    e = Engine(verbose=False)
    yield e
    e.close()


def _random_claims(rng, n, max_sup=90, span=1500):
    dates = sorted(rng.sample(range(span), n))
    return [(d, rng.randint(1, max_sup)) for d in dates]


# ---------------------------------------------------------------------
# Stockpiling
# ---------------------------------------------------------------------


def test_stockpiling_closed_form_matches_sequential(eng):
    """The cumsum/running-max form must equal the sequential recurrence.

    Runs 200 random patients with overlapping supplies, which is the case
    that actually exercises the push-forward behaviour.
    """
    rng = random.Random(7)
    rows = []
    expected = []
    for patid in range(200):
        claims = _random_claims(rng, rng.randint(1, 25))
        for (a, r), (s, e) in zip(claims, stockpile_sequential(claims),
                                  strict=True):
            rows.append((patid, a, r))
            expected.append((patid, s, e))

    eng.con.execute("CREATE OR REPLACE TABLE t (patid INT, a INT, r INT)")
    eng.con.executemany("INSERT INTO t VALUES (?,?,?)", rows)

    got = eng.con.execute("""
        WITH running AS (
            SELECT patid, a, r,
                   sum(r) OVER w AS cum_sup,
                   a - (sum(r) OVER w - r) AS anchor
            FROM t
            WINDOW w AS (PARTITION BY patid ORDER BY a ROWS UNBOUNDED PRECEDING)
        ),
        solved AS (
            SELECT *, max(anchor) OVER (
                       PARTITION BY patid ORDER BY a ROWS UNBOUNDED PRECEDING
                   ) AS ranchor
            FROM running
        )
        SELECT patid,
               cum_sup - 1 + ranchor - r + 1 AS s,
               cum_sup - 1 + ranchor         AS e
        FROM solved ORDER BY patid, a
    """).fetchall()

    assert [tuple(g) for g in got] == expected


def test_stockpiling_preserves_total_supply(eng):
    """Stockpiling shifts dates; it must never create or destroy supply."""
    rng = random.Random(11)
    claims = _random_claims(rng, 40)
    out = stockpile_sequential(claims)
    assert sum(r for _, r in claims) == sum(e - s + 1 for s, e in out)


def test_stockpiling_output_never_overlaps():
    rng = random.Random(13)
    for _ in range(50):
        out = stockpile_sequential(_random_claims(rng, rng.randint(2, 30)))
        # pairwise: consecutive supplies must not overlap. `zip(out,
        # out[1:])` is intentionally ragged here, so strict= would be
        # wrong; itertools.pairwise says what is meant.
        for (_, e1), (s2, _) in itertools.pairwise(out):
            assert s2 > e1


# ---------------------------------------------------------------------
# Washout
# ---------------------------------------------------------------------


@pytest.mark.parametrize("gap", [0, 1, 30, 183, 365])
def test_findgap_running_max_matches_selfjoin(eng, gap):
    """The running-max washout must equal the O(n^2) overlap scan.

    This is the rewrite that removes the range self-join, so it is the
    one most worth pinning down.
    """
    rng = random.Random(100 + gap)
    rows = []
    expected = set()
    for patid in range(150):
        n = rng.randint(1, 20)
        dates = sorted(rng.sample(range(1200), n))
        claims = [(d, d + rng.randint(1, 120)) for d in dates]
        rows += [(patid, a, e) for a, e in claims]
        expected |= {(patid, a) for a in findgap_bruteforce(claims, gap)}

    eng.con.execute("CREATE OR REPLACE TABLE g (patid INT, a INT, e INT)")
    eng.con.executemany("INSERT INTO g VALUES (?,?,?)", rows)

    got = eng.con.execute(f"""
        WITH h AS (
            SELECT patid, a,
                   max(e) OVER (PARTITION BY patid ORDER BY a
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                       AS prior_max_e
            FROM g
        )
        SELECT patid, a FROM h
        WHERE {'TRUE' if gap == 0 else
               'prior_max_e IS NULL OR prior_max_e < a - ' + str(gap)}
    """).fetchall()

    assert set(got) == expected


# ---------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------


def test_age_years_is_birthday_aware(eng):
    """The /365.25 approximation the port used drifts near birthdays.

    This is the documented source of the age-bucket tolerance warnings.
    """
    q = "SELECT age_years(DATE '1960-06-15', CAST(? AS DATE))"
    assert eng.con.execute(q, ["2020-06-14"]).fetchone()[0] == 59
    assert eng.con.execute(q, ["2020-06-15"]).fetchone()[0] == 60
    assert eng.con.execute(q, ["2020-06-16"]).fetchone()[0] == 60
    # leap-day birthday
    q2 = "SELECT age_years(DATE '2000-02-29', CAST(? AS DATE))"
    assert eng.con.execute(q2, ["2021-02-28"]).fetchone()[0] == 20
    assert eng.con.execute(q2, ["2021-03-01"]).fetchone()[0] == 21


def test_periods_overlap_is_inclusive(eng):
    f = lambda a, b, c, d: eng.con.execute(
        f"SELECT periods_overlap(DATE '{a}', DATE '{b}', DATE '{c}', DATE '{d}')"
    ).fetchone()[0]
    assert f("2020-01-01", "2020-01-10", "2020-01-10", "2020-01-20")  # touch
    assert not f("2020-01-01", "2020-01-09", "2020-01-10", "2020-01-20")
    assert f("2020-01-05", "2020-01-06", "2020-01-01", "2020-01-31")  # contained


def test_span_days_is_inclusive(eng):
    v = eng.con.execute(
        "SELECT span_days(DATE '2020-01-01', DATE '2020-01-01')"
    ).fetchone()[0]
    assert v == 1
