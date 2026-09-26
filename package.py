"""Build the distributable archive, honouring .gitignore.

Used by package.sh when the tree is not a git repository. Exclusions are
read from .gitignore so there is exactly one list to maintain — the
previous hand-written exclusions let a .ruff_cache into a shipped
archive because they named the caches that existed when they were
written.
"""

from __future__ import annotations

import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def patterns() -> list[str]:
    out = [".git"]
    gitignore = ROOT / ".gitignore"
    if gitignore.exists():
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line.rstrip("/"))
    return out


def excluded(rel: Path, pats: list[str]) -> bool:
    # Match each path component, so `docs/__pycache__/x.pyc` is caught by
    # the pattern `__pycache__` rather than needing `**/__pycache__/**`.
    for part in rel.parts:
        for pat in pats:
            if fnmatch.fnmatch(part, pat):
                return True
    return any(fnmatch.fnmatch(str(rel), pat) for pat in pats)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "qrp-duckdb.zip")
    pats = patterns()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if excluded(rel, pats):
                continue
            z.write(path, Path("pyqrp-duck") / rel)
            n += 1
    print(f"  {n} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
