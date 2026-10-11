"""
Reading real QRP input files.

The indirection that matters
----------------------------
The JSON produced by `tools/SAS2JSON/create_json.sas` does NOT use fixed
top-level keys. `PROC JSON` writes each table under the *name of the SAS
dataset*, which is whatever the study author set:

    %let COHORTFILE = anmod_mpl1r_cohortfile_v3;
    ...
    write value "&COHORTFILE.";     ->  JSON key "anmod_mpl1r_cohortfile_v3"

The logical-name to actual-name mapping lives in the `QRP_PARAMETERS`
table, which is a key/value table with columns `parameter` and `run1`
(`run2`, `run3`, ... for multi-run studies):

    parameter    | run1
    -------------|---------------------------
    cohortfile   | anmod_mpl1r_cohortfile_v3
    type2file    | anmod_mpl1r_type2_v3
    runid        | mpl1r
    startdate    | 18628

The PySpark port resolved this with:

    DATAFRAMES = {row["parameter"]: globals().get(row["run1"]) ...}

My first loader assumed literal keys (`"cohortfile"`, `"type2file"`) and
would silently produce a zero-cohort study on a real file. `resolve()`
below does the indirection properly, and `describe()` reports exactly
what was found so a mismatch is loud rather than silent.

Dates
-----
`create_json.sas` deliberately strips date FORMATs so PROC JSON emits
SAS date values as integers (days since 1960-01-01), not '30SEP2015'.
Both representations are accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SAS_EPOCH = date(1960, 1, 1)

# Logical names this implementation understands. Anything else in the
# file is preserved but unused, and reported by describe().
KNOWN_TABLES = (
    "qrp_parameters",
    "cohortfile",
    "cohortcodes",
    "type2file",
    "monitoringfile",
    "inclusioncodes",
    "covariatecodes",
    "userstrata",
    "stockpilingfile",
    "zipfile",
    "riskscorefile",
    "riskscorecodes",
    "utilfile",
    "mfufile",
    "drugclassfile",
    "labcodesmap",
    "labcodes",
    # Not a real QRP table. In the SAS package, dispensing strength comes
    # from the drugclass lookup keyed on NDC. Until that lookup is wired
    # in, a two-column (code, strength) table can be supplied directly.
    "codestrength",
)

# Tables the Type 2 path actually consumes today.
REQUIRED_TABLES = ("qrp_parameters", "cohortfile", "type2file", "cohortcodes")

# Tables recognised but not yet implemented — listed so a study using
# them gets a warning instead of a silently narrower result.
UNIMPLEMENTED_TABLES = (
    "stockpilingfile",
    "riskscorefile",
    "utilfile",
    "mfufile",
)


def sas_date(value: Any) -> date | None:
    """Accept a SAS date integer, an ISO string, or a date."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return date.fromordinal(SAS_EPOCH.toordinal() + int(value))
    s = str(value).strip()
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return date.fromordinal(SAS_EPOCH.toordinal() + int(s))
    return date.fromisoformat(s[:10])


@dataclass
class InputFile:
    """A parsed QRP input file, with logical names resolved."""

    path: Path
    raw: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]   # logical name -> rows
    aliases: dict[str, str]                    # logical name -> JSON key
    scalars: dict[str, Any]                    # qrp_parameters scalars
    unresolved: list[str] = field(default_factory=list)
    extra_keys: list[str] = field(default_factory=list)

    def rows(self, logical: str) -> list[dict[str, Any]]:
        return self.tables.get(logical, [])


def _norm_rows(value: Any) -> list[dict[str, Any]]:
    """Lowercase every column name; PROC JSON preserves SAS casing."""
    if not isinstance(value, list):
        return []
    out = []
    for r in value:
        if isinstance(r, dict):
            out.append({str(k).lower(): v for k, v in r.items()})
    return out


