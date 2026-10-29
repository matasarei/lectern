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
[unix]
web-deps:
    [ web/node_modules/.package-lock.json -nt web/package-lock.json ] || npm --prefix web ci

# npm ci only when the lock file has changed since the last one: it starts from nothing.
[windows]
web-deps:
    if (-not (Test-Path web/node_modules/.package-lock.json) -or (Get-Item web/node_modules/.package-lock.json).LastWriteTime -lt (Get-Item web/package-lock.json).LastWriteTime) { npm --prefix web ci; if (-not $? -or $LASTEXITCODE) { exit 1 } }
