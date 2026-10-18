#!/usr/bin/env bash
# Remove your Lectern: its service, ~/.lectern/app and ~/.lectern/bin. The data (models,
# projects, the database) and the settings stay unless --purge is given. ~/.lectern/src, the
# copy you may be running this from, always stays. Lingering is left as it is: other user
# services may need it.
#
#   ./uninstall.sh [--purge]
set -euo pipefail

purge=false
[ "${1:-}" = "--purge" ] && purge=true
if [ "$(id -u)" -eq 0 ]; then
    echo "uninstall.sh: run it as yourself, not with sudo: Lectern is installed per user." >&2
    exit 1
fi

# Without the user session the service can't be stopped, and its files mustn't go under it.
if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "uninstall.sh: there's no systemctl --user session here. Log in as yourself directly" >&2
    echo "(ssh, or the desktop), not through su or sudo." >&2
    exit 1
fi

dir="${LECTERN_DIR:-$HOME/.lectern}"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lectern.service"

# Its folders are app, bin and data: under a LECTERN_DIR that isn't an install (~/work, say)
# those names are someone else's. An install has its settings, or its versions folder.
if [ ! -f "$dir/lectern.env" ] && [ ! -d "$dir/app/versions" ]; then
    echo "uninstall.sh: there's no Lectern installed in $dir (no lectern.env, no app/versions);" >&2
    echo "nothing was removed." >&2
    exit 1
fi

if [ -f "$unit" ]; then
    systemctl --user disable --now lectern || true
    rm -f "$unit"
    systemctl --user daemon-reload
fi
rm -rf "${dir:?}/app" "${dir:?}/bin"
if $purge; then
    rm -rf "${dir:?}/data" "${dir:?}/lectern.env"
    echo "Lectern is removed, with its models, projects and settings. Kept: $dir/src, if any."
else
    echo "Lectern is removed. Kept: $dir/data (models, projects), $dir/lectern.env."
fi
