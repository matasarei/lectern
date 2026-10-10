#!/usr/bin/env bash
# Remove your Lectern from this Mac: Lectern.app, ~/.lectern/app and ~/.lectern/bin, and its
# log. The data (models, projects, the database) and the settings stay unless --purge is
# given. ~/.lectern/src, the copy you may be running this from, always stays.
#
#   ./uninstall.sh [--purge]
set -euo pipefail

purge=false
[ "${1:-}" = "--purge" ] && purge=true
if [ "$(id -u)" -eq 0 ]; then
    echo "uninstall.sh: run it as yourself, not with sudo: Lectern is installed per user." >&2
    exit 1
fi

here="$(cd "$(dirname "$0")" && pwd)"
dir="${LECTERN_DIR:-$HOME/.lectern}"
bundle="$HOME/Applications/Lectern.app"
binary="$bundle/Contents/MacOS/Lectern"

# The CFBundleIdentifier of an Info.plist written as XML ("" for anything else), as in
# install.sh: another app that happens to be called Lectern is left alone.
identifier_of() {
    sed -n '/<key>CFBundleIdentifier<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;}' "$1" 2>/dev/null || true
}
ours=false
if [ -e "$bundle" ]; then
    if [ "$(identifier_of "$bundle/Contents/Info.plist")" = "$(identifier_of "$here/tray/Info.plist")" ]; then
        ours=true
    else
        echo "uninstall.sh: $bundle is another app, not this one: left alone." >&2
    fi
fi

# An open app is quit first, which stops Lectern: their files mustn't go from under them.
if $ours && pgrep -f "$binary" >/dev/null; then
    echo "Shutting Lectern down…"
    pgrep -f "$binary" | while read -r pid; do kill -TERM "$pid" 2>/dev/null || true; done
    for _ in $(seq "${LECTERN_QUIT_WAIT:-60}"); do
        pgrep -f "$binary" >/dev/null || break
        sleep 1
    done
    if pgrep -f "$binary" >/dev/null; then
        echo "uninstall.sh: Lectern.app didn't quit. Shut it down from its menu and run this again;" >&2
        echo "nothing was removed." >&2
        exit 1
    fi
fi

if $ours; then rm -rf "$bundle"; fi
rm -rf "${dir:?}/app" "${dir:?}/bin" "${HOME:?}/Library/Logs/Lectern"
if $purge; then
    rm -rf "${dir:?}/data" "${dir:?}/lectern.env"
    echo "Lectern is removed, with its models, projects and settings. Kept: $dir/src, if any."
else
    echo "Lectern is removed. Kept: $dir/data (models, projects), $dir/lectern.env."
fi
