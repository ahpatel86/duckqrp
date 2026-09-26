"""Compare a SAS parity dump against a DuckDB one.

    python tools/parity_compare.py SAS_DIR DUCK_DIR [--tolerance 1e-9]

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
KEYS: dict[str, tuple[str, ...]] = {
    "stockpiled": ("cohortgrp", "patid", "adate"),
    "index_candidates": ("cohortgrp", "patid", "adate"),
    "pov1": ("cohortgrp", "patid", "indexdt"),
    "ptsmasterlist": ("cohortgrp", "patid", "indexdt"),
    "cohort_final": ("cohortgrp", "patid", "indexdt"),
    "attrition": ("group", "level"),
    "denominators": ("cohortgrp", "agegroup", "sex", "index_year"),
    "censoring": ("group", "level", "censdays_value_cat"),
    "followuptime": ("group", "level", "fupdays_value_cat"),
    "t2_cida": ("group", "level", "agegroup", "sex"),
    "numcounts": ("group", "level", "agegroup", "sex"),
    "denomcounts": ("group", "level", "agegroup", "sex"),
    "distindex": ("group", "distindextype", "distindexlist"),
    "distindexmap": ("group", "distindextype", "code"),
}

# Columns with no SAS counterpart, or whose difference is not a defect.
IGNORE: dict[str, frozenset[str]] = {
    "stockpiled": frozenset({"orig_adate"}),
    "cohort_final": frozenset({"exit_reason"}),
}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


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
        issues.append(f"EXTRA COLUMN    {name}: {extra}")

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
    ap.add_argument("--tolerance", type=float, default=1e-9,
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
    for name in sorted(set(sas_tables) - set(duck_tables)):
        issues.append(f"MISSING TABLE   {name}: in SAS, absent from DuckDB")
    for name in sorted(set(duck_tables) - set(sas_tables)):
        issues.append(f"EXTRA TABLE     {name}: in DuckDB, absent from SAS")

    notes: list[str] = []
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
