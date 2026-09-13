"""
Read and print result tables.

The runbook told analysts to "read attrition first" — and then offered
no way to do it. Parquet does not open in Excel, and the output folder
was a mix of extensionless files and directories. Telling someone to
check a result they cannot open is not a runbook step.

`qrp show` reads the outputs and prints them, using the DuckDB that is
already installed. No pandas, no notebook, no extra install — the table
is formatted here rather than via `.df()`, which would have pulled in
pandas and numpy and failed for every base-install user.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

def _manifest(out: Path) -> dict:
    """Read the manifest written by the SAS layout, if there is one.

    The manifest is why SAS naming costs nothing downstream: tools look
    tables up by logical name instead of parsing `<runid>_<suffix>` and
    guessing which library it landed in.
    """
    import json

    path = out / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _source(out: Path, table: str) -> str | None:
    """Resolve a table name to something read_parquet can open."""
    man = _manifest(out)
    entry = man.get("tables", {}).get(table)
    if entry:
        base = out / entry["library"] / entry["file"]
        if base.is_dir():
            return str(base / "**" / "*.parquet")
        if base.with_suffix(".parquet").exists():
            return str(base.with_suffix(".parquet"))
    flat = out / f"{table}.parquet"
    if flat.exists():
        return str(flat)
    part = out / table
    if part.is_dir():
        return str(part / "**" / "*.parquet")
    if part.exists():          # extensionless file from an older run
        return str(part)
    return None


def available(out: str | Path) -> list[str]:
    out = Path(out)
    if not out.exists():
        return []
    man = _manifest(out)
    if man.get("tables"):
        return sorted(man["tables"])
    names = set()
    for entry in out.iterdir():
        if entry.name == "csv":
            continue
        names.add(entry.name[:-8] if entry.name.endswith(".parquet")
                  else entry.name)
    return sorted(names)


def show(out: str | Path, table: str | None = None, limit: int = 50,
         where: str | None = None) -> str:
    out = Path(out)
    tables = available(out)
    if not tables:
        return (f"No results found in {out}\n"
                f"  Did the run finish, and was --out set to this folder?")

    if table is None:
        lines = [f"Result tables in {out}:", ""]
        con = duckdb.connect()
        for name in tables:
            src = _source(out, name)
            try:
                n = con.execute(
                    f"SELECT count(*) FROM read_parquet('{src}')"
                ).fetchone()[0]
                lines.append(f"  {name:<24}{n:>14,} rows")
            except Exception:
                lines.append(f"  {name:<24}{'unreadable':>14}")
        con.close()
        lines += ["", "Show one with:  qrp show --out <dir> <table>"]
        return "\n".join(lines)

    src = _source(out, table)
    if src is None:
        return (f"No table called '{table}' in {out}\n"
                f"  Available: {', '.join(tables)}")

    con = duckdb.connect()
    try:
        # `--where` is RAW SQL, deliberately: a local analyst CLI
        # reading parquet the caller already has on disk. It is the one
        # place the "no supplied text reaches SQL" rule does not apply.
        # If show is ever exposed over a network (qrp serve, an API),
        # this must NOT be passed through unchanged. See
        # docs/SECURITY.md.
        clause = f"WHERE {where}" if where else ""
        total = con.execute(
            f"SELECT count(*) FROM read_parquet('{src}') {clause}"
        ).fetchone()[0]
        cur = con.execute(
            f"SELECT * FROM read_parquet('{src}') {clause} LIMIT {int(limit)}"
        )
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:
        return f"Could not read {table}: {exc}"
    finally:
        con.close()

    head = f"{table}  —  {total:,} rows"
    if total > len(rows):
        head += f"  (showing first {len(rows)})"
    return head + "\n" + _render(headers, rows)


def _fmt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _render(headers: list[str], rows: list[tuple], width: int = 30) -> str:
    """Plain-text table. Deliberately dependency-free."""
    cells = [[_fmt(v)[:width] for v in r] for r in rows]
    widths = [
        max(len(h), *(len(c[i]) for c in cells)) if cells else len(h)
        for i, h in enumerate(headers)
    ]
    numeric = [
        all(not c[i] or c[i].replace(",", "").replace(".", "")
            .replace("-", "").isdigit() for c in cells)
        for i in range(len(headers))
    ]

    def line(vals: list[str]) -> str:
        return "  ".join(
            v.rjust(w) if numeric[i] else v.ljust(w)
            for i, (v, w) in enumerate(zip(vals, widths, strict=True))
        ).rstrip()

    out = [line(headers), "-" * min(sum(widths) + 2 * len(widths), 160)]
    out += [line(c) for c in cells]
    return "\n".join(out)


def to_csv(out: str | Path, dest: str | Path,
           tables: list[str] | None = None) -> list[Path]:
    """Export result tables as CSV for Excel."""
    out, dest = Path(out), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    written = []
    try:
        for name in tables or available(out):
            src = _source(out, name)
            if src is None:
                continue
            target = dest / f"{name}.csv"
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{src}')) "
                f"TO '{target}' (FORMAT CSV, HEADER)"
            )
            written.append(target)
    finally:
        con.close()
    return written
