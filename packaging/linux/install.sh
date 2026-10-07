#!/usr/bin/env bash
# Install or upgrade Lectern on a Linux host (x86_64, systemd), as the service `lectern`.
#
#   sudo ./install.sh lectern_ai-<version>-py3-none-any.whl
#
# - uv (pinned, checksum checked) and a Python 3.12 that uv manages live in /opt/lectern;
#   the system's Python and any uv of yours are never touched.
# - Each install goes into /opt/lectern/versions/<version>-<time>; /opt/lectern/venv points
#   at the one in use, and only moves once an install has fully succeeded. A failed upgrade
#   leaves the running version as it was.
# - Data (models, projects): /var/lib/lectern. Settings: /etc/lectern/lectern.env, written
#   only when it doesn't exist, so your edits survive upgrades.
# - Runs again safely: an upgrade is the same command with a newer wheel.
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

wheel="${1:-}"
[ -n "$wheel" ] || die "give the wheel: sudo ./install.sh lectern_ai-<version>-py3-none-any.whl"
[ -f "$wheel" ] || die "no such file: $wheel"
case "$(basename "$wheel")" in
    lectern_ai-*.whl) ;;
    *) die "$wheel isn't a Lectern wheel (lectern_ai-<version>-py3-none-any.whl)" ;;
esac
wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
version="$(basename "$wheel" | cut -d- -f2)"

if [ -z "$root" ]; then
    [ "$(id -u)" -eq 0 ] || die "run it with sudo: it creates a user and a service."
    [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] \
        || die "this installer is for Linux on x86_64; this is $(uname -s) $(uname -m)."
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
elif [ ! -x "$uv" ] || [ "$("$uv" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
    echo "Getting uv $UV_VERSION…"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL "${LECTERN_UV_URL:-$UV_LINUX_X86_64_URL}" -o "$tmp/uv.tar.gz" \
        || die "couldn't download uv (is the network up?)."
    got="$(sha256sum "$tmp/uv.tar.gz" | cut -d' ' -f1)"
    [ "$got" = "$UV_LINUX_X86_64_SHA256" ] \
        || die "uv's checksum is wrong ($got); not installing it."
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    install -m 0755 "$tmp"/uv-*/uv "$uv"
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
       && "$uv" pip install --quiet --python "$target/bin/python" "$wheel"; }; then
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

Next:
  1. Put the address nginx will serve it at in /etc/lectern/lectern.env (LECTERN_PUBLIC_URL),
     then: sudo systemctl restart lectern
  2. Add the nginx site (see packaging/linux/nginx-lectern.conf).
  3. Open the link this prints:
       sudo -u lectern /opt/lectern/bin/lectern-url
NEXT
