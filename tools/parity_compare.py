"""Compare a SAS parity dump against a DuckDB one.

    python tools/parity_compare.py SAS_DIR DUCK_DIR [--tolerance 1e-6]

The default relative tolerance, 1e-6, absorbs SAS's lower-precision
storage of some reals (34.5333251953125 vs 34.5333333...) while still
flagging real differences, such as means rounded to 4 decimals.

`qrp run --parity-dump` writes the DuckDB side. This reads both trees,
matches tables by their path, and reports where they differ.

Why this exists
---------------
Every source of ground truth applied to this package has found defects:
reading the SAS macros found eight divergences, real lab data found four
more, a real input file found four more, a code review found thirteen,
and a sweep of the outputs found that EVERY output table was wrong in
some way. Reading has repeatedly failed, including on claims already
believed verified.

SAS's actual output is the one source of truth never applied. This is
the half of the harness that applies it.

What it reports
---------------
Differences are classified, because they are not equally serious:

  MISSING/EXTRA TABLE   a table one side produced and the other did not
  MISSING/EXTRA COLUMN  a column mismatch — the commonest defect found
                        in this package, and invisible to a row count
  ROW COUNT             different numbers of rows
  KEY MISMATCH          same count, different keys present
  VALUE                 same key, different value

A row count alone is a weak check: two tables can have identical counts
and disagree on every row. Keys are compared before values for that
reason.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

# Key columns per table, used to align rows before comparing values.
# Without these a diff can only say "these files differ", which is not
# actionable on a 60,000-row master list.
# Keys use SAS's column names; DuckDB's are mapped onto them (ALIASES).
KEYS: dict[str, tuple[str, ...]] = {
    "stockpiled": ("group", "patid", "adate"),
    "index_candidates": ("group", "patid", "adate"),
    "pov1": ("group", "patid", "indexdt"),
    "ptsmasterlist": ("group", "patid", "indexdt"),
    "cohort_final": ("group", "patid", "indexdt"),
    "attrition": ("group", "level"),
    "denominators": ("group", "agegroup", "sex", "index_year"),
    "censoring": ("group", "level", "censdays_value_cat"),
    "followuptime": ("group", "level", "fupdays_value_cat"),
    "t2_cida": ("group", "level", "agegroup", "sex"),
    "numcounts": ("group", "level", "agegroup", "sex"),
    "denomcounts": ("group", "level", "agegroup", "sex"),
    "distindex": ("group", "distindextype", "distindexlist"),
    # by id: stockgroup rows carry no code, so (type, code) collided
    "distindexmap": ("group", "distindextype", "distindexid"),
    "baseline": ("group",),
    # by code, not rank: SAS orders tied codes in no reproducible way
    "mfu": ("group", "analysisnum", "code"),
}

# DuckDB column names that mean the same as SAS's. Without this the
# master list was keyed on `cohortgrp`, which SAS calls `Group`: every
# SAS row got a blank key and no row was actually compared.
ALIASES: dict[str, str] = {"cohortgrp": "group"}

_MON = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def norm(v: str | None) -> str:
    """One canonical form for a cell, so keys and values compare.

    SAS's CSV export writes dates in their display format (01JAN2020,
    01/01/2020) and numbers as floats (1.0); DuckDB writes ISO dates and
    integers. Codes with a leading zero are left alone.
    """
    s = (v or "").strip()
    if s in (".", "NA", "NULL", "NaN", "nan"):
        return ""
    u = s.upper()
    if len(u) == 9 and u[:2].isdigit() and u[2:5] in _MON and u[5:].isdigit():
        return f"{u[5:]}-{_MON[u[2:5]]:02d}-{u[:2]}"                    # DATE9.
    if len(s) == 10 and s[2] == "/" and s[5] == "/" and s.replace("/", "").isdigit():
        return f"{s[6:]}-{s[:2]}-{s[3:5]}"                              # MMDDYY10.
    if len(s) >= 19 and s[4] == "-" and s[10] in " T" and s[11:19] == "00:00:00":
        return s[:10]                                                   # midnight timestamp
    if s[:1] == "0" and len(s) > 1 and s[1:2].isdigit():
        return s                                                        # a code, e.g. 0636
    try:
        f = float(s)
    except ValueError:
        return s
    if math.isfinite(f) and f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return s

# Columns with no SAS counterpart, or whose difference is not a defect.
IGNORE: dict[str, frozenset[str]] = {
    "stockpiled": frozenset({"orig_adate"}),
    "cohort_final": frozenset({"exit_reason"}),
}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Rows with lower-cased, aliased column names and normalised cells."""
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        names = {c: ALIASES.get(c.lower(), c.lower()) for c in reader.fieldnames or []}
        rows = [{names[k]: norm(v) for k, v in r.items() if k in names}
                for r in reader]
        return list(names.values()), rows


def find_tables(root: Path) -> dict[str, Path]:
    """Map a logical path -> file, keyed on the part below the root.

    Keyed on the relative path rather than the filename so two cohorts'
    copies of the same table do not collide.
    """
    out: dict[str, Path] = {}
    for pattern in ("*.csv", "*.parquet"):
        for path in root.rglob(pattern):
            out[str(path.relative_to(root))] = path
    return out


def same_value(a: str, b: str, tol: float) -> bool:
    """Compare cell values, tolerating float and date-format noise.

    SAS writes dates and floats differently from DuckDB, and a harness
    that flags every such difference reports thousands of false
    positives and gets ignored — which is worse than not running it.
    """
    if a == b:
        return True
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    # missing: SAS writes '.', DuckDB writes ''
    if a in ("", ".", "NA", "NULL") and b in ("", ".", "NA", "NULL"):
        return True
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return False
    if math.isnan(fa) and math.isnan(fb):
        return True
    return abs(fa - fb) <= tol * max(1.0, abs(fa), abs(fb))


