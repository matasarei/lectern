#!/usr/bin/env bash
# Install or upgrade Lectern on this Linux machine as a service. Run it as yourself:
#   ./install.sh
# See README "Install on a Linux server"; the work is in packaging/linux/install.sh.
exec "$(dirname "$0")/packaging/linux/install.sh" "$@"
