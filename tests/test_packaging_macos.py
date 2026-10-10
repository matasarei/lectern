"""Packaging for a Mac: the menu-bar app (packaging/macos/build_app.sh)."""

from __future__ import annotations

import contextlib
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "packaging" / "macos"

# Swift isn't compiled anywhere else: CI is Linux. These run on a Mac with the Command Line Tools.
needs_swiftc = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="building the app needs macOS with the Command Line Tools (swiftc)",
)


def _build(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "build_app.sh"), *args],  # noqa: S607
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **env},
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip


@needs_swiftc
def test_the_app_builds_into_a_signed_bundle(tmp_path):
    bundle = tmp_path / "Lectern.app"
    built = _build(str(bundle), "/Users/someone/.lectern", "1.2.3")
    assert built.returncode == 0, built.stderr

    assert (bundle / "Contents/MacOS/Lectern").read_bytes()[:4] == b"\xcf\xfa\xed\xfe"  # Mach-O
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Lectern"
    assert info["CFBundleShortVersionString"] == "1.2.3"
    assert info["LecternDir"] == "/Users/someone/.lectern"
    assert info["LSUIElement"] is True  # a menu-bar item, no Dock icon
    verified = subprocess.run(  # noqa: S603
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],  # noqa: S607
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert verified.returncode == 0, verified.stderr


@needs_swiftc
def test_building_again_replaces_the_bundle(tmp_path):
    bundle = tmp_path / "Lectern.app"
    assert _build(str(bundle), "/Users/someone/.lectern", "1.0.0").returncode == 0
    (bundle / "Contents/left-over").touch()
    assert _build(str(bundle), "/Users/someone/.lectern", "1.1.0").returncode == 0
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == "1.1.0"
    assert not (bundle / "Contents/left-over").exists()


def test_the_info_plist_template_is_a_plist_with_both_placeholders():
    text = (MACOS / "tray/Info.plist").read_text()
    info = plistlib.loads(text.encode())
    assert info["CFBundleShortVersionString"] == "@VERSION@"
    assert info["LecternDir"] == "@LECTERN_DIR@"


def test_build_app_only_writes_to_a_path_that_is_an_app(tmp_path):
    # It starts by removing what's at the path: never a folder that isn't a bundle.
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "keep").touch()
    done = _build(str(folder), "/Users/someone/.lectern", "1.0.0")
    assert done.returncode == 1 and ".app" in done.stderr
    assert (folder / "keep").exists()


@pytest.mark.parametrize("where", ["relative/lectern", "/Users/some one/.lectern", "/Users/a<b"])
def test_build_app_refuses_an_install_folder_the_plist_cant_hold(tmp_path, where):
    done = _build(str(tmp_path / "Lectern.app"), where, "1.0.0")
    assert done.returncode == 1 and "install folder" in done.stderr
    assert not (tmp_path / "Lectern.app").exists()


def test_build_app_without_swiftc_says_how_to_get_it(tmp_path):
    done = _build(str(tmp_path / "Lectern.app"), "/Users/someone/.lectern", "1.0.0",
                  LECTERN_SWIFTC="no-such-swiftc")  # fmt: skip
    assert done.returncode == 1 and "xcode-select --install" in done.stderr
    assert not (tmp_path / "Lectern.app").exists()


# --- install.sh in a fake home, with fake system commands ------------------------------------

