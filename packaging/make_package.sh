#!/usr/bin/env bash
# Make the package to copy to a server that shouldn't need Node.js:
#   dist/lectern-<version>-linux.tar.gz → on the server: tar -xzf …; cd lectern-<version>; ./install.sh
# It holds the wheel (with the web UI) and the install scripts; nothing else.
set -euo pipefail
cd "$(dirname "$0")/.."

wheel="$(./packaging/build_wheel.sh | tail -1)"
version="$(basename "$wheel" | cut -d- -f2)"
name="lectern-$version"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

mkdir -p "$stage/$name/packaging"
cp install.sh uninstall.sh README.md "$wheel" "$stage/$name/"
cp -R packaging/linux packaging/pins.sh packaging/install_lib.sh "$stage/$name/packaging/"
# No macOS metadata files (._*) in the archive when it's made on a Mac.
COPYFILE_DISABLE=1 tar -czf "dist/$name-linux.tar.gz" -C "$stage" "$name"
echo "dist/$name-linux.tar.gz"
