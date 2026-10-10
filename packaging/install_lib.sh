# shellcheck shell=bash disable=SC2034,SC2154  # sourced: the installer sets and reads these
# The parts of a host install that don't depend on the system it's on: the pinned uv, the
# wheel, a new version folder that's switched in only once its install has succeeded, the
# settings, and waiting for Lectern to answer. Sourced, after pins.sh, by
# packaging/<system>/install.sh, which runs under `set -euo pipefail` (a step that fails
# here stops the install only because of it) and sets:
#   here      its own folder (packaging/<system>)
#   dir       the install folder (~/.lectern), with app="$dir/app", data="$dir/data" and
#             settings="$dir/lectern.env"
#   uv        the uv to run, once it has one (fetch_uv, or LECTERN_UV_BIN)
# Each function says what it sets for the installer to use.

die() { echo "install.sh: $*" >&2; exit 1; }

# uv, pinned and checksum-checked, into $1/uv (unless it's already that version). $2 is the
# release archive for this system and $3 its sha256, both from pins.sh.
fetch_uv() {
    local to="$1" url="$2" sha256="$3" tmp got
    if [ -x "$to/uv" ] && [ "$("$to/uv" --version 2>/dev/null | cut -d' ' -f2)" = "$UV_VERSION" ]; then
        return
    fi
    echo "Getting uv $UV_VERSION…"
    mkdir -p "$to"
    tmp="$(mktemp -d)"
    curl -fsSL "${LECTERN_UV_URL:-$url}" -o "$tmp/uv.tar.gz" \
        || { rm -rf "$tmp"; die "couldn't download uv (is the network up?)."; }
    got="$(sha256sum "$tmp/uv.tar.gz" | cut -d' ' -f1)"
    if [ "$got" != "$sha256" ]; then
        rm -rf "$tmp"
        die "uv's checksum is wrong ($got); not installing it."
    fi
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    install -m 0755 "$tmp"/uv-*/uv "$to/uv"
    rm -rf "$tmp"
}

# The most recent of the files given (wheel names are plain: lectern_ai-<version>-…whl).
# shellcheck disable=SC2012
newest() { { ls -t "$@" 2>/dev/null || true; } | head -1; }  # a glob with no match: ""

# The wheel to install: $1 when one is given; else, in a working copy of the repository,
# built from it (that needs npm: $2 says how to get Node.js on this system); else the
# newest one in the package. Sets wheel (an absolute path) and version.
find_wheel() {
    local how_to_get_node="$2" top
    wheel="$1"
    top="$(cd "$here/../.." && pwd)"  # the working copy's root, or the package's
    if [ -z "$wheel" ]; then
        if [ -f "$top/pyproject.toml" ] && [ -d "$top/web" ]; then
            # A working copy: build its wheel (it writes node_modules and dist here).
            command -v "${LECTERN_NPM:-npm}" >/dev/null || die "building the web UI needs Node.js (npm): \
$how_to_get_node. Or copy a \
package made with packaging/make_package.sh, which needs none."
            echo "Building Lectern from $top…"
            PATH="$(dirname "$uv"):$PATH" "$top/packaging/build_wheel.sh" >/dev/null \
                || die "building the app failed (see above)."
            wheel="$(newest "$top"/dist/lectern_ai-*.whl)"
        else
            wheel="$(newest "$top"/lectern_ai-*.whl "$here"/lectern_ai-*.whl)"
            [ -n "$wheel" ] || die "no Lectern wheel next to this script, and this isn't a working copy of the repository."
        fi
    fi
    [ -f "$wheel" ] || die "no such file: $wheel"
    case "$(basename "$wheel")" in
        lectern_ai-*.whl) ;;
        *) die "$wheel isn't a Lectern wheel (lectern_ai-<version>-py3-none-any.whl)" ;;
    esac
    wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
    version="$(basename "$wheel" | cut -d- -f2)"
}

# The wheel installed into a new folder, app/versions/<version>-<time>-<pid>. Sets target.
# A failed install removes the folder and stops here: the version in use is unchanged.
install_version() {
    export UV_PYTHON_INSTALL_DIR="$app/python" UV_CACHE_DIR="$app/cache" UV_NO_CONFIG=1
    target="$app/versions/$version-$(date +%Y%m%d%H%M%S)-$$"  # $$: two runs in a second
    echo "Installing Lectern $version…"
    if ! { "$uv" venv --quiet --python 3.12 "$target" \
           && "$uv" pip install --quiet --refresh-package lectern-ai \
                  --python "$target/bin/python" "$wheel"; }; then
        rm -rf "$target"
        die "installing Lectern $version failed (see above); the version in use is unchanged."
    fi
}

# Point app/venv at the new folder. Sets previous: the folder it pointed at, "" on a first
# install.
switch_venv() {
    previous="$(readlink "$app/venv" 2>/dev/null || true)"
    # Switched in one step (GNU mv -T renames over the old link), so the service never sees none.
    ln -sfn "$target" "$app/venv.next"
    if ! mv -T "$app/venv.next" "$app/venv" 2>/dev/null; then
        rm -f "$app/venv" "$app/venv.next"  # BSD mv (the tests on macOS)
        ln -s "$target" "$app/venv"
    fi
}

# The way to the access link, with Lectern's settings loaded: ~/.lectern/bin/lectern-url
write_lectern_url() {
    cat > "$dir/bin/lectern-url" <<URL
#!/bin/sh
set -a
. "$settings"
set +a
exec "$app/venv/bin/lectern" url --port "\${LECTERN_PORT:-8765}"
URL
    chmod 0755 "$dir/bin/lectern-url"
}

# The settings file, written only when it's missing, so your edits survive updates. $1 is
# the command that restarts Lectern on this system, for the file's own first lines.
write_settings() {
    [ ! -f "$settings" ] || return 0
    cat > "$settings" <<ENV
# Lectern's settings (read by its service). Restart after a change:
#   $1
LECTERN_HOME=$data
LECTERN_PORT=8765
# The address(es) people use to reach Lectern, through whatever you put in front of it,
# e.g. https://lectern.example.com (comma-separate several).
LECTERN_PUBLIC_URL=
# To reach Lectern on your network without a proxy, uncomment this and set LECTERN_PUBLIC_URL
# to the address people type, e.g. http://192.168.1.2:8765. There's no login yet and it's
# plain http: see README "Reaching it from other computers".
# LECTERN_HOST=0.0.0.0
ENV
}

# Wait for Lectern to answer on /health, up to LECTERN_HEALTH_WAIT seconds (60); fails when
# it doesn't. Sets port: LECTERN_PORT in the settings, or 8765.
wait_for_health() {
    port="$(sed -n 's/^LECTERN_PORT=//p' "$settings" | tail -1)"
    port="${port:-8765}"
    echo "Waiting for Lectern to answer on 127.0.0.1:$port…"
    for _ in $(seq "${LECTERN_HEALTH_WAIT:-60}"); do
        if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}

# Keep the new version and the one before it (to go back to), drop the rest.
prune_versions() {
    local old
    for old in "${app:?}"/versions/*; do
        [ "$old" = "$target" ] || [ "$old" = "$previous" ] || rm -rf "$old"
    done
}
