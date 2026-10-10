#!/usr/bin/env bash
# Remove your Lectern (keeps models, projects and settings unless --purge):
#   ./uninstall.sh [--purge]
case "$(uname -s)" in
    Darwin) system=macos ;;
    *) system=linux ;;
esac
exec "$(dirname "$0")/packaging/$system/uninstall.sh" "$@"