def compare_table(name: str, sas: Path, duck: Path,
                  tol: float, max_examples: int
                  ) -> tuple[list[str], list[str]]:
    sas_cols, sas_rows = read_csv(sas)
    duck_cols, duck_rows = read_csv(duck)
    table = Path(name).stem
    ignore = IGNORE.get(table, frozenset())

    issues: list[str] = []
    notes: list[str] = []
    sas_set = {c.lower() for c in sas_cols} - ignore
    duck_set = {c.lower() for c in duck_cols} - ignore

    missing = sorted(sas_set - duck_set)
    extra = sorted(duck_set - sas_set)
    if missing:
        issues.append(f"MISSING COLUMN  {name}: {missing}")
    if extra:
        # A note: SAS's table is the contract, and DuckDB's master list
        # legitimately carries internal columns beyond SAS's.
        notes.append(f"EXTRA COLUMN    {name}: {extra}")

    if len(sas_rows) != len(duck_rows):
        issues.append(
            f"ROW COUNT       {name}: sas={len(sas_rows):,} "
            f"duck={len(duck_rows):,} (diff {len(duck_rows)-len(sas_rows):+,})"
        )

    key = [k for k in KEYS.get(table, ()) if k in duck_set and k in sas_set]
    shared = sorted(sas_set & duck_set - set(key))
    if not key:
        # No usable key: fall back to positional comparison, and say so,
        # because a positional diff on unordered data is meaningless.
        # A note, not a difference: it explains HOW the comparison was
        # done. Counting it as a difference made a tree compared against
        # itself report six, and a tool that cries wolf on identical
        # input stops being run.
        notes.append(f"NOTE            {name}: no key columns; "
                     f"comparing positionally")
        for i, (sr, dr) in enumerate(zip(sas_rows, duck_rows,
                                            strict=False)):
            for col in shared:
                if not same_value(sr.get(col, ""), dr.get(col, ""), tol):
                    issues.append(f"VALUE           {name}[{i}].{col}: "
                                  f"sas={sr.get(col)!r} duck={dr.get(col)!r}")
                    if len(issues) > max_examples:
                        return issues, notes
        return issues, notes

    def keyed(rows):
        out = {}
        for r in rows:
            out[tuple(r.get(k, "") for k in key)] = r
        return out

    sas_by, duck_by = keyed(sas_rows), keyed(duck_rows)
    only_sas = sorted(set(sas_by) - set(duck_by))[:max_examples]
    only_duck = sorted(set(duck_by) - set(sas_by))[:max_examples]
    if only_sas:
        issues.append(f"KEY MISMATCH    {name}: {len(set(sas_by)-set(duck_by)):,} "
                      f"keys only in SAS, e.g. {only_sas[:3]}")
    if only_duck:
        issues.append(f"KEY MISMATCH    {name}: {len(set(duck_by)-set(sas_by)):,} "
                      f"keys only in DuckDB, e.g. {only_duck[:3]}")

    # Value differences, counted per column so one systematic error does
    # not drown the report in thousands of identical lines.
    per_col: dict[str, int] = defaultdict(int)
    examples: dict[str, tuple] = {}
    for k in set(sas_by) & set(duck_by):
        sr, dr = sas_by[k], duck_by[k]
        for col in shared:
            if not same_value(sr.get(col, ""), dr.get(col, ""), tol):
                per_col[col] += 1
                examples.setdefault(col, (k, sr.get(col), dr.get(col)))
    for col, n in sorted(per_col.items(), key=lambda x: -x[1]):
        k, sv, dv = examples[col]
        issues.append(f"VALUE           {name}.{col}: {n:,} rows differ, "
                      f"e.g. key={k} sas={sv!r} duck={dv!r}")
    return issues, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sas_dir")
    ap.add_argument("duck_dir")
    ap.add_argument("--tolerance", type=float, default=1e-6,
                    help="relative tolerance for numeric comparison")
    ap.add_argument("--max-examples", type=int, default=20,
                    help="cap on reported examples per table")
    args = ap.parse_args()

    sas_root, duck_root = Path(args.sas_dir), Path(args.duck_dir)
    for root in (sas_root, duck_root):
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            return 2

    sas_tables = find_tables(sas_root)
    duck_tables = find_tables(duck_root)

    issues: list[str] = []
    notes: list[str] = []
    for name in sorted(set(sas_tables) - set(duck_tables)):
        issues.append(f"MISSING TABLE   {name}: in SAS, absent from DuckDB")
    for name in sorted(set(duck_tables) - set(sas_tables)):
        # a note: the SAS export skips tables it did not produce
        notes.append(f"EXTRA TABLE     {name}: in DuckDB, absent from SAS")

    shared = sorted(set(sas_tables) & set(duck_tables))
    for name in shared:
        found, said = compare_table(name, sas_tables[name],
                                    duck_tables[name],
                                    args.tolerance, args.max_examples)
        issues.extend(found)
        notes.extend(said)

    print(f"tables: {len(sas_tables)} SAS, {len(duck_tables)} DuckDB, "
          f"{len(shared)} compared")
    print()
    for line in notes:
        print(line)
    if notes:
        print()
    if not issues:
        print("No differences.")
        return 0
    for line in issues:
        print(line)
    print()
    print(f"{len(issues)} difference(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
