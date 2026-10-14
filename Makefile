# Lectern, from this copy of the repository.
#
#   make                    build for development: the Python and web dependencies, the web UI
#   make install            install or update Lectern for you (./install.sh)
#   make uninstall          remove it, keeping models, projects and settings (./uninstall.sh)
#   make uninstall PURGE=1  remove those too: a full wipe (./uninstall.sh --purge)
#   make purge              the same as the line above
#
# Make reads `make uninstall --purge` as an option of its own and refuses it: PURGE=1 it is.
# LECTERN_DIR and the installers' other settings pass through, as with the scripts.

.PHONY: all install uninstall purge

all: web/node_modules/.package-lock.json
	uv sync
	npm --prefix web run build

# npm ci only when the lock file has changed since the last one: it starts from nothing.
web/node_modules/.package-lock.json: web/package-lock.json
	npm --prefix web ci

install:
	./install.sh

# Only 1 (or yes, true) wipes, and only typed on the command line: make also takes the
# environment's variables, and a PURGE=1 left exported there must not.
wipe := $(and $(filter command line,$(origin PURGE)),$(filter 1 yes true,$(PURGE)))

uninstall:
	./uninstall.sh$(if $(wipe), --purge)

purge:
	./uninstall.sh --purge
