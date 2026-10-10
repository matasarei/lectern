#!/usr/bin/env bash
# Install or update Lectern for you on this Mac (Apple Silicon): Lectern itself in ~/.lectern,
# and Lectern.app, the menu-bar app that starts and stops it, in ~/Applications. No sudo.
#
#   ./install.sh            (from a copy of the repository)
#
# It builds both from this copy: the web UI and the wheel (that needs Node.js), and the app
# (that needs the Command Line Tools: xcode-select --install).
# A wheel can also be given: ./install.sh lectern_ai-<version>-py3-none-any.whl
#
#   ~/.lectern/src               where the README says to clone (any copy works)
#   ~/.lectern/app               uv (pinned, checksum checked), its Python 3.12, the installed code
#   ~/.lectern/data              Lectern's data (LECTERN_HOME): the database, models, projects, token
#   ~/.lectern/lectern.env       settings, written only when missing, so your edits survive updates
#   ~/Applications/Lectern.app   what you open: it runs Lectern for as long as it's open
#
# - As on Linux (packaging/install_lib.sh), each install goes into app/versions/<version>-…
#   and app/venv points at the one in use. Here the app is built too, into Lectern.app.new.
#   Nothing that runs is touched until both are ready: a failure before that leaves the
#   running app and version as they were.
# - An update is the same command after `git pull`. An app that's open is quit (which stops
#   Lectern the way its Quit does), replaced and opened again; one that wasn't open isn't
#   started.
#
# LECTERN_DIR moves ~/.lectern; LECTERN_UV_BIN and LECTERN_HEALTH_WAIT as on Linux;
# LECTERN_SWIFTC names another swiftc; LECTERN_QUIT_WAIT is how many seconds to wait for the
# open app to quit (60).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/pins.sh
. "$here/../pins.sh"
# shellcheck source=packaging/install_lib.sh
. "$here/../install_lib.sh"

