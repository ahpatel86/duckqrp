"""
Generate a synthetic Sentinel-CDM-shaped dataset for benchmarking.

Schemas match real SCDM extracts, verified against one. The dispensed
code column is `rx` (with `rx_codetype`), NOT `ndc` — an earlier version
of this generator used `ndc`, which is the code VOCABULARY rather than
the column name, and that error propagated into the pipeline where it
went undetected because every test ran against this same wrong data.
Synthetic fixtures reproduce your assumptions; they cannot falsify them.

Uses DuckDB itself to generate and write the parquet, so producing a
50-million-life dataset does not require holding it in Python memory.
Shapes match what the pipeline reads in 10_normalize.sql.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

# Exposure and outcome code sets the demo study refers to.
EXPOSURE_CODES = [f"E{i:05d}" for i in range(1, 41)]
EVENT_CODES = [f"D{i:05d}" for i in range(1, 21)]
NOISE_DRUG_CODES = [f"N{i:05d}" for i in range(1, 400)]
NOISE_DX_CODES = [f"X{i:05d}" for i in range(1, 800)]


def _lit(codes: list[str]) -> str:
    return "[" + ", ".join(f"'{c}'" for c in codes) + "]"


def generate(out: Path, n_patients: int, seed: int = 42) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for _t in ("demographic", "enrollment", "dispensing", "diagnosis", "death"):
        (out / _t).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SELECT setseed({(seed % 1000) / 1000.0})")

    con.execute(f"""
        CREATE OR REPLACE TABLE _pat AS
        SELECT
            i AS patid,
            DATE '1930-01-01' + CAST(random() * 30000 AS INTEGER) AS birth_date,
            CASE WHEN random() < 0.51 THEN 'F' ELSE 'M' END       AS sex,
            CAST(CAST(random() * 5 AS INTEGER) AS VARCHAR)         AS race,
            CASE WHEN random() < 0.15 THEN 'Y'
                 WHEN random() < 0.9  THEN 'N' ELSE 'U' END        AS hispanic,
            lpad(CAST(CAST(random() * 99999 AS INTEGER) AS VARCHAR), 5, '0') AS postalcode
        FROM range(1, {n_patients + 1}) t(i)
    """)

    con.execute(f"""
        COPY (
            SELECT patid, birth_date, sex, hispanic, race,
                   hispanic AS imputedhispanic, race AS imputedrace,
                   postalcode, DATE '2012-01-01' AS postalcode_date
            FROM _pat
        ) TO '{out / "demographic" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # 1-3 enrollment spans per patient, some with gaps.
    con.execute(f"""
        COPY (
            SELECT
                p.patid,
                DATE '2010-01-01'
                    + CAST(random() * 400 AS INTEGER)
                    + CAST((s.n - 1) * 900 AS INTEGER)         AS enr_start,
                DATE '2010-01-01'
                    + CAST(random() * 400 AS INTEGER)
                    + CAST((s.n - 1) * 900 AS INTEGER)
                    + 400 + CAST(random() * 450 AS INTEGER)    AS enr_end,
                'Y' AS medcov,
                CASE WHEN random() < 0.93 THEN 'Y' ELSE 'N' END AS drugcov,
                'Y' AS chart,
                'MC' AS plantype,
                'P'  AS payertype
            FROM _pat p
            CROSS JOIN range(1, 4) s(n)
            WHERE random() < CASE s.n WHEN 1 THEN 1.0 WHEN 2 THEN 0.55 ELSE 0.25 END
        ) TO '{out / "enrollment" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # ~14 dispensings per patient, ~22% of them study drugs.
    con.execute(f"""
        COPY (
            SELECT
                p.patid,
                DATE '2010-01-01' + CAST(random() * 2100 AS INTEGER) AS rxdate,
                CAST(p.patid * 7 % 9999 AS BIGINT) AS providerid,
                CASE WHEN random() < 0.22
                     THEN {_lit(EXPOSURE_CODES)}[
                            1 + CAST(random() * {len(EXPOSURE_CODES) - 1} AS INTEGER)]
                     ELSE {_lit(NOISE_DRUG_CODES)}[
                            1 + CAST(random() * {len(NOISE_DRUG_CODES) - 1} AS INTEGER)]
                END AS rx,
                'ND' AS rx_codetype,
                (10 + CAST(random() * 80 AS INTEGER))  AS rxsup,
                (30 + CAST(random() * 60 AS INTEGER))  AS rxamt
            FROM _pat p
            CROSS JOIN range(1, 15) d(n)
            WHERE random() < 0.94
        ) TO '{out / "dispensing" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # ~20 diagnoses per patient, ~6% of them outcome codes.
    con.execute(f"""
        COPY (
            SELECT
                p.patid,
                DATE '2010-01-01' + CAST(random() * 2100 AS INTEGER) AS adate,
                CASE WHEN random() < 0.06
                     THEN {_lit(EVENT_CODES)}[
                            1 + CAST(random() * {len(EVENT_CODES) - 1} AS INTEGER)]
                     ELSE {_lit(NOISE_DX_CODES)}[
                            1 + CAST(random() * {len(NOISE_DX_CODES) - 1} AS INTEGER)]
                END AS dx,
                '09' AS dx_codetype,
                CAST(p.patid * 13 % 99999 AS BIGINT) AS encounterid,
                CAST(p.patid * 7 % 9999 AS BIGINT)   AS providerid,
                CASE WHEN random() < 0.3 THEN 'P' ELSE 'S' END AS pdx,
                CASE WHEN random() < 0.2 THEN 'IP' ELSE 'AV' END AS enctype,
                NULL AS origdx,
                NULL AS padmit
            FROM _pat p
            CROSS JOIN range(1, 21) d(n)
            WHERE random() < 0.95
        ) TO '{out / "diagnosis" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    con.execute(f"""
        COPY (
            SELECT patid,
                   DATE '2011-01-01' + CAST(random() * 1800 AS INTEGER) AS deathdt,
                   'N' AS dtimpute, 'D' AS source, 'E' AS confidence
            FROM _pat WHERE random() < 0.04
        ) TO '{out / "death" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # ---- deliberate tie injection -------------------------------------
    # Real claims data contains exact duplicates and same-day/same-supply
    # dispensings; random generation does not. Without them the
    # tie-breaking clauses in the pipeline are never exercised, and the
    # determinism tests pass vacuously (verified: a deliberately
    # non-total ORDER BY still passed on untied data).
    con.execute(f"""
        COPY (
            SELECT * FROM read_parquet('{out / "dispensing" / "data.parquet"}')
            UNION ALL
            -- same patient, same day, same supply, different amount
            SELECT patid, rxdate, providerid, rx, rx_codetype, rxsup, rxamt + 1
            FROM read_parquet('{out / "dispensing" / "data.parquet"}')
            WHERE patid % 20 = 0
        ) TO '{out / "dispensing" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.execute(f"""
        COPY (
            SELECT * FROM read_parquet('{out / "demographic" / "data.parquet"}')
            UNION ALL
            -- same patient, same birth date, different sex: a genuine tie
            SELECT patid, birth_date,
                   CASE WHEN sex = 'F' THEN 'M' ELSE 'F' END,
                   hispanic, race, imputedhispanic, imputedrace,
                   postalcode, postalcode_date
            FROM read_parquet('{out / "demographic" / "data.parquet"}')
            WHERE patid % 50 = 0
        ) TO '{out / "demographic" / "data.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    size = sum(f.stat().st_size for f in out.rglob("*.parquet"))
    # procedure and lab_result. Both are mainstream in real studies —
    # PX is 150 of 1,124 cohort codes in the production input file seen,
    # and the lab schema was verified against a real 1.4M-row extract —
    # so a fixture without them silently skips those code paths, and the
    # table-resolution tests fall back to column fingerprinting.
    (out / "procedure").mkdir(exist_ok=True)
    con.execute(f"""COPY (
      SELECT d.patid,
             DATE '2010-01-01' + CAST(random()*2000 AS INTEGER) AS adate,
             'P' || lpad((CAST(random()*19 AS INTEGER)+1)::VARCHAR, 5, '0') AS px,
             'C4' AS px_codetype,
             ['IP','AV','ED'][1 + CAST(random()*2 AS INTEGER)] AS enctype,
             CAST(d.patid*13 % 99999 AS BIGINT) AS encounterid,
             CAST(d.patid*7 % 9999 AS BIGINT) AS providerid,
             NULL AS origpx
      FROM read_parquet('{out / "demographic"}/**/*.parquet') d, range(1,4)
    ) TO '{out / "procedure" / "data.parquet"}' (FORMAT PARQUET)""")

    # Real SCDM lab: NO lab_code column — LAB01 matches a seven-attribute
    # combination. result_dt/order_dt are NULL, as in the real extract,
    # so the LABDATETYPE fall-through is exercised.
    (out / "lab_result").mkdir(exist_ok=True)
    con.execute(f"""COPY (
      SELECT d.patid,
             DATE '2010-01-01' + CAST(random()*2000 AS INTEGER) AS lab_dt,
             NULL::DOUBLE AS result_dt, NULL::DOUBLE AS order_dt,
             round(random()*200, 2) AS ms_result_n, '' AS ms_result_c,
             ['N','U','C'][1 + CAST(random()*2 AS INTEGER)] AS result_type,
             ['2160-0','3094-0','2823-3'][1 + CAST(random()*2 AS INTEGER)] AS loinc,
             ['PX1','PX2','PX3'][1 + CAST(random()*2 AS INTEGER)] AS px,
             ['CREATININE','SODIUM','GLUCOSE'][1 + CAST(random()*2 AS INTEGER)] AS ms_test_name,
             '' AS ms_test_sub_category, 'SR_PLS' AS specimen_source,
             ['MG/DL','MMOL/L'][1 + CAST(random()*1 AS INTEGER)] AS ms_result_unit,
             ['X','R','F'][1 + CAST(random()*2 AS INTEGER)] AS fast_ind,
             'O' AS pt_loc
      FROM read_parquet('{out / "demographic"}/**/*.parquet') d, range(1,4)
    ) TO '{out / "lab_result" / "data.parquet"}' (FORMAT PARQUET)""")

    counts = {
        t: con.execute(
            f"SELECT count(*) FROM read_parquet('{out / t}/**/*.parquet')"
        ).fetchone()[0]
        for t in ("demographic", "enrollment", "dispensing", "diagnosis",
                  "death", "procedure", "lab_result")
    }
    print(f"generated {n_patients:,} patients -> {out}  ({size / 1e6:.1f} MB)")
    for t, n in counts.items():
        print(f"  {t:<14} {n:>12,} rows")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--patients", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    generate(Path(a.out), a.patients, a.seed)