FAKES = {
    # Every call is logged as "<name> <args>". The installer is told it's on an Apple Silicon
    # Mac wherever the tests run (CI is Linux); STATE/arch says otherwise.
    "uname": 'case "$1" in -s) echo Darwin;; -m) cat "$STATE/arch" 2>/dev/null || echo arm64;; '
    '*) /usr/bin/uname "$@";; esac',
    # uv and curl as in test_packaging.py: `venv` makes the folder with a python and a
    # lectern, `pip install` can be told to fail (FAIL_PIP); /health answers unless
    # STATE/down, and anything else is the real curl.
    "uv": 'echo "uv $*" >> "$LOG"; '
    'if [ "$1" = venv ]; then d="${@: -1}"; mkdir -p "$d/bin"; touch "$d/bin/python"; '
    'printf "#!/bin/sh\\n" > "$d/bin/lectern"; '
    'elif [ "$1" = pip ] && [ -n "${FAIL_PIP:-}" ]; then echo "no network" >&2; exit 1; '
    'elif [ "$1" = --version ]; then echo "uv 0.12.23"; fi',
    "curl": 'case "$*" in *127.0.0.1:*/health*) echo "curl $*" >> "$LOG"; '
    '[ ! -f "$STATE/down" ];; *) exec /usr/bin/curl "$@";; esac',
    # swiftc "compiles" by creating the file -o names. STATE/no-tools: the stub macOS has
    # without the Command Line Tools; FAIL_SWIFTC: a compile error.
    "swiftc": 'echo "swiftc $*" >> "$LOG"; [ ! -f "$STATE/no-tools" ] || exit 1; '
    '[ "$1" != --version ] || exit 0; '
    'if [ -n "${FAIL_SWIFTC:-}" ]; then echo "error: it does not compile" >&2; exit 1; fi; '
    'while [ $# -gt 0 ]; do if [ "$1" = -o ]; then : > "$2"; fi; shift; done',
    "codesign": 'echo "codesign $*" >> "$LOG"',
    "open": 'echo "open $*" >> "$LOG"',
    # The running app is the process whose id is in STATE/app-pid, for as long as it lives
    # (a killed child of the test is a zombie until the test collects it: not running).
    "pgrep": 'p="$(cat "$STATE/app-pid" 2>/dev/null)"; [ -n "$p" ] || exit 1; '
    's="$(ps -o stat= -p "$p" 2>/dev/null)"; [ -n "$s" ] && [ "${s#Z}" = "$s" ] && echo "$p"',
}


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir, state = tmp_path / "fakebin", tmp_path / "state"
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),  # ~/.lectern and ~/Applications are the defaults
        "USER": "someone",
        "LECTERN_UV_BIN": str(bin_dir / "uv"),
        "LECTERN_HEALTH_WAIT": "2",
        "LECTERN_QUIT_WAIT": "5",
        "LOG": str(tmp_path / "log"),
        "STATE": str(state),
        **extra,
    }


def _install(tmp_path: Path, wheel: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "install.sh"), str(wheel)],  # noqa: S607
        env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def _wheel(tmp_path: Path, version: str) -> Path:
    wheel = tmp_path / f"lectern_ai-{version}-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    return wheel


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "log").read_text().splitlines()


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home/.lectern"


def _bundle(tmp_path: Path) -> Path:
    return tmp_path / "home/Applications/Lectern.app"