[ "$(id -u)" -ne 0 ] || die "run it as yourself, not as root or with sudo: Lectern installs \
into your home folder (~/.lectern and ~/Applications)."
case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) ;;
    Darwin/*) die "Intel Macs aren't supported yet." ;;
    *) die "this installer is for macOS; this is $(uname -s) $(uname -m)." ;;
esac
# On a Mac without the tools /usr/bin/swiftc is a stub that only asks to install them.
"${LECTERN_SWIFTC:-swiftc}" --version >/dev/null 2>&1 || die "building Lectern.app needs the \
Command Line Tools (swiftc): xcode-select --install"

dir="${LECTERN_DIR:-$HOME/.lectern}"
# It goes into the app's Info.plist and into bin/lectern-serve: plain paths only.
case "$dir" in
    /*) ;;
    *) die "the install folder must be an absolute path: $dir" ;;
esac
case "$dir" in
    *[!A-Za-z0-9._/+@-]*) die "the install folder can't hold spaces or special characters: \"$dir\" (LECTERN_DIR)" ;;
esac
app="$dir/app"
data="$dir/data"
settings="$dir/lectern.env"
bundle="$HOME/Applications/Lectern.app"
binary="$bundle/Contents/MacOS/Lectern"

# The CFBundleIdentifier of an Info.plist written as XML ("" for anything else).
identifier_of() {
    sed -n '/<key>CFBundleIdentifier<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;}' "$1" 2>/dev/null || true
}
# Another app that happens to be called Lectern is never replaced.
if [ -e "$bundle" ] \
    && [ "$(identifier_of "$bundle/Contents/Info.plist")" != "$(identifier_of "$here/tray/Info.plist")" ]; then
    die "$bundle is another app, not this one: move it away first."
fi

# --- uv ---------------------------------------------------------------------------------
if [ -n "${LECTERN_UV_BIN:-}" ]; then
    uv="$LECTERN_UV_BIN"
else
    fetch_uv "$app/bin" "$UV_MACOS_ARM64_URL" "$UV_MACOS_ARM64_SHA256"
    uv="$app/bin/uv"
fi

# --- the wheel --------------------------------------------------------------------------
find_wheel "${1:-}" "brew install node, or the installer from nodejs.org"

mkdir -p "$app/versions" "$dir/bin" "$data" "$(dirname "$bundle")"

# --- the new version and the new app, before anything that runs is touched ----------------
install_version
if ! "$here/build_app.sh" "$bundle.new" "$dir" "$version"; then
    rm -rf "$bundle.new" "$target"
    die "building Lectern.app failed (see above); the version in use is unchanged."
fi

# Models downloaded while running Lectern from a checkout: offered once, before Lectern's
# first start here makes its own (empty) models folder.
models_before="$HOME/Library/Application Support/Lectern/models"
offer_models=""
if [ -d "$models_before" ] && [ ! -e "$data/models" ]; then offer_models=1; fi

# --- the switch ---------------------------------------------------------------------------
# An open app is quit first: it handles SIGTERM as its own Quit, which stops Lectern cleanly.
# It's opened again below. An app that isn't open isn't started by an update.
start=""
[ -e "$bundle" ] || start=1  # a first install
if pgrep -f "$binary" >/dev/null; then
    start=1
    echo "Quitting Lectern…"
    pgrep -f "$binary" | while read -r pid; do kill -TERM "$pid" 2>/dev/null || true; done
    for _ in $(seq "${LECTERN_QUIT_WAIT:-60}"); do
        pgrep -f "$binary" >/dev/null || break
        sleep 1
    done
    if pgrep -f "$binary" >/dev/null; then
        rm -rf "$bundle.new" "$target"
        die "Lectern.app didn't quit. Quit it from its menu and run this again; the version \
in use is unchanged."
    fi
fi
switch_venv
rm -rf "$bundle.old"
[ ! -e "$bundle" ] || mv "$bundle" "$bundle.old"
mv "$bundle.new" "$bundle"
rm -rf "$bundle.old"
write_lectern_url
# What Lectern.app starts. exec keeps the process id, so the app's signals reach Lectern.
cat > "$dir/bin/lectern-serve" <<SERVE
#!/bin/sh
set -a
. "$settings"
set +a
# An app opened by macOS has a bare PATH; the script sandbox looks for Podman or Docker on it.
PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
export PATH
exec "$app/venv/bin/lectern" serve --no-browser --port "\${LECTERN_PORT:-8765}"
SERVE
chmod 0755 "$dir/bin/lectern-serve"
write_settings "Restart, in Lectern's menu (its icon in the menu bar)"

# --- start it -----------------------------------------------------------------------------
if [ -z "$start" ]; then
    cat <<CLOSED
Upgraded to Lectern $version. Lectern wasn't open, so it isn't now: open it from
~/Applications. Update: git pull && ./install.sh   Log: ~/Library/Logs/Lectern/lectern.log
CLOSED
    exit 0
fi
# --background: the app starts Lectern but opens no browser window.
open -g "$bundle" --args --background

# Only a version that answers counts as installed. Until then nothing older is deleted.
if ! wait_for_health; then
    echo "install.sh: Lectern $version didn't start; see: ~/Library/Logs/Lectern/lectern.log" >&2
    if [ -n "$previous" ] && [ -d "$previous" ]; then
        echo "The previous version is kept in $previous. To go back to it:" >&2
        echo "  ln -sfn $previous $app/venv   then Restart, in Lectern's menu" >&2
        echo "(only if the new version hasn't migrated the database yet: the log says)" >&2
    fi
    exit 1
fi
if [ -n "$previous" ]; then
    echo "Upgraded to Lectern $version and opened it again."
else
    echo "Lectern $version is installed and running."
fi
prune_versions  # the new version works: it and the one before it stay

cat <<NEXT

Lectern is in ~/Applications and in the menu bar: Open Lectern, in its menu.
It runs on this Mac at http://127.0.0.1:$port (LECTERN_PORT in $settings).
The link with its access token:  $dir/bin/lectern-url
Update: git pull && ./install.sh   Log: ~/Library/Logs/Lectern/lectern.log

Lectern runs while the app is open. To have it open when you log in:
System Settings → General → Login Items → add Lectern.
NEXT
if [ -n "$offer_models" ]; then
    cat <<MODELS

You already have models in "$models_before"
(from running Lectern out of a checkout). To use them here instead of downloading them
again, quit Lectern and run:
  rmdir "$data/models" 2>/dev/null; ln -s "$models_before" "$data/models"
MODELS
fi
