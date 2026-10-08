#!/usr/bin/env bash
# Install or update Lectern for you on this Linux machine (x86_64, systemd), in ~/.lectern,
# as your own service (systemctl --user). No sudo.
#
#   ./install.sh            (from a copy of the repository, or from a package)
#
# In a working copy of the repository it builds the app from it first (the web UI, then the
# wheel; that needs Node.js). In a package (packaging/make_package.sh) the wheel is already
# there. nginx and the public address are yours to set up afterwards; it says how.
# A wheel can also be given: ./install.sh lectern_ai-<version>-py3-none-any.whl
#
#   ~/.lectern/src          where the README says to clone (any copy works)
#   ~/.lectern/app          uv (pinned, checksum checked), its Python 3.12, the installed code
#   ~/.lectern/data         Lectern's data (LECTERN_HOME): the database, models, projects, token
#   ~/.lectern/lectern.env  settings, written only when missing, so your edits survive updates
#   ~/.config/systemd/user/lectern.service
#
# - Each install goes into app/versions/<version>-<time>-<pid>; app/venv points at the one
#   in use, and only moves once an install has fully succeeded. A failed install leaves the
#   running version as it was. Once the new version answers on /health, the one before it
#   is kept (to go back to) and older ones are removed.
# - An update is the same command from the newer copy or package (git pull && ./install.sh).
#   The data and settings stay; the new code replaces the old (even a fix built with the
#   same version number: Lectern's wheel is never taken from uv's cache), and the database
#   is migrated forward when the new version starts.
#
# LECTERN_DIR moves ~/.lectern; LECTERN_UV_BIN uses a uv you already have instead of
# downloading one; LECTERN_HEALTH_WAIT is how many seconds to wait for the service to
# answer (60); LECTERN_DRI (tests) is where the GPU's render nodes are looked for.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/pins.sh
. "$here/../pins.sh" 2>/dev/null || . "$here/pins.sh"

die() { echo "install.sh: $*" >&2; exit 1; }
user="${USER:-$(id -un)}"  # not every shell sets USER (su, cron)

[ "$(id -u)" -ne 0 ] || die "run it as yourself, not as root or with sudo: Lectern installs \
into your home folder (~/.lectern) and runs as your own service."
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] \
    || die "this installer is for Linux on x86_64; this is $(uname -s) $(uname -m)."
command -v systemctl >/dev/null || die "systemd (systemctl) is needed."
systemctl --user show-environment >/dev/null 2>&1 || die "there's no systemctl --user \
session here. Log in as yourself directly (ssh, or the desktop), not through su or sudo; \
or: sudo machinectl shell $user@"

dir="${LECTERN_DIR:-$HOME/.lectern}"
app="$dir/app"
data="$dir/data"
settings="$dir/lectern.env"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# uv, pinned and checksum-checked, into $1/uv (unless it's already that version).
fetch_uv() {
    local to="$1" tmp got
    if [ -x "$to/uv" ] && [ "$("$to/uv" --version 2>/dev/null | cut -d' ' -f2)" = "$UV_VERSION" ]; then
        return
    fi
    echo "Getting uv $UV_VERSION…"
    mkdir -p "$to"
    tmp="$(mktemp -d)"
    curl -fsSL "${LECTERN_UV_URL:-$UV_LINUX_X86_64_URL}" -o "$tmp/uv.tar.gz" \
        || { rm -rf "$tmp"; die "couldn't download uv (is the network up?)."; }
    got="$(sha256sum "$tmp/uv.tar.gz" | cut -d' ' -f1)"
    if [ "$got" != "$UV_LINUX_X86_64_SHA256" ]; then
        rm -rf "$tmp"
        die "uv's checksum is wrong ($got); not installing it."
    fi
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    install -m 0755 "$tmp"/uv-*/uv "$to/uv"
    rm -rf "$tmp"
}

# The most recent of the files given (wheel names are plain: lectern_ai-<version>-…whl).
# shellcheck disable=SC2012
newest() { { ls -t "$@" 2>/dev/null || true; } | head -1; }  # a glob with no match: ""

# --- uv ---------------------------------------------------------------------------------
if [ -n "${LECTERN_UV_BIN:-}" ]; then
    uv="$LECTERN_UV_BIN"
else
    fetch_uv "$app/bin"
    uv="$app/bin/uv"
fi

# --- the wheel --------------------------------------------------------------------------
wheel="${1:-}"
top="$(cd "$here/../.." && pwd)"  # the working copy's root, or the package's
if [ -z "$wheel" ]; then
    if [ -f "$top/pyproject.toml" ] && [ -d "$top/web" ]; then
        # A working copy: build its wheel (it writes node_modules and dist here).
        command -v "${LECTERN_NPM:-npm}" >/dev/null || die "building the web UI needs Node.js (npm): \
sudo dnf install nodejs (Fedora) or sudo apt install nodejs npm (Debian/Ubuntu). Or copy a \
package made with packaging/make_package.sh, which needs none."
        echo "Building Lectern from $top…"
        PATH="$(dirname "$uv"):$PATH" "$top/packaging/build_wheel.sh" >/dev/null \
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