def load(path: str | Path, lookup: str | Path | None = None) -> InputFile:
    """Load a QRP input file, resolving the qrp_parameters indirection."""
    path = Path(path)
    raw = json.loads(path.read_text())
    # PROC JSON writes keys in SAS casing; index case-insensitively.
    by_key = {str(k).lower(): v for k, v in raw.items()}

    if lookup:
        for k, v in json.loads(Path(lookup).read_text()).items():
            by_key.setdefault(str(k).lower(), v)

    params = _norm_rows(by_key.get("qrp_parameters"))

    # qrp_parameters is key/value: parameter + run1 (or run2, run3...).
    run_col = next(
        (c for c in ("run1", "run", "value")
         if params and c in params[0]),
        "run1",
    )
    scalars: dict[str, Any] = {}
    for r in params:
        name = str(r.get("parameter") or "").strip().lower()
        if name:
            scalars[name] = r.get(run_col)

    tables: dict[str, list[dict[str, Any]]] = {
        "qrp_parameters": params
    }
    aliases: dict[str, str] = {"qrp_parameters": "QRP_PARAMETERS"}
    unresolved: list[str] = []
    used_keys = {"qrp_parameters"}

    for logical in KNOWN_TABLES:
        if logical == "qrp_parameters":
            continue
        # 1. the dataset name given in qrp_parameters
        alias = scalars.get(logical)
        candidates = []
        if alias:
            candidates.append(str(alias).strip().lower())
        # 2. the logical name itself, for hand-written files like the demos
        candidates.append(logical)

        for cand in candidates:
            if cand in by_key and isinstance(by_key[cand], list):
                tables[logical] = _norm_rows(by_key[cand])
                aliases[logical] = cand
                used_keys.add(cand)
                break
        else:
            if alias:
                # named in qrp_parameters but the array is absent
                unresolved.append(f"{logical} -> {alias!r} (not in JSON)")

    extra = sorted(k for k in by_key if k not in used_keys)
    return InputFile(
        path=path,
        raw=raw,
        tables=tables,
        aliases=aliases,
        scalars=scalars,
        unresolved=unresolved,
        extra_keys=extra,
    )


def describe(inp: InputFile) -> str:
    """Human-readable report of what was found.

    Printed by `qrp inspect`. The point is that a study whose tables did
    not resolve should fail loudly here rather than quietly producing an
    empty cohort list.
    """
    lines = [f"input file: {inp.path}", ""]

    lines.append("qrp_parameters scalars:")
    interesting = (
        "runid", "type", "startdate", "enddate", "censordate",
        "periodidstart", "periodidend", "run_envelope",
    )
    for k in interesting:
        if k in inp.scalars:
            v = inp.scalars[k]
            extra = ""
            if k.endswith("date"):
                try:
                    extra = f"  ({sas_date(v)})"
                except Exception:
                    extra = "  (unparseable)"
            lines.append(f"  {k:<16} {v}{extra}")
    lines.append("")

    lines.append(f"{'table':<20}{'json key':<38}{'rows':>7}  columns")
    lines.append("-" * 100)
    for logical in KNOWN_TABLES:
        if logical == "qrp_parameters":
            continue
        rows = inp.tables.get(logical)
        if rows is None:
            mark = "REQUIRED, MISSING" if logical in REQUIRED_TABLES else "-"
            lines.append(f"{logical:<20}{mark:<38}{'':>7}")
            continue
        cols = sorted(rows[0].keys()) if rows else []
        shown = ", ".join(cols[:8]) + (" ..." if len(cols) > 8 else "")
        note = ""
        if logical in UNIMPLEMENTED_TABLES:
            note = "  [present but NOT YET IMPLEMENTED]"
        lines.append(
            f"{logical:<20}{inp.aliases.get(logical, ''):<38}"
            f"{len(rows):>7}  {shown}{note}"
        )

    if inp.unresolved:
        lines += ["", "UNRESOLVED (named in qrp_parameters, absent from JSON):"]
        lines += [f"  {u}" for u in inp.unresolved]

    if inp.extra_keys:
        lines += ["", "JSON keys not mapped to any known table:"]
        lines += [f"  {k}" for k in inp.extra_keys]

    missing = [t for t in REQUIRED_TABLES if not inp.tables.get(t)]
    lines += [""]
    if missing:
        lines.append(f"RESULT: cannot run — missing {', '.join(missing)}")
    else:
        lines.append(
            f"RESULT: usable — {len(inp.rows('cohortfile'))} cohort row(s), "
            f"{len(inp.rows('cohortcodes'))} code row(s), "
            f"{len(inp.rows('covariatecodes'))} covariate row(s)"
        )
    return "\n".join(lines)
