#!/usr/bin/env bash
# Install or update Lectern for you on this Linux machine, in ~/.lectern, as your own
# service. Run it as yourself, no sudo:
#   ./install.sh
# See README "Install on a Linux server"; the work is in packaging/linux/install.sh.
exec "$(dirname "$0")/packaging/linux/install.sh" "$@"
