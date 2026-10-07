#!/usr/bin/env bash
# Install or upgrade Lectern on a Linux host (x86_64, systemd), as the service `lectern`.
#
#   ./install.sh            (from a copy of the repository, or from a package)
#
# Run it as yourself. In a working copy of the repository it builds the app from it first
# (the web UI, then the wheel; that needs Node.js), as you, never as root. In a package
# (packaging/make_package.sh) the wheel is already there. Then it asks for sudo once and does
# the system part. nginx and the public address are yours to set up afterwards; it says how.
# A wheel can also be given: ./install.sh lectern_ai-<version>-py3-none-any.whl
#
# - uv (pinned, checksum checked) and a Python 3.12 that uv manages live in /opt/lectern;
#   the system's Python and any uv of yours are never touched.
# - Each install goes into /opt/lectern/versions/<version>-<time>; /opt/lectern/venv points
#   at the one in use, and only moves once an install has fully succeeded. A failed upgrade
#   leaves the running version as it was.
# - Data (models, projects): /var/lib/lectern. Settings: /etc/lectern/lectern.env, written
#   only when it doesn't exist, so your edits survive upgrades.
# - Runs again safely: an update is the same command from the newer copy or package. The
#   database, models, projects and settings stay; the new code replaces the old (even a fix
#   built with the same version number: Lectern's wheel is never taken from uv's cache), and
#   the database is migrated forward when the new version starts.
#
# LECTERN_INSTALL_ROOT (tests only) prefixes every path; LECTERN_UV_BIN uses a uv you
# already have instead of downloading one.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/pins.sh
. "$here/../pins.sh" 2>/dev/null || . "$here/pins.sh"

root="${LECTERN_INSTALL_ROOT:-}"
prefix="$root/opt/lectern"
etc="$root/etc/lectern"
data="$root/var/lib/lectern"
units="$root/etc/systemd/system"

die() { echo "install.sh: $*" >&2; exit 1; }

# uv, pinned and checksum-checked, into $1/uv (unless it's already that version).
fetch_uv() {
    local dir="$1" tmp got
    if [ -x "$dir/uv" ] && [ "$("$dir/uv" --version 2>/dev/null | cut -d' ' -f2)" = "$UV_VERSION" ]; then
        return
    fi
    echo "Getting uv $UV_VERSION…"
    mkdir -p "$dir"
    tmp="$(mktemp -d)"
    curl -fsSL "${LECTERN_UV_URL:-$UV_LINUX_X86_64_URL}" -o "$tmp/uv.tar.gz" \
        || { rm -rf "$tmp"; die "couldn't download uv (is the network up?)."; }
    got="$(sha256sum "$tmp/uv.tar.gz" | cut -d' ' -f1)"
    if [ "$got" != "$UV_LINUX_X86_64_SHA256" ]; then
        rm -rf "$tmp"
        die "uv's checksum is wrong ($got); not installing it."
    fi
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    install -m 0755 "$tmp"/uv-*/uv "$dir/uv"
    rm -rf "$tmp"
}

# The most recent of the files given (wheel names are plain: lectern_ai-<version>-…whl).
# shellcheck disable=SC2012
newest() { { ls -t "$@" 2>/dev/null || true; } | head -1; }  # a glob with no match: ""

wheel="${1:-}"
top="$(cd "$here/../.." && pwd)"  # the working copy's root, or the package's
if [ -z "$wheel" ]; then
    if [ -f "$top/pyproject.toml" ] && [ -d "$top/web" ]; then
        # A working copy: build its wheel as yourself (it writes node_modules and dist here).
        if [ -z "$root" ] && [ "$(id -u)" -eq 0 ]; then
            die "run it as yourself, without sudo: it builds the app from this working copy first, then asks for sudo."
        fi
        command -v "${LECTERN_NPM:-npm}" >/dev/null || die "building the web UI needs Node.js (npm): \
sudo dnf install nodejs (Fedora) or sudo apt install nodejs npm (Debian/Ubuntu). Or copy a \
package made with packaging/make_package.sh, which needs none."
        if [ -n "${LECTERN_UV_BIN:-}" ]; then
            build_uv_dir="$(dirname "$LECTERN_UV_BIN")"
        else
            build_uv_dir="${XDG_CACHE_HOME:-$HOME/.cache}/lectern-install"
            fetch_uv "$build_uv_dir"
        fi
        echo "Building Lectern from $top…"
        PATH="$build_uv_dir:$PATH" "$top/packaging/build_wheel.sh" >/dev/null \
            || die "building the app failed (see above)."
        wheel="$(newest "$top"/dist/lectern_ai-*.whl)"
    else
        wheel="$(newest "$top"/lectern_ai-*.whl "$here"/lectern_ai-*.whl)"
        [ -n "$wheel" ] || die "no Lectern wheel next to this script, and this isn't a working copy of the repository."
    fi
