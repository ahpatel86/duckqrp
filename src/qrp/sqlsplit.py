"""
Split a .sql file into individual statements.

Needed because the pipeline previously ran each .sql file as one call,
so a stage containing six CREATE TABLE statements reported a single
timing and the word "script" — no row counts, no idea which statement
was slow. Statement-level execution is what makes the log diagnostic
rather than merely chronological.

Splitting on ";" naively is wrong for this codebase: an audit found
semicolons inside `--` comments in 7 of 11 files and inside string
literals in 8 of 11. So this walks the text tracking whether it is
inside a line comment, a block comment, a single-quoted string or a
double-quoted identifier, and only breaks on a semicolon at depth zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What a statement produces, for reporting.
_TARGET = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\s+|TEMPORARY\s+)?"
    r"(TABLE|VIEW|MACRO)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_DROP = re.compile(r"^\s*DROP\s+(TABLE|VIEW)", re.IGNORECASE)


@dataclass(frozen=True)
class Statement:
    sql: str
    kind: str | None       # TABLE | VIEW | MACRO | DROP | None
    target: str | None     # object being created, when there is one

    @property
    def label(self) -> str:
        if self.target:
            return f"{self.target}"
        first = " ".join(self.sql.split())[:48]
        return first + ("…" if len(first) == 48 else "")


def split_statements(sql: str) -> list[Statement]:
    """Split SQL into statements, ignoring semicolons in comments/strings."""
    out: list[Statement] = []

    def push(raw: str) -> None:
        stmt = _make(raw)
        if stmt is not None:
            out.append(stmt)

    buf: list[str] = []
    i, n = 0, len(sql)
    in_line = in_block = in_str = in_ident = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line:
            buf.append(ch)
            if ch == "\n":
                in_line = False
        elif in_block:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 1
                in_block = False
        elif in_str:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":          # escaped quote
                    buf.append(nxt)
                    i += 1
                else:
                    in_str = False
        elif in_ident:
            buf.append(ch)
            if ch == '"':
                in_ident = False
        elif ch == "-" and nxt == "-":
            buf.append(ch)
            in_line = True
        elif ch == "/" and nxt == "*":
            buf.append(ch)
            in_block = True
        elif ch == "'":
            buf.append(ch)
            in_str = True
        elif ch == '"':
            buf.append(ch)
            in_ident = True
        elif ch == ";":
            push("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1

    tail = "".join(buf)
    if tail.strip():
        push(tail)
    return out


def _make(raw: str) -> Statement | None:
    if not raw.strip():
        return None
    # strip comments only for classification, never for execution
    stripped = re.sub(r"--[^\n]*", "", raw)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    if not stripped.strip():
        return None
    m = _TARGET.search(stripped)
    if m:
        return Statement(raw, m.group(1).upper(), m.group(2))
    if _DROP.match(stripped):
        return Statement(raw, "DROP", None)
    return Statement(raw, None, None)
