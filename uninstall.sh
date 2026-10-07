#!/usr/bin/env bash
# Remove Lectern from this machine (keeps models, projects and settings unless --purge):
#   sudo ./uninstall.sh [--purge]
exec "$(dirname "$0")/packaging/linux/uninstall.sh" "$@"