def test_a_first_install_puts_lectern_in_the_home_folder_and_builds_and_opens_the_app(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    home, bundle = _home(tmp_path), _bundle(tmp_path)

    venv = (home / "app/venv").resolve()
    assert venv.parent == (home / "app/versions").resolve() and venv.name.startswith("0.1.0-")
    env = (home / "lectern.env").read_text()
    assert f"LECTERN_HOME={home}/data" in env and "LECTERN_PUBLIC_URL=" in env
    assert "systemctl" not in env  # the Linux way to restart isn't the Mac's
    assert (home / "bin/lectern-url").exists() and (home / "data").is_dir()

    assert (bundle / "Contents/MacOS/Lectern").exists()
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == "0.1.0" and info["LecternDir"] == str(home)
    assert not bundle.with_name("Lectern.app.new").exists()

    log = _log(tmp_path)
    assert f"open -g {bundle} --args --background" in log  # started quietly: no browser
    assert any(line.startswith("codesign --force --sign - ") for line in log)
    assert not any(c.split()[0] in ("sudo", "systemctl", "loginctl") for c in log)
    assert "Lectern 0.1.0 is installed and running." in done.stdout
    assert "Login Items" in done.stdout
    # Nothing written outside the home folder (beyond the test's own fakes and wheels).
    outside = {p.name for p in tmp_path.iterdir()} - {"home", "fakebin", "state", "log"}
    assert all(name.endswith(".whl") for name in outside), outside


def test_on_a_mac_run_as_root_it_refuses(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "fakebin/id").write_text(
        '#!/usr/bin/env bash\nif [ "$1" = -u ]; then echo 0; else /usr/bin/id "$@"; fi\n'
    )
    (tmp_path / "fakebin/id").chmod(0o755)
    done = subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "run it as yourself" in done.stderr
    assert not _home(tmp_path).exists() and not _bundle(tmp_path).exists()


def test_an_intel_mac_is_refused(tmp_path):
    _env(tmp_path)
    (tmp_path / "state/arch").write_text("x86_64\n")
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 1 and "Intel Macs aren't supported yet" in done.stderr
    assert not _home(tmp_path).exists() and not _bundle(tmp_path).exists()


def test_without_the_command_line_tools_it_says_how_to_get_them(tmp_path):
    _env(tmp_path)
    (tmp_path / "state/no-tools").touch()
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 1 and "xcode-select --install" in done.stderr
    assert not _home(tmp_path).exists() and not _bundle(tmp_path).exists()


def test_on_a_mac_a_wrong_uv_checksum_is_refused(tmp_path):
    fake = tmp_path / "uv.tar.gz"
    fake.write_bytes(b"not uv")
    env = _env(tmp_path, LECTERN_UV_URL=fake.as_uri())
    del env["LECTERN_UV_BIN"]
    done = subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "uv's checksum is wrong" in done.stderr
    assert not (_home(tmp_path) / "app/bin/uv").exists() and not _bundle(tmp_path).exists()


@pytest.mark.skipif(shutil.which("shasum") is None, reason="shasum isn't installed")
def test_the_checksum_falls_back_to_shasum_where_there_is_no_sha256sum(tmp_path):
    # Older macOS has shasum only. A PATH holding just what the function needs, without
    # sha256sum, wherever the tests run.
    only = tmp_path / "bin"
    only.mkdir()
    for tool in ("shasum", "cut"):
        (only / tool).symlink_to(shutil.which(tool))
    (tmp_path / "file").write_bytes(b"abc")
    done = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f'. "{ROOT}/packaging/install_lib.sh"; sha256_of "{tmp_path}/file"'],
        env={"PATH": str(only)}, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert done.stdout.strip() == expected, done.stderr


