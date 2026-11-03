#!/usr/bin/env bash
# Build a self-contained bundle that installs with NO internet access.
#
# Data partner sites are frequently air-gapped or behind a proxy that
# blocks PyPI. `pip install qrp-duckdb` assumes neither is true, which
# makes it the wrong first instruction for the people most likely to be
# running this.
#
# Run this ONCE on a machine that HAS internet, on the same OS and
# Python minor version as the target site. Wheels are platform-specific:
# a bundle built on Linux/3.11 will not install on Windows/3.12.
#
#   ./tools/make_offline_bundle.sh                 # core only
#   ./tools/make_offline_bundle.sh --with-ui       # include the terminal UI
#
# Produces qrp-offline-<platform>-py<version>.tar.gz.
set -euo pipefail

WITH_UI=0
[ "${1:-}" = "--with-ui" ] && WITH_UI=1

PYTAG="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PLAT="$(python3 -c 'import platform; print(platform.system().lower())')"
STAGE="$(mktemp -d)/qrp-offline"
mkdir -p "$STAGE/wheels"

echo "Building for python $PYTAG on $PLAT"

python3 -m pip download --dest "$STAGE/wheels" . >/dev/null
if [ "$WITH_UI" = "1" ]; then
    python3 -m pip download --dest "$STAGE/wheels" textual >/dev/null
fi
python3 -m pip wheel --no-deps --wheel-dir "$STAGE/wheels" . >/dev/null

cp -r study "$STAGE/" 2>/dev/null || true
cp docs/RUNBOOK.md "$STAGE/" 2>/dev/null || true

cat > "$STAGE/INSTALL.sh" <<'INNER'
#!/usr/bin/env bash
# Offline install. No internet needed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HOME/qrp-env}"

echo "Creating $TARGET"
python3 -m venv "$TARGET"

# --no-index: never reach for the network, so this either succeeds from
# the bundled wheels or fails loudly. It does not silently half-install
# from a stale cache.
"$TARGET/bin/pip" install --no-index --find-links "$HERE/wheels" \
    --quiet qrp-duckdb

echo
"$TARGET/bin/qrp" version
echo
echo "Checking the installation works..."
"$TARGET/bin/qrp" doctor
echo
echo "Installed. Run it with:"
echo "    $TARGET/bin/qrp --help"
INNER
chmod +x "$STAGE/INSTALL.sh"

OUT="qrp-offline-${PLAT}-py${PYTAG}.tar.gz"
tar -czf "$OUT" -C "$(dirname "$STAGE")" qrp-offline
rm -rf "$(dirname "$STAGE")"
echo "wrote $OUT"
