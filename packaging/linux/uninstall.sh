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

dir="${LECTERN_DIR:-$HOME/.lectern}"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lectern.service"

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
