#!/usr/bin/env bash
# Remove Lectern from a host: the service and /opt/lectern. The models and projects
# (/var/lib/lectern) and the settings (/etc/lectern) stay unless --purge is given.
#
#   sudo ./uninstall.sh [--purge]
set -euo pipefail

root="${LECTERN_INSTALL_ROOT:-}"
purge=false
[ "${1:-}" = "--purge" ] && purge=true
if [ -z "$root" ] && [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh: run it with sudo." >&2
    exit 1
fi

if systemctl list-unit-files lectern.service >/dev/null 2>&1; then
    systemctl disable --now lectern || true
fi
rm -f "$root/etc/systemd/system/lectern.service"
systemctl daemon-reload
rm -rf "$root/opt/lectern"
if $purge; then
    rm -rf "$root/var/lib/lectern" "$root/etc/lectern"
    if getent passwd lectern >/dev/null; then userdel lectern; fi
    echo "Lectern is removed, with its models, projects and settings."
else
    echo "Lectern is removed. Kept: /var/lib/lectern (models, projects), /etc/lectern."
fi
