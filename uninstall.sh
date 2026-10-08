#!/usr/bin/env bash
# Remove your Lectern (keeps models, projects and settings unless --purge):
#   ./uninstall.sh [--purge]
exec "$(dirname "$0")/packaging/linux/uninstall.sh" "$@"