def test_lectern_serve_loads_the_settings_and_becomes_the_server(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home = _home(tmp_path)
    # A stand-in for the installed `lectern` that says how it was started.
    (home / "app/venv/bin/lectern").write_text(
        '#!/bin/sh\necho "args: $*"\necho "home: $LECTERN_HOME"\necho "path: $PATH"\n'
    )
    (home / "app/venv/bin/lectern").chmod(0o755)
    (home / "lectern.env").write_text(f"LECTERN_HOME={home}/data\nLECTERN_PORT=9001\n")
    script = (home / "bin/lectern-serve").read_text()
    assert f'exec "{home}/app/venv/bin/lectern" serve' in script  # exec: the same process id
    ran = subprocess.run(  # noqa: S603
        [str(home / "bin/lectern-serve")], env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert "args: serve --no-browser --port 9001" in ran.stdout
    assert f"home: {home}/data" in ran.stdout
    # An app opened by macOS has a bare PATH; Docker and Podman live in these.
    assert "path: /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" in ran.stdout


def test_models_from_a_checkout_are_offered_once(tmp_path):
    _env(tmp_path)
    old = tmp_path / "home/Library/Application Support/Lectern/models"
    old.mkdir(parents=True)
    first = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert first.returncode == 0, first.stderr
    assert f'ln -s "{old}" "{_home(tmp_path)}/data/models"' in first.stdout

    (_home(tmp_path) / "data/models").mkdir()  # Lectern has its own now, or the link was made
    again = _install(tmp_path, _wheel(tmp_path, "0.1.1"))
    assert again.returncode == 0 and "ln -s" not in again.stdout


def test_without_models_from_a_checkout_nothing_is_offered(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0 and "ln -s" not in done.stdout


def test_the_top_install_script_picks_the_installer_by_system(tmp_path):
    done = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=_env(tmp_path), capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert _bundle(tmp_path).exists()  # uname says Darwin: the Mac installer ran


# --- updating in place ----------------------------------------------------------------------


@contextlib.contextmanager
def _open_app(tmp_path: Path, command: str = "exec sleep 600") -> Iterator[subprocess.Popen]:
    """A process standing in for the open Lectern.app: the fake pgrep finds it by its pid."""
    app = subprocess.Popen(["/bin/sh", "-c", command])  # noqa: S603
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state/app-pid").write_text(f"{app.pid}\n")
    try:
        yield app
    finally:
        app.kill()
        app.wait()


def _version(bundle: Path) -> str:
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    return info["CFBundleShortVersionString"]


def _opens(tmp_path: Path) -> int:
    return sum(line.startswith("open ") for line in _log(tmp_path))


def test_an_update_quits_the_open_app_replaces_both_and_opens_it_again(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home, bundle = _home(tmp_path), _bundle(tmp_path)
    (home / "lectern.env").write_text((home / "lectern.env").read_text() + "LECTERN_PORT=9000\n")
    (home / "data/models").mkdir()
    first = (home / "app/venv").resolve()
    with _open_app(tmp_path) as app:
        done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
        assert done.returncode == 0, done.stderr
        # Asked to quit with SIGTERM, which the app treats as its own Quit.
        assert app.wait(timeout=10) == -15

    assert _version(bundle) == "0.2.0" and (home / "app/venv").resolve().name.startswith("0.2.0-")
    assert "LECTERN_PORT=9000" in (home / "lectern.env").read_text()  # your settings stay
    assert (home / "data/models").is_dir()
    assert _opens(tmp_path) == 2  # the first install, and again after this update
    assert "curl -fsS http://127.0.0.1:9000/health" in _log(tmp_path)
    assert "Upgraded to Lectern 0.2.0 and opened it again." in done.stdout
    kept = sorted(p.name for p in (home / "app/versions").iterdir())
    assert len(kept) == 2 and first.name in kept  # the one before, to go back to
    assert not bundle.with_name("Lectern.app.new").exists()
    assert not bundle.with_name("Lectern.app.old").exists()


def test_an_update_doesnt_start_an_app_that_wasnt_open(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    health_checks = sum("/health" in line for line in _log(tmp_path))
    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
    assert done.returncode == 0, done.stderr
    assert _version(_bundle(tmp_path)) == "0.2.0"
    assert _opens(tmp_path) == 1  # only the first install's
    assert sum("/health" in line for line in _log(tmp_path)) == health_checks  # nothing to wait for
    assert "wasn't open" in done.stdout


@pytest.mark.parametrize("failing", ["FAIL_PIP", "FAIL_SWIFTC"])
def test_a_failed_update_leaves_the_app_and_the_version_as_they_were(tmp_path, failing):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home, bundle = _home(tmp_path), _bundle(tmp_path)
    before = (home / "app/venv").resolve()
    with _open_app(tmp_path) as app:
        done = _install(tmp_path, _wheel(tmp_path, "0.2.0"), **{failing: "1"})
        assert done.returncode == 1 and "the version in use is unchanged" in done.stderr
        assert app.poll() is None  # the open app wasn't asked to quit
    assert (home / "app/venv").resolve() == before and _version(bundle) == "0.1.0"
    assert [p.name for p in (home / "app/versions").iterdir()] == [before.name]
    assert not bundle.with_name("Lectern.app.new").exists()
    assert _opens(tmp_path) == 1


def test_an_update_that_doesnt_start_says_so_and_keeps_the_previous_version(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home = _home(tmp_path)
    before = (home / "app/venv").resolve()
    (tmp_path / "state/down").touch()
    with _open_app(tmp_path):
        done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
    assert done.returncode == 1 and "Lectern 0.2.0 didn't start" in done.stderr
    assert "Library/Logs/Lectern/lectern.log" in done.stderr
    assert f"ln -sfn {before} {home}/app/venv" in done.stderr  # the way back
    assert before.is_dir()


def test_an_open_app_that_wont_quit_stops_the_update_before_anything_is_switched(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home, bundle = _home(tmp_path), _bundle(tmp_path)
    before = (home / "app/venv").resolve()
    with _open_app(tmp_path, "trap '' TERM; while :; do sleep 1; done") as app:
        done = _install(tmp_path, _wheel(tmp_path, "0.2.0"), LECTERN_QUIT_WAIT="2")
        assert app.poll() is None
    assert done.returncode == 1 and "didn't quit" in done.stderr
    assert (home / "app/venv").resolve() == before and _version(bundle) == "0.1.0"
    assert [p.name for p in (home / "app/versions").iterdir()] == [before.name]
    assert not bundle.with_name("Lectern.app.new").exists()


def test_another_app_called_lectern_is_never_replaced(tmp_path):
    _env(tmp_path)
    other = _bundle(tmp_path) / "Contents"
    other.mkdir(parents=True)
    theirs = {"CFBundleIdentifier": "com.example.reader"}
    (other / "Info.plist").write_bytes(plistlib.dumps(theirs))
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 1 and "another app" in done.stderr
    assert plistlib.loads((other / "Info.plist").read_bytes()) == theirs
    assert not _home(tmp_path).exists()  # stopped before anything was built


# --- uninstall.sh ---------------------------------------------------------------------------


def _uninstall(tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "uninstall.sh"), *args],  # noqa: S607
        env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def test_uninstall_on_a_mac_keeps_the_data_unless_purged(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home, bundle = _home(tmp_path), _bundle(tmp_path)
    (home / "data/models").mkdir(parents=True)
    (home / "src").mkdir()
    logs = tmp_path / "home/Library/Logs/Lectern"
    logs.mkdir(parents=True)
    (logs / "lectern.log").touch()

    with _open_app(tmp_path) as app:
        kept = _uninstall(tmp_path)
        assert kept.returncode == 0, kept.stderr
        assert app.wait(timeout=10) == -15  # the open app was quit first, which stops Lectern
    assert not bundle.exists() and not (home / "app").exists() and not (home / "bin").exists()
    assert not logs.exists()
    assert (home / "data/models").exists() and (home / "lectern.env").exists()
    assert (home / "src").exists()

    purged = _uninstall(tmp_path, "--purge")
    assert purged.returncode == 0, purged.stderr
    assert not (home / "data").exists() and not (home / "lectern.env").exists()
    assert (home / "src").exists()  # the copy it may be running from


def test_uninstall_leaves_another_app_called_lectern_alone(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    theirs = {"CFBundleIdentifier": "com.example.reader"}
    (_bundle(tmp_path) / "Contents/Info.plist").write_bytes(plistlib.dumps(theirs))
    done = _uninstall(tmp_path)
    assert done.returncode == 0 and "another app" in done.stderr
    assert (_bundle(tmp_path) / "Contents/Info.plist").exists()
    assert not (_home(tmp_path) / "app").exists()  # Lectern's own files still go


def test_uninstall_with_an_app_that_wont_quit_removes_nothing(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    with _open_app(tmp_path, "trap '' TERM; while :; do sleep 1; done"):
        done = _uninstall(tmp_path, LECTERN_QUIT_WAIT="2")
    assert done.returncode == 1 and "didn't quit" in done.stderr
    assert _bundle(tmp_path).exists() and (_home(tmp_path) / "app/venv").exists()


def test_uninstall_on_a_mac_run_as_root_refuses(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    (tmp_path / "fakebin/id").write_text(
        '#!/usr/bin/env bash\nif [ "$1" = -u ]; then echo 0; else /usr/bin/id "$@"; fi\n'
    )
    (tmp_path / "fakebin/id").chmod(0o755)
    env = _env(tmp_path)
    done = subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "uninstall.sh")],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "run it as yourself" in done.stderr
    assert _bundle(tmp_path).exists() and (_home(tmp_path) / "app/venv").exists()


def test_the_top_uninstall_script_picks_by_system_too(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    done = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "uninstall.sh")],  # noqa: S607
        env=_env(tmp_path), capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert not _bundle(tmp_path).exists()
