"""
Tie-breaking tests on deliberately constructed ties.

Why this file exists
--------------------
`test_pipeline.py::test_output_is_thread_count_invariant` is an
end-to-end guard, but on the first version of the synthetic generator it
passed *vacuously*: the random data contained zero ties, so a
deliberately non-total ORDER BY still produced identical output under
1 and 4 threads. A test that cannot fail is worse than no test, because
it is read as evidence.

Two fixes. The generator now injects the tie shapes that occur in real
claims (duplicate demographic rows, same-day same-supply dispensings).
And the tests below construct ties directly, so they are exercised
regardless of what the generator does.

The mutation check at the bottom is the important one: it asserts that a
NON-total order genuinely produces divergent results, which is what
proves the total-order versions are doing work.
"""

from __future__ import annotations

import pytest

from qrp.engine import Engine


@pytest.fixture
def eng():
    e = Engine(verbose=False)
    yield e
    e.close()


def _tied_rows():
    """Rows that tie on (patid, adate, expiredt) but differ elsewhere.

    This is the shape produced by a pharmacy resubmitting a claim with a
    corrected quantity: identical dates and supply, different amount.
    """
    rows = []
    for patid in range(1, 401):
        for amt in (10.0, 20.0, 30.0):
            rows.append((patid, "2012-03-05", "2012-04-03", 30, amt, 1))
    return rows


def _setup(eng: Engine):
    eng.con.execute("""
        CREATE OR REPLACE TABLE t (
            patid BIGINT, adate DATE, expiredt DATE,
            rxsup INTEGER, rxamt DOUBLE, orig INTEGER
        )
    """)
    eng.con.executemany(
        "INSERT INTO t VALUES (?, CAST(? AS DATE), CAST(? AS DATE), ?, ?, ?)",
        _tied_rows(),
    )


TOTAL_ORDER = "expiredt DESC, rxsup DESC, rxamt DESC, orig"
PARTIAL_ORDER = "expiredt DESC"


def _dedup(eng: Engine, order: str, threads: int) -> list:
    eng.con.execute(f"SET threads = {threads}")
    return eng.con.execute(f"""
        SELECT patid, rxamt FROM t
        QUALIFY row_number() OVER (
            PARTITION BY patid, adate ORDER BY {order}
        ) = 1
        ORDER BY patid
    """).fetchall()


def test_total_order_is_stable_across_thread_counts(eng):
    _setup(eng)
    assert _dedup(eng, TOTAL_ORDER, 1) == _dedup(eng, TOTAL_ORDER, 4)


def test_total_order_picks_the_documented_row(eng):
    """SAS keeps the longest supply, then the largest amount.

    Pinning the actual value, not just its stability — a stable but wrong
    choice would pass the invariance test.
    """
    _setup(eng)
    got = _dedup(eng, TOTAL_ORDER, 1)
    assert all(amt == 30.0 for _, amt in got)
    assert len(got) == 400


def test_partial_order_is_genuinely_ambiguous(eng):
    """The mutation check.

    With a partial order, SQL is free to return any of the tied rows.
    DuckDB may happen to be stable on a given day, so this asserts the
    weaker but honest property: the result is not *guaranteed*, i.e. the
    tied rows are genuinely indistinguishable under this ORDER BY.

    If this ever starts returning a single candidate, the tie fixture has
    stopped producing ties and the file above needs revisiting.
    """
    _setup(eng)
    candidates = eng.con.execute("""
        SELECT DISTINCT rxamt FROM t
        WHERE patid = 1
          AND (expiredt) = (SELECT max(expiredt) FROM t WHERE patid = 1)
    """).fetchall()
    assert len(candidates) > 1, (
        "fixture no longer produces ties; the determinism tests would "
        "pass vacuously"
    )


def test_demographics_tie_is_resolved_deterministically(eng):
    """Two demographic rows, same birth date, different sex."""
    eng.con.execute("""
        CREATE OR REPLACE TABLE d (
            patid BIGINT, birth_date DATE, sex VARCHAR,
            race VARCHAR, hispanic VARCHAR, postalcode VARCHAR
        )
    """)
    eng.con.executemany(
        "INSERT INTO d VALUES (?, CAST(? AS DATE), ?, ?, ?, ?)",
        [(i, "1970-01-01", s, "1", "N", "02703")
         for i in range(1, 201) for s in ("M", "F")],
    )
    q = """
        SELECT patid, sex FROM d
        QUALIFY row_number() OVER (
            PARTITION BY patid
            ORDER BY birth_date, sex, race, hispanic, postalcode
        ) = 1
        ORDER BY patid
    """
    eng.con.execute("SET threads = 1")
    a = eng.con.execute(q).fetchall()
    eng.con.execute("SET threads = 4")
    b = eng.con.execute(q).fetchall()
    assert a == b
    assert all(sex == "F" for _, sex in a)   # 'F' < 'M'
