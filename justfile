# Lectern, from this copy of the repository: the Makefile's targets, on Windows too.
#
#   just                    build for development: the Python and web dependencies, the web UI
#   just install            install or update Lectern for you (install.sh, or install.ps1)
#   just uninstall          remove it, keeping models, projects and settings
#   just purge              remove those too: a full wipe, asked first
#
# just runs the recipes in this folder wherever it is called from, so there is no guard for
# that here. LECTERN_DIR and the installers' other settings pass through, as with the scripts.

# Windows PowerShell 5.1, which every Windows 10 and 11 has and the installers are written for;
# without it just would look for sh, which only Git Bash brings. Bypass is for this process
# only, as in the README's commands: under Windows' default policy even npm (npm.ps1) is refused.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# Build for development: the Python and web dependencies, the web UI.
build: web-deps
    uv sync
    npm --prefix web run build

# npm ci only when the lock file has changed since the last one: it starts from nothing.
[private]
[unix]
web-deps:
    [ web/node_modules/.package-lock.json -nt web/package-lock.json ] || npm --prefix web ci

# npm ci only when the lock file has changed since the last one: it starts from nothing.
[private]
[windows]
web-deps:
    if (-not (Test-Path web/node_modules/.package-lock.json) -or (Get-Item web/node_modules/.package-lock.json).LastWriteTime -lt (Get-Item web/package-lock.json).LastWriteTime) { npm --prefix web ci; if (-not $? -or $LASTEXITCODE) { exit 1 } }

# Install or update Lectern for you (README "Quick start").
[unix]
install:
    ./install.sh

# Install or update Lectern for you (README "Install on Windows").
[windows]
install:
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install.ps1

# Remove Lectern, keeping its models, projects and settings.
[unix]
uninstall:
    ./uninstall.sh

# Remove Lectern, keeping its models, projects and settings.
[windows]
uninstall:
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1

# A wipe deletes every model and project, and nothing brings them back: only y or yes goes on.
# No answer (nobody at the keyboard) keeps everything; `yes | just purge` for a scripted test.
# The question and the wipe are one script, so a No can't be followed by the wipe.
# Remove Lectern with its models, projects and settings: asks first.
[unix]
purge:
    #!/usr/bin/env sh
    printf 'This deletes the models, projects and settings in %s.\nGo on? [y/N] ' \
        "${LECTERN_DIR:-$HOME/.lectern}"
    read -r answer
    case "$answer" in y|Y|yes|YES) ;; *) echo 'Nothing removed.'; exit 1 ;; esac
    exec ./uninstall.sh --purge

# Remove Lectern with its models, projects and settings: uninstall.ps1 -Purge asks first.
[windows]
purge:
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 -Purge
