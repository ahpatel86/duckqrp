#!/usr/bin/env bash
# Build the distributable archive.
#
# .gitignore is the single source of truth for what is excluded. Hand
# maintained --exclude lists drift: a .ruff_cache once shipped because
# the exclusions named __pycache__ and .pytest_cache but predated the
# introduction of ruff.
set -euo pipefail
OUT="${1:-qrp-duckdb.zip}"
[[ "$OUT" = /* ]] || OUT="$PWD/$OUT"

if git rev-parse --git-dir >/dev/null 2>&1; then
    git archive --format=zip --prefix=pyqrp-duck/ -o "$OUT" HEAD
else
    python3 package.py "$OUT"
fi
echo "wrote $OUT"
