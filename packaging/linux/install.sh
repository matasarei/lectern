#!/usr/bin/env bash
# Install or update Lectern for you on this Linux machine (x86_64, systemd), in ~/.lectern,
# as your own service (systemctl --user). No sudo.
#
#   ./install.sh            (from a copy of the repository, or from a package)
#
# In a working copy of the repository it builds the app from it first (the web UI, then the
# wheel; that needs Node.js). In a package (packaging/make_package.sh) the wheel is already
# there. How other computers reach it (a reverse proxy, a tunnel) is yours to set up.
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
# What isn't particular to Linux: uv, the wheel, the version switch, the /health wait.
# shellcheck source=packaging/install_lib.sh
. "$here/../install_lib.sh"

user="${USER:-$(id -un)}"  # not every shell sets USER (su, cron)

[ "$(id -u)" -ne 0 ] || die "run it as yourself, not as root or with sudo: Lectern installs \
into your home folder (~/.lectern) and runs as your own service."
case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) ;;
    *) die "this installer is for Linux on x86_64; this is $(uname -s) $(uname -m)." ;;
esac
command -v systemctl >/dev/null || die "systemd (systemctl) is needed."
systemctl --user show-environment >/dev/null 2>&1 || die "there's no systemctl --user \
session here. Log in as yourself directly (ssh, or the desktop), not through su or sudo; \
or: sudo machinectl shell $user@"

dir="${LECTERN_DIR:-$HOME/.lectern}"
# It goes into the unit's ExecStart (split on spaces) and through sed: plain paths only.
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
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# --- uv ---------------------------------------------------------------------------------
if [ -n "${LECTERN_UV_BIN:-}" ]; then
    uv="$LECTERN_UV_BIN"
else
    fetch_uv "$app/bin" "$UV_LINUX_X86_64_URL" "$UV_LINUX_X86_64_SHA256"
    uv="$app/bin/uv"
fi

# --- the wheel --------------------------------------------------------------------------
find_wheel "${1:-}" "sudo dnf install nodejs (Fedora) or sudo apt install nodejs npm (Debian/Ubuntu)"

if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    echo "NVIDIA GPU found: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    echo "No NVIDIA driver found: Lectern will use Vulkan if present, else the CPU (slow)."
fi

mkdir -p "$app/versions" "$dir/bin" "$data" "$units"

# --- the app ----------------------------------------------------------------------------
install_version ""  # the bare wheel: no extras on Linux
switch_venv
write_lectern_url
write_lectern_command

# --- settings and the service -------------------------------------------------------------
write_settings "systemctl --user restart lectern"
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
if ! wait_for_health; then
    echo "install.sh: Lectern $version didn't start; see: journalctl --user -u lectern -n 50" >&2
    if [ -n "$previous" ] && [ -d "$previous" ]; then
        echo "The previous version is kept in $previous. To go back to it:" >&2
        echo "  ln -sfn $previous $app/venv && systemctl --user restart lectern" >&2
        echo "(only if the new version hasn't migrated the database yet: the log says)" >&2
    fi
    exit 1
fi
echo "$started"
prune_versions  # the new version works: it and the one before it stay

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
Before anyone else uses it, turn accounts on:  $dir/bin/lectern user init <name>
Update: git pull && ./install.sh   Logs: journalctl --user -u lectern

To reach it from other computers, put a reverse proxy or a tunnel in front of
127.0.0.1:$port and set its address as LECTERN_PUBLIC_URL in $settings, then:
systemctl --user restart lectern. README "Reaching it from other computers" has examples.
NEXT
