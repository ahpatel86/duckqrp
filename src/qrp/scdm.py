"""
SCDM schema expectations, and a probe that checks a real dataset.

What this implementation reads
------------------------------
Only the columns listed below are touched. Everything else in the SCDM
table is ignored and, because DuckDB pushes projection into the parquet
reader, never read off disk.

Two things make this more forgiving than the PySpark port:

* **DuckDB identifiers are case-insensitive.** `PatID`, `patid` and
  `PATID` all resolve to the same column, so the ~130 lowercase-map
  rebuilds the port needed are simply unnecessary. Only genuine *name*
  differences (`dx_codetype` vs `dxcodetype`) need aliasing.

* **Dates are coerced once.** `10_normalize.sql` casts to DATE, so
  DATE, TIMESTAMP, ISO strings and SAS date integers all work provided
  the column is nameable.

`probe()` reports what a real dataset actually contains against this
list, so a schema mismatch is found in seconds rather than three stages
into a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import duckdb


@dataclass(frozen=True)
class TableSpec:
    name: str                       # canonical table name
    required: tuple[str, ...]       # columns the pipeline reads
    optional: tuple[str, ...] = ()
    needed_by: str = ""
    used: bool = True               # False = read by SAS QRP, not by this
    aliases: tuple[str, ...] = ()   # other names sites use for this table
    # Column-level synonyms: canonical -> names seen in the wild. The
    # dispensed-code column is `rx` in SCDM; an earlier version of this
    # file required `ndc`, which is the NDC vocabulary rather than the
    # column, so real dispensing data failed to validate.
    #
    # An empty mapping, not None: `dict = None` with a type: ignore was
    # three faults in one line — untyped container, wrong default for
    # the annotation, and a silenced checker instead of a fix. Every
    # reader then had to write `spec.column_aliases or {}`.
    column_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict)
    # A site with no death file should still be able to run, with death
    # censoring simply never triggering — better than refusing.
    optional_table: bool = False


SCDM: tuple[TableSpec, ...] = (
    TableSpec(
        "enrollment",
        ("patid", "enr_start", "enr_end", "medcov", "drugcov"),
        ("chart",),
        "enrollment spans, all eligibility windows",
        aliases=("enrol", "enroll", "enr"),
    ),
    TableSpec(
        "demographic",
        ("patid", "birth_date", "sex"),
        ("race", "hispanic", "postalcode", "postalcode_date"),
        "age strata, demographic eligibility, stratified output",
        aliases=("demographics", "demog", "dem"),
    ),
    TableSpec(
        "dispensing",
        ("patid", "rxdate", "rx", "rxsup"),
        ("rxamt", "rx_codetype"),
        "exposure extraction, stockpiling, episodes; rxamt only for dose",
        aliases=("dispense", "dis"),
        column_aliases={"rx": ("ndc", "rxcode", "code")},
    ),
    TableSpec(
        "diagnosis",
        ("patid", "adate", "dx"),
        ("dx_codetype", "pdx", "enctype"),
        "outcome events, DX covariates",
        aliases=("diag", "dia", "dx"),
    ),
    TableSpec(
        "death",
        ("patid", "deathdt"),
        (),
        "death censoring; an empty table is fine",
        aliases=("deaths", "dth"),
        optional_table=True,
    ),
    TableSpec(
        "procedure",
        ("patid", "adate", "px"),
        ("px_codetype", "enctype", "encounterid", "providerid", "origpx"),
        "PX codes on cohort, covariate and inclusion definitions",
        aliases=("proc", "procedures"),
        optional_table=True,
    ),
    TableSpec("encounter", (), (), "care setting, encounter-based events", used=False),
    TableSpec(
        "lab_result",
        # Verified against a real SCDM lab extract. There is NO
        # `lab_code` column — LAB01 matches a seven-attribute
        # combination, not a code (ms_extractlabs.sas:251-258). Only
        # patid and lab_dt are genuinely required; everything the three
        # extraction paths need is optional because a study uses one
        # path, not all three.
        ("patid", "lab_dt"),
        ("result_dt", "order_dt", "ms_result_n", "ms_result_c",
         "result_type", "loinc", "px", "ms_test_name",
         "ms_test_sub_category", "specimen_source", "ms_result_unit",
         "fast_ind", "pt_loc"),
        "lab result covariates; optional",
        aliases=("labs", "lab", "lab_results"),
        optional_table=True,
    ),
)


# Layouts a site might reasonably use. Tried in order; the first that
# matches wins. Resolving here rather than hardcoding one glob in the SQL
# means the layout is a property of the site's data, not a requirement
# this tool imposes on it.
#
#   <root>/enrollment/**/*.parquet     directory, possibly hive-partitioned
#   <root>/enrollment.parquet          one file per table
#   <root>/enrollment_001.parquet      several files per table
#   <root>/ENROLLMENT.parquet          any case
#   <root>/enrollment/*.csv            csv, if that is what the site has
_PATTERNS = (
    "{name}/**/*.parquet",
    "{name}.parquet",
    "{name}_*.parquet",
    "{name}-*.parquet",
    "{name}*.parquet",
    "{name}/**/*.csv",
    "{name}.csv",
)


def check_table_map(table_map: Mapping[str, str] | None) -> None:
    """Reject a --table-map key that names no SCDM table.

    A misspelt key — `dispensng=...` — was silently IGNORED: the
    override was dropped, the real table was reported missing, and
    nothing said the key had not been recognised. The operator believed
    they had overridden it. That is worse than an error, because it
    looks like a data problem rather than a typo.
    """
    if not table_map:
        return
    import difflib

    known = {t.name for t in SCDM}
    for key in table_map:
        if key in known:
            continue
        near = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
        hint = f" Did you mean '{near[0]}'?" if near else ""
        raise ValueError(
            f"--table-map names '{key}', which is not an SCDM table this "
            f"package reads.{hint}\n  Valid names: {', '.join(sorted(known))}"
        )


def resolve_table(root: str | Path, spec: "TableSpec | str",
                  table_map: dict[str, str] | None = None,
                  by_columns: dict[str, list[tuple[str, str]]] | None = None,
                  ) -> str | None:
    """Find the read pattern for one table, or None.

    Three strategies, in order of how much they trust:

    1. **Explicit mapping** — the operator said which file it is. Always
       wins; nothing should override a human being.
    2. **Name matching** — the canonical name or a known alias, in any
       of the supported layouts, case-insensitively.
    3. **Column matching** — identify the table by its schema. This is
       what handles site-specific names like `dp042_enr_2024q1`, and it
       is only used when the name gave nothing, so a correctly named
       file is never second-guessed.
    """
    root = Path(root)
    name_key = spec if isinstance(spec, str) else spec.name

    if table_map and name_key in table_map:
        given = table_map[name_key]
        path = Path(given)
        if not path.is_absolute():
            path = root / given
        if path.is_dir():
            return str(path / "**" / "*.parquet")
        if path.exists() or "*" in given:
            return str(path)
        # Say WHERE it looked. An absolute path was never "under" the
        # input folder, and saying so sent people to the wrong place.
        where = (f"'{path}'" if Path(given).is_absolute()
                 else f"'{given}' relative to the input folder, i.e. '{path}'")
        raise FileNotFoundError(
            f"--table-map {name_key}=... points at {where}, which does not "
            f"exist.\n  Check the path. An absolute path is used as-is; a "
            f"relative one is resolved against --indata ({root})."
        )
    if isinstance(spec, str):
        names = [spec]
    else:
        names = [spec.name, *spec.aliases]

    # Build a case-insensitive index of what is actually there once,
    # rather than stat-ing every combination.
    try:
        actual = {e.name.lower(): e.name for e in root.iterdir()}
    except OSError:
        return None

    for name in names:
        for pattern in _PATTERNS:
            filled = pattern.format(name=name)
            head = filled.split("/")[0]
            if "*" in head and "/" not in filled:
                # Several files for one table: enrollment_001.parquet,
                # enrollment-a.parquet, enrollmentX.parquet. Match them
                # case-insensitively and hand DuckDB a real glob.
                import fnmatch

                matches = sorted(
                    real for low, real in actual.items()
                    if fnmatch.fnmatch(low, head.lower())
                )
                if matches:
                    # Preserve the on-disk casing of the common prefix so
                    # the glob works on case-sensitive filesystems.
                    prefix = matches[0][: len(name)]
                    ext = matches[0].rsplit(".", 1)[-1]
                    return str(root / f"{prefix}*.{ext}")
                continue
            real = actual.get(head.lower())
            if real is None:
                continue
            candidate = filled.replace(head, real, 1)
            full = root / candidate
            if "*" in candidate:
                import glob as _glob

                if _glob.glob(str(full), recursive=True):
                    return str(full)
            elif full.exists():
                return str(full)

    # Nothing matched by name — fall back to identifying by columns.
    if by_columns is not None and not isinstance(spec, str):
        hits = by_columns.get(spec.name, [])
        if len(hits) == 1:
            return hits[0][1]
    return None


# ---------------------------------------------------------------------
# Content-based identification
# ---------------------------------------------------------------------


def _candidate_sources(root: Path) -> dict[str, str]:
    """Every readable thing under `root`, as name -> read pattern.

    One entry per directory (treated as a partitioned table) and one per
    loose file, with numbered siblings collapsed into a single glob so
    `enr_001.parquet`…`enr_009.parquet` is considered once.
    """
    import re

    out: dict[str, str] = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out

    # Group loose files by a zero-padded numeric suffix so that
    # enr_001..enr_009 is considered once. Two rules keep this safe:
    #
    #   * only a SEPARATED, ZERO-PADDED suffix counts (`_001`, `-02`).
    #     `PT_MASTER_V3` and `elig_2024q1` are versions and quarters, not
    #     shard numbers, and must not be grouped.
    #   * grouping needs at least TWO siblings. A lone file is used as
    #     itself.
    #
    # This matters: an earlier version collapsed `PT_MASTER_V2` and
    # `PT_MASTER_V3` into one glob and silently read BOTH — 152,000 rows
    # where the correct answer was 102,000. A wrong answer, not an error.
    shard = re.compile(r"^(?P<base>.+?)[_-](?P<num>\d{2,})$")
    groups: dict[tuple[str, str], list[Path]] = {}
    singles: list[Path] = []

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            for ext in ("parquet", "csv"):
                import glob as _glob

                pat = str(entry / "**" / f"*.{ext}")
                if _glob.glob(pat, recursive=True):
                    out[entry.name] = pat
                    break
            continue
        if entry.suffix.lower() not in (".parquet", ".csv"):
            continue
        m = shard.match(entry.stem)
        if m:
            groups.setdefault((m.group("base"), entry.suffix), []).append(entry)
        else:
            singles.append(entry)

    for (base, suffix), members in groups.items():
        if len(members) >= 2:
            out[base] = str(root / f"{base}*{suffix}")
        else:
            singles.extend(members)

    for entry in singles:
        out[entry.stem] = str(entry)
    return out


def columns_of(pattern: str) -> tuple[str, ...]:
    """Column names for a read pattern, without scanning the data.

    Parquet exposes its schema in the footer, so this is a metadata read
    even for a 300GB table. CSV needs a one-row peek.
    """
    con = duckdb.connect()
    try:
        if pattern.endswith(".csv"):
            cur = con.execute(
                f"SELECT * FROM read_csv_auto('{pattern}') LIMIT 0")
            return tuple(d[0] for d in cur.description)
        rows = con.execute(
            "SELECT name FROM parquet_schema(?) WHERE num_children IS NULL",
            [pattern],
        ).fetchall()
        return tuple(r[0] for r in rows)
    except Exception:
        return ()
    finally:
        con.close()


def identify_by_columns(root: str | Path) -> dict[str, list[tuple[str, str]]]:
    """Match tables by their COLUMNS rather than their names.

    Names at a real site are unpredictable — `dp042_enr_2024q1.parquet`,
    `MSPD_ENROLLMENT_V3`, a study code, a quarter stamp. An alias list is
    guesswork. Column signatures are not: every SCDM table this pipeline
    reads has a required-column set that no other one has (they share
    only `patid`), so a table can be identified by what is in it.

    Returns canonical name -> list of (source name, read pattern) that
    satisfy its required columns. More than one entry means ambiguity,
    which is reported rather than silently resolved.
    """
    root = Path(root)
    matches: dict[str, list[tuple[str, str]]] = {s.name: [] for s in SCDM}
    for source, pattern in _candidate_sources(root).items():
        cols = {c.lower() for c in columns_of(pattern)}
        if not cols:
            continue
        for spec in SCDM:
            if not spec.used or not spec.required:
                continue
            if set(spec.required) <= cols:
                matches[spec.name].append((source, pattern))
    return matches


def reader(pattern: str) -> str:
    """The DuckDB function to read a resolved pattern with."""
    return "read_csv_auto" if pattern.endswith(".csv") else "read_parquet"


@dataclass
class TableReport:
    name: str
    present: bool
    rows: int = 0
    columns: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    error: str = ""
    pattern: str = ""       # what it actually matched


def _columns(con: duckdb.DuckDBPyConnection, glob: str) -> tuple[str, ...]:
    rows = con.execute(
        "SELECT name FROM parquet_schema(?) WHERE num_children IS NULL", [glob]
    ).fetchall()
    return tuple(r[0] for r in rows)


def probe(indata: str | Path, sample_rows: bool = True,
          table_map: dict[str, str] | None = None) -> list[TableReport]:
    """Inspect an SCDM parquet root against what this pipeline needs."""
    root = Path(indata)
    con = duckdb.connect()
    out: list[TableReport] = []
    by_columns = identify_by_columns(root)

    for spec in SCDM:
        glob = resolve_table(root, spec, table_map=table_map,
                             by_columns=by_columns)
        if glob is None:
            out.append(TableReport(spec.name, present=False))
            continue
        try:
            if glob.endswith(".csv"):
                cols = tuple(
                    d[0] for d in con.execute(
                        f"SELECT * FROM read_csv_auto('{glob}') LIMIT 0"
                    ).description
                )
            else:
                cols = _columns(con, glob)
        except Exception as e:
            out.append(TableReport(spec.name, present=False, pattern=glob,
                                   error=str(e).split("\n")[0][:90]))
            continue

        lower = {c.lower() for c in cols}
        # A required column counts as present under any of its synonyms.
        for canon, alts in spec.column_aliases.items():
            if canon not in lower and any(a in lower for a in alts):
                lower.add(canon)
        n = 0
        if sample_rows:
            try:
                n = con.execute(
                    f"SELECT count(*) FROM {reader(glob)}('{glob}')"
                ).fetchone()[0]
            except Exception:
                n = -1

        out.append(TableReport(
            name=spec.name,
            present=True,
            rows=n,
            columns=cols,
            pattern=glob,
            missing_required=tuple(c for c in spec.required if c not in lower),
            missing_optional=tuple(c for c in spec.optional if c not in lower),
        ))
    con.close()
    return out


def format_report(reports: Iterable[TableReport]) -> str:
    by_name = {r.name: r for r in reports}
    lines = [
        f"{'table':<14}{'status':<11}{'rows':>13}  matched / notes",
        "-" * 104,
    ]
    blocking: list[str] = []

    for spec in SCDM:
        r = by_name.get(spec.name)
        if r is None or not r.present:
            if not spec.used:
                lines.append(f"{spec.name:<14}{'absent':<11}{'':>13}  "
                             f"not read by this implementation")
            elif spec.optional_table:
                lines.append(f"{spec.name:<14}{'absent':<11}{'':>13}  "
                             f"optional — {spec.needed_by}")
            else:
                lines.append(f"{spec.name:<14}{'MISSING':<11}{'':>13}  "
                             f"needed for: {spec.needed_by}")
                if spec.required:
                    blocking.append(f"{spec.name}: no matching files")
            continue

        if not spec.used:
            lines.append(f"{spec.name:<14}{'present':<11}{r.rows:>13,}  "
                         f"not read by this implementation")
            continue

        rows = f"{r.rows:,}" if r.rows >= 0 else "?"
        if r.missing_required:
            lines.append(f"{spec.name:<14}{'BAD SCHEMA':<11}{rows:>13}  "
                         f"missing required: {', '.join(r.missing_required)}")
            blocking.append(
                f"{spec.name}: missing {', '.join(r.missing_required)}"
            )
        else:
            # Show WHAT was matched. With flexible layouts, "ok" alone
            # leaves the user guessing which files were actually read.
            shown = r.pattern
            if len(shown) > 52:
                shown = "…" + shown[-51:]
            note = shown
            if r.missing_optional:
                note += f"  (no {', '.join(r.missing_optional)})"
            lines.append(f"{spec.name:<14}{'ok':<11}{rows:>13}  {note}")

    lines.append("")
    if blocking:
        lines.append("BLOCKING:")
        lines += [f"  - {b}" for b in blocking]
        lines.append("")
        lines.append(
            "Layouts accepted: one folder per table, one file per table, "
            "several files per\ntable, or CSV — any case. Point --indata at "
            "the folder containing them.\nColumn names are matched "
            "case-insensitively (PatID = patid = PATID)."
        )
    else:
        lines.append("RESULT: schema is compatible.")
    return "\n".join(lines)