fi
[ -f "$wheel" ] || die "no such file: $wheel"
case "$(basename "$wheel")" in
    lectern_ai-*.whl) ;;
    *) die "$wheel isn't a Lectern wheel (lectern_ai-<version>-py3-none-any.whl)" ;;
esac
wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
version="$(basename "$wheel" | cut -d- -f2)"

if [ -z "$root" ]; then
    [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] \
        || die "this installer is for Linux on x86_64; this is $(uname -s) $(uname -m)."
    if [ "$(id -u)" -ne 0 ]; then
        echo "Now the system part (the lectern user, /opt/lectern, the service): sudo asks for your password."
        exec sudo "$0" "$wheel"
    fi
fi
command -v systemctl >/dev/null || die "systemd (systemctl) is needed."

if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    echo "NVIDIA GPU found: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    echo "No NVIDIA driver found: Lectern will use Vulkan if present, else the CPU (slow)."
fi

mkdir -p "$prefix/bin" "$prefix/versions" "$etc" "$data" "$units"

# --- uv ---------------------------------------------------------------------------------
uv="$prefix/bin/uv"
if [ -n "${LECTERN_UV_BIN:-}" ]; then
    uv="$LECTERN_UV_BIN"
else
    fetch_uv "$prefix/bin"
fi

# --- the user ---------------------------------------------------------------------------
if ! getent passwd lectern >/dev/null; then
    useradd --system --home-dir /var/lib/lectern --no-create-home \
        --shell /usr/sbin/nologin lectern
fi
for group in video render; do  # the GPU's device files
    if getent group "$group" >/dev/null; then usermod -aG "$group" lectern; fi
done
chown lectern:lectern "$data"

# --- the app ----------------------------------------------------------------------------
export UV_PYTHON_INSTALL_DIR="$prefix/python" UV_CACHE_DIR="$prefix/cache" UV_NO_CONFIG=1
target="$prefix/versions/$version-$(date +%Y%m%d%H%M%S)"
echo "Installing Lectern $version…"
if ! { "$uv" venv --quiet --python 3.12 "$target" \
       && "$uv" pip install --quiet --refresh-package lectern-ai \
              --python "$target/bin/python" "$wheel"; }; then
    rm -rf "$target"
    die "installing Lectern $version failed (see above); the version in use is unchanged."
fi
previous="$(readlink "$prefix/venv" 2>/dev/null || true)"
# Switched in one step (GNU mv -T renames over the old link), so the service never sees none.
ln -sfn "$target" "$prefix/venv.next"
if ! mv -T "$prefix/venv.next" "$prefix/venv" 2>/dev/null; then
    rm -f "$prefix/venv" "$prefix/venv.next"  # BSD mv (the tests on macOS)
    ln -s "$target" "$prefix/venv"
fi
# Older versions go once the new one is in place.
for old in "$prefix"/versions/*; do
    [ "$old" = "$target" ] || rm -rf "$old"
done

# The way to the access link: Lectern's settings loaded, as the lectern user (it owns the
# token): sudo -u lectern /opt/lectern/bin/lectern-url
cat > "$prefix/bin/lectern-url" <<'URL'
#!/bin/sh
set -a
. /etc/lectern/lectern.env
set +a
exec /opt/lectern/venv/bin/lectern url --port "${LECTERN_PORT:-8765}"
URL
chmod 0755 "$prefix/bin/lectern-url"

# --- settings and the service -------------------------------------------------------------
if [ ! -f "$etc/lectern.env" ]; then
    cat > "$etc/lectern.env" <<ENV
# Lectern's settings (read by the lectern service). Restart after a change:
#   sudo systemctl restart lectern
LECTERN_HOME=/var/lib/lectern
LECTERN_PORT=8765
# The address nginx serves Lectern at, e.g. http://lectern.lan (comma-separate several).
LECTERN_PUBLIC_URL=
ENV
fi
install -m 0644 "$here/lectern.service" "$units/lectern.service"
systemctl daemon-reload
if [ -n "$previous" ] && systemctl is-active --quiet lectern; then
    systemctl restart lectern
    echo "Upgraded to Lectern $version and restarted it."
else
    systemctl enable --now lectern
    echo "Lectern $version is installed and running."
fi

cat <<NEXT

Lectern runs on this machine at http://127.0.0.1:8765 (LECTERN_PORT in lectern.env).
The link with its access token:  sudo -u lectern /opt/lectern/bin/lectern-url

To reach it from your network (by hand, see README "Install on a Linux server"):
  1. Put the address you'll use in /etc/lectern/lectern.env (LECTERN_PUBLIC_URL=http://<name>),
     then: sudo systemctl restart lectern
  2. Add the nginx site: packaging/linux/nginx-lectern.conf
NEXT
