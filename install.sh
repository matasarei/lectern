#!/usr/bin/env bash
# Install or update Lectern for you on this machine, in ~/.lectern. Run it as yourself, no
# sudo:
#   ./install.sh
# On Linux it becomes your own service (README "Install on a Linux server"); on a Mac it
# also builds Lectern.app into ~/Applications (README "Install on a Mac"). The work is in
# packaging/<system>/install.sh.
case "$(uname -s)" in
    Darwin) system=macos ;;
    *) system=linux ;;
esac
exec "$(dirname "$0")/packaging/$system/install.sh" "$@"