if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    echo "NVIDIA GPU found: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    echo "No NVIDIA driver found: Lectern will use Vulkan if present, else the CPU (slow)."
fi

mkdir -p "$app/versions" "$dir/bin" "$data" "$units"

# --- the app ----------------------------------------------------------------------------
export UV_PYTHON_INSTALL_DIR="$app/python" UV_CACHE_DIR="$app/cache" UV_NO_CONFIG=1
target="$app/versions/$version-$(date +%Y%m%d%H%M%S)-$$"  # $$: two runs in a second
echo "Installing Lectern $version…"
if ! { "$uv" venv --quiet --python 3.12 "$target" \
       && "$uv" pip install --quiet --refresh-package lectern-ai \
              --python "$target/bin/python" "$wheel"; }; then
    rm -rf "$target"
    die "installing Lectern $version failed (see above); the version in use is unchanged."
fi
previous="$(readlink "$app/venv" 2>/dev/null || true)"
# Switched in one step (GNU mv -T renames over the old link), so the service never sees none.
ln -sfn "$target" "$app/venv.next"
if ! mv -T "$app/venv.next" "$app/venv" 2>/dev/null; then
    rm -f "$app/venv" "$app/venv.next"  # BSD mv (the tests on macOS)
    ln -s "$target" "$app/venv"
fi

# The way to the access link, with Lectern's settings loaded: ~/.lectern/bin/lectern-url
cat > "$dir/bin/lectern-url" <<URL
#!/bin/sh
set -a
. "$settings"
set +a
exec "$app/venv/bin/lectern" url --port "\${LECTERN_PORT:-8765}"
URL
chmod 0755 "$dir/bin/lectern-url"

# --- settings and the service -------------------------------------------------------------
if [ ! -f "$settings" ]; then
    cat > "$settings" <<ENV
# Lectern's settings (read by its service). Restart after a change:
#   systemctl --user restart lectern
LECTERN_HOME=$data
LECTERN_PORT=8765
# The address nginx serves Lectern at, e.g. http://lectern.lan (comma-separate several).
LECTERN_PUBLIC_URL=
ENV
fi
sed "s|@LECTERN_DIR@|$dir|g" "$here/lectern.service" > "$units/lectern.service"
systemctl --user daemon-reload
if [ -n "$previous" ] && systemctl --user is-active --quiet lectern; then
    systemctl --user restart lectern
    started="Upgraded to Lectern $version and restarted it."
else
    systemctl --user enable --now lectern
    started="Lectern $version is installed and running."
fi

# Only a version that answers counts as installed. Until then nothing older is deleted.
port="$(sed -n 's/^LECTERN_PORT=//p' "$settings" | tail -1)"
port="${port:-8765}"
echo "Waiting for Lectern to answer on 127.0.0.1:$port…"
up=""
for _ in $(seq "${LECTERN_HEALTH_WAIT:-60}"); do
    if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then up=1; break; fi
    sleep 1
done
if [ -z "$up" ]; then
    echo "install.sh: Lectern $version didn't start; see: journalctl --user -u lectern -n 50" >&2
    if [ -n "$previous" ] && [ -d "$previous" ]; then
        echo "The previous version is kept in $previous. To go back to it:" >&2
        echo "  ln -sfn $previous $app/venv && systemctl --user restart lectern" >&2
        echo "(only if the new version hasn't migrated the database yet: the log says)" >&2
    fi
    exit 1
fi
echo "$started"
# The new version works: keep it and the one before it (to go back to), drop the rest.
for old in "$app"/versions/*; do
    [ "$old" = "$target" ] || [ "$old" = "$previous" ] || rm -rf "$old"
done

# --- starting at boot, and the GPU --------------------------------------------------------
# A user service runs only while you're logged in, unless lingering is on.
if [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null || true)" != yes ]; then
    # Never a password prompt halfway through: when polkit wants an admin, print the line.
    if ! loginctl --no-ask-password enable-linger 2>/dev/null; then
        echo
        echo "To start Lectern at boot, without logging in, run once:"
        echo "  sudo loginctl enable-linger $user"
    fi
fi
# Vulkan (and the GPU when nobody is at the screen) needs the render group.
if compgen -G "${LECTERN_DRI:-/dev/dri}/renderD*" >/dev/null \
    && ! id -nG | tr ' ' '\n' | grep -qx render; then
    echo
    echo "To let Lectern use the GPU's render node when you're not at the screen, run once,"
    echo "then reboot (logging out isn't enough: with lingering, your services keep running"
    echo "with the groups they started with):"
    echo "  sudo usermod -aG render,video $user"
fi

cat <<NEXT

Lectern runs on this machine at http://127.0.0.1:$port (LECTERN_PORT in $settings).
The link with its access token:  $dir/bin/lectern-url
Update: git pull && ./install.sh   Logs: journalctl --user -u lectern

To reach it from your network (by hand, see README "Install on a Linux server"):
  1. Put the address you'll use in $settings (LECTERN_PUBLIC_URL=http://<name>),
     then: systemctl --user restart lectern
  2. Add the nginx site: packaging/linux/nginx-lectern.conf
NEXT
