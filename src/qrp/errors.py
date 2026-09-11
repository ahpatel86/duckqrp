"""
Turning exceptions into advice.

Shared by the CLI and the UI, because they must not diverge. An earlier
version had this logic only on the UI path, so `qrp run` printed a raw
DuckDB traceback while the same failure in the TUI produced a plain
sentence naming the setting to change. The runbook promises actionable
errors; that promise has to hold wherever the failure happens.
"""

from __future__ import annotations

import re as _re


_QUOTED = _re.compile(r"'[^']*'|\"[^\"]*\"")


def _redact_values(text: str) -> str:
    """Strip quoted literals from a database error message.

    DuckDB quotes the offending cell value in conversion and constraint
    errors. Those values come from the claims data, and the run log is
    not a patient-level artefact, so they must not survive into it.
    """
    return _QUOTED.sub("'<redacted>'", text)


def explain(exc: BaseException) -> str:
    """A message an analyst can act on, not just the exception text."""
    name = type(exc).__name__
    text = str(exc)
    first = text.splitlines()[0] if text else name

    if "OutOfMemory" in name or "Out of Memory" in text:
        return (
            "Out of memory: the memory limit is too low for this dataset.\n"
            "  DuckDB spills to disk, but it still needs a minimum working "
            "set.\n"
            "  Raise --memory-limit (or omit it to let DuckDB choose), and "
            "point\n"
            "  --temp-dir at a disk with free space."
        )

    if "No files found" in text:
        missing = ""
        if "/**/*.parquet" in text:
            seg = text.split("/**/*.parquet")[0].rstrip("'\"")
            missing = seg.rsplit("/", 1)[-1] if "/" in seg else seg
        return (
            f"No parquet files found for the '{missing}' table.\n"
            "  --indata should point at the PARENT folder that contains "
            "enrollment/,\n"
            "  demographic/, dispensing/, diagnosis/ and death/ — not at one "
            "of them.\n"
            "  Run `qrp inspect --indata <path>` to see what was found."
        )

    if "Referenced column" in text or "not found in FROM clause" in text:
        return (
            f"A required column is missing or named differently.\n"
            f"  {first}\n"
            "  Column names are matched case-insensitively, so only a genuine "
            "name\n"
            "  difference causes this. Run `qrp inspect --indata <path>` for a "
            "full list."
        )

    if "temp" in text.lower() and ("space" in text.lower()
                                  or "write" in text.lower()):
        return (f"The spill directory is unusable.\n  {first}\n"
                "  Check --temp-dir exists, is writable, and has free space.")

    if "Conversion Error" in name or "Conversion Error" in text:
        # DuckDB embeds the OFFENDING VALUE in this message —
        # "Could not convert string 'X' to INT32" — and X is a cell from
        # the claims data. On a real run that is a patient identifier, a
        # date of birth, or a code, and this message is written to the
        # run log, which is not a patient-level artefact and is not
        # protected as one. Redact the value and keep the type, which is
        # the part that tells the user what to fix. Reported in review.
        return (f"A column could not be converted to the expected type.\n"
                f"  {_redact_values(first)}\n"
                "  This usually means a date or numeric column holds "
                "unexpected values. The offending value is withheld: it "
                "is claims data, and this log is not a patient-level "
                "output.")

    if name in ("FileNotFoundError", "IsADirectoryError"):
        # Some of these are raised deliberately with a multi-line,
        # already-actionable message (table identification). Truncating
        # to the first line threw away the part that told the user what
        # to do, so pass a multi-line message through unchanged.
        if "\n" in text:
            return text
        return f"{first}\n  Check the path is spelled correctly and exists."

    # Catch-all. Redact here too: constraint violations, out-of-range
    # errors and several other DuckDB messages quote the offending cell
    # the same way conversion errors do, and any of them can reach this
    # branch. Fixing only the reported path would leave the same leak
    # one exception type away.
    return f"{name}: {_redact_values(first)}"
