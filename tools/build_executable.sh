#!/usr/bin/env bash
# Build a single-file `qrp` executable that needs NO Python at the site.
#
#   ./tools/build_executable.sh
#
# Produces dist/qrp (Linux/macOS) or dist/qrp.exe (Windows), ~120 MB.
#
# BUILD ON THE TARGET OS. PyInstaller bundles native code — DuckDB's
# engine is a 60 MB compiled library — so a Linux build will not run on
# Windows, nor the reverse. Build once per platform the data partners
# use; the output is then a single file to copy.
#
# Verified on Linux: with PATH pointed at an empty directory, so that no
# Python can possibly be found, `qrp doctor` passes 11/11 and the
# production study runs end to end.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# Install PyInstaller only if missing. Run this inside a venv: a system
# Python managed by the OS refuses pip installs (PEP 668), and that
# refusal is correct.
python3 -c "import PyInstaller" 2>/dev/null \
    || python3 -m pip install --quiet pyinstaller
ENTRY="$(mktemp -d)/qrp_entry.py"
printf 'import sys\nfrom qrp.cli import main\nsys.exit(main())\n' > "$ENTRY"

SEP=":"; case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) SEP=";";; esac

python3 -m PyInstaller --onefile --name qrp \
    --paths "$HERE/src" \
    --add-data "$HERE/src/qrp/sql${SEP}qrp/sql" \
    --collect-all duckdb \
    --hidden-import qrp.selfcheck \
    --log-level WARN \
    "$ENTRY"

echo
echo "Built: $(ls dist/qrp* )"
echo "Check it:  ./dist/qrp version && ./dist/qrp doctor"
