#!/usr/bin/env bash
# Build the wheel a host install (packaging/linux/install.sh) or the Docker image installs:
# the web UI first, so the wheel carries it (pyproject's artifacts = src/lectern/web_dist).
# Run from anywhere; the wheel lands in dist/.
set -euo pipefail
cd "$(dirname "$0")/.."

npm --prefix web ci
npm --prefix web run build
rm -f dist/lectern_ai-*.whl
uv build --wheel --out-dir dist

wheel=$(ls dist/lectern_ai-*.whl)
# Listed first, then searched: `unzip | grep -q` fails under pipefail when grep stops early.
listing=$(unzip -l "$wheel")
if [[ "$listing" != *"lectern/web_dist/index.html"* ]]; then
    echo "The wheel has no web UI (lectern/web_dist/index.html); the build went wrong." >&2
    exit 1
fi
echo "$wheel"
