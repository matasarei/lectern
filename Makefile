# Lectern, from this copy of the repository.
#
#   make                    build for development: the Python and web dependencies, the web UI
#   make install            install or update Lectern for you (./install.sh)
#   make uninstall          remove it, keeping models, projects and settings (./uninstall.sh)
#   make uninstall PURGE=1  remove those too: a full wipe (./uninstall.sh --purge), asked first
#   make purge              the same as the line above
#
# Make reads `make uninstall --purge` as an option of its own and refuses it: PURGE=1 it is.
# LECTERN_DIR and the installers' other settings pass through, as with the scripts.

# The commands below are relative to this folder: run from another with -f, they would fail
# one by one or build in the wrong place.
here := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))
ifneq ($(here),$(realpath $(CURDIR)))
$(error run it in $(here), or from anywhere as make -C $(here))
endif

.PHONY: all install uninstall purge confirm-wipe

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

uninstall: $(if $(wipe),confirm-wipe)
	./uninstall.sh$(if $(wipe), --purge)

purge: confirm-wipe
	./uninstall.sh --purge

# A wipe deletes every model and project, and nothing brings them back: only y or yes goes on.
# No answer (nobody at the keyboard) keeps everything; `yes | make purge` for a scripted test.
confirm-wipe:
	@printf 'This deletes the models, projects and settings in %s.\nGo on? [y/N] ' \
		'$(or $(LECTERN_DIR),$(HOME)/.lectern)'; \
	read -r answer; \
	case "$$answer" in y|Y|yes|YES) ;; *) echo 'Nothing removed.'; exit 1 ;; esac
