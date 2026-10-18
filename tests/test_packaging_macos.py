"""Packaging for a Mac: the menu-bar app (packaging/macos/build_app.sh)."""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
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
    # Its icon, drawn by the app itself while it was being built.
    assert info["CFBundleIconFile"] == "Lectern"
    icns = bundle / "Contents/Resources/Lectern.icns"
    assert icns.read_bytes()[:4] == b"icns"
    # Every size is in it twice: for an ordinary screen, and at double the pixels for Retina.
    unpacked = tmp_path / "unpacked.iconset"
    subprocess.run(  # noqa: S603
        ["/usr/bin/iconutil", "-c", "iconset", "-o", str(unpacked), str(icns)],
        check=True,
        timeout=60,
    )
    names = {p.name for p in unpacked.iterdir()}
    for points in (16, 32, 128, 256, 512):
        assert {f"icon_{points}x{points}.png", f"icon_{points}x{points}@2x.png"} <= names
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


def _png(path: Path) -> tuple[int, int, bytes]:
    """A PNG's width and height, and its pixel data still filtered: enough to tell a drawing
    from a blank."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    packed, at = b"", 8
    while at < len(data):
        (length,), kind = struct.unpack(">I", data[at : at + 4]), data[at + 4 : at + 8]
        if kind == b"IDAT":
            packed += data[at + 8 : at + 8 + length]
        at += 12 + length
    return width, height, zlib.decompress(packed)


@needs_swiftc
def test_the_app_draws_every_size_of_its_icon(tmp_path):
    bundle = tmp_path / "Lectern.app"
    assert _build(str(bundle), "/Users/someone/.lectern", "1.0.0").returncode == 0
    iconset = tmp_path / "Lectern.iconset"
    drawn = subprocess.run(  # noqa: S603
        [str(bundle / "Contents/MacOS/Lectern"), "--draw-icon", str(iconset)],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert drawn.returncode == 0, drawn.stderr

    # What iconutil asks of an .iconset: these names, each at its size in pixels.
    expected = {
        f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png": points * scale
        for points in (16, 32, 128, 256, 512)
        for scale in (1, 2)
    }
    assert {p.name for p in iconset.iterdir()} == set(expected)
    for name, pixels in expected.items():
        width, height, picture = _png(iconset / name)
        assert (width, height) == (pixels, pixels), name
        assert len(set(picture)) > 32, f"{name} looks blank"  # a plate, a book, a letter


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


# --- the app itself, run for real ---------------------------------------------------------------
# The built binary is started directly, as a menu-bar app for a second or two, against a
# stand-in for Lectern in a temporary install folder. CFFIXED_USER_HOME moves its log there.


def _on_a_mac_desktop() -> bool:
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        return False
    session = subprocess.run(  # noqa: S603
        ["/bin/launchctl", "managername"], capture_output=True, text=True, timeout=60
    )
    return session.stdout.strip() == "Aqua"  # a login at the screen, not ssh


needs_a_desktop = pytest.mark.skipif(
    not _on_a_mac_desktop(),
    reason="running the app needs a Mac desktop session and the Command Line Tools",
)

# What `lectern serve` does that the app depends on: prints the access link, keeps running,
# and ends on SIGINT. It leaves a file behind to say that's how it was stopped.
STAND_IN = """#!/bin/sh
echo "Lectern is running at http://127.0.0.1:1/#token=SECRET"
echo "an ordinary line"
trap 'echo interrupted > "$0.stopped"; exit 0' INT
while :; do sleep 1; done
"""


def _until(condition, what: str, timeout: float = 20.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"never happened: {what}")


def _ps(pid: int, column: str) -> str:
    """One of ps's columns for the process ("args=", "stat="); "" once it's gone."""
    shown = subprocess.run(  # noqa: S603
        ["/bin/ps", "-o", column, "-p", str(pid)], capture_output=True, text=True, timeout=60
    )
    return shown.stdout.strip()


def _alive(pid: int) -> bool:
    """Running, not just a number: a killed process whose parent is gone stays a zombie until
    launchd collects it, and os.kill(pid, 0) still finds a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not _ps(pid, "stat=").startswith("Z")


def _text(path: Path) -> str:
    """The log so far. The app writes the pid file a moment before its first log line, so a
    test that has just seen the pid file may find no log yet."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


@pytest.fixture
def installed(tmp_path):
    """An install folder with a stand-in for Lectern, and the app built for it."""
    folder = tmp_path / "lectern"
    lectern = folder / "app/venv/bin/lectern"
    lectern.parent.mkdir(parents=True)
    lectern.write_text(STAND_IN)
    lectern.chmod(0o755)
    (folder / "bin").mkdir()
    (folder / "bin/lectern-serve").write_text(f'#!/bin/sh\nexec "{lectern}" serve --no-browser\n')
    (folder / "bin/lectern-serve").chmod(0o755)
    bundle = tmp_path / "Lectern.app"
    built = _build(str(bundle), str(folder), "1.0.0")
    assert built.returncode == 0, built.stderr
    yield folder, bundle
    # Whatever a failed test left running.
    pid_file = folder / "app/lectern.pid"
    if pid_file.exists() and _alive(int(pid_file.read_text())):
        os.kill(int(pid_file.read_text()), signal.SIGKILL)


@contextlib.contextmanager
def _app(tmp_path: Path, bundle: Path) -> Iterator[subprocess.Popen]:
    app = subprocess.Popen(  # noqa: S603
        [str(bundle / "Contents/MacOS/Lectern"), "--background"],
        env={"PATH": "/usr/bin:/bin", "CFFIXED_USER_HOME": str(tmp_path / "home")},
    )
    try:
        yield app
    finally:
        app.kill()
        app.wait()


@needs_a_desktop
def test_the_app_runs_lectern_keeps_the_token_out_of_its_log_and_stops_it_on_quit(
    tmp_path, installed
):
    folder, bundle = installed
    pid_file = folder / "app/lectern.pid"
    log = tmp_path / "home/Library/Logs/Lectern/lectern.log"
    with _app(tmp_path, bundle) as app:
        _until(pid_file.exists, "the app started Lectern and wrote its process id")
        lectern = int(pid_file.read_text())
        assert _alive(lectern)
        _until(lambda: "an ordinary line" in _text(log), "the log")
        assert "SECRET" not in log.read_text() and "token" not in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600

        app.send_signal(signal.SIGTERM)  # what the installer sends: the app's own Shut Down
        assert app.wait(timeout=20) == 0
    stopped = folder / "app/venv/bin/lectern.stopped"
    assert stopped.read_text().strip() == "interrupted"  # SIGINT, as Ctrl+C: runners stop too
    assert "left running" not in log.read_text()  # nothing was left for the app to end
    assert not _alive(lectern) and not pid_file.exists()


@needs_a_desktop
def test_a_log_past_its_limit_is_moved_aside_when_lectern_starts(tmp_path, installed):
    """Only the start at launch can be driven from here; the one from Restart, in the menu,
    goes through the same start() and is checked by hand."""
    folder, bundle = installed
    log = tmp_path / "home/Library/Logs/Lectern/lectern.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"an old line\n" * 500_000)  # 6 MB, past the 5 MB limit
    with _app(tmp_path, bundle) as app:
        _until(lambda: "---- Lectern 1.0.0 started" in _text(log), "the app's first log line")
        app.send_signal(signal.SIGTERM)
        assert app.wait(timeout=20) == 0
    older = log.with_name("lectern.log.1")
    assert older.stat().st_size == 6_000_000
    assert "an old line" not in log.read_text() and log.stat().st_size < 10_000


@needs_a_desktop
@pytest.mark.parametrize(
    "runner_command",
    ["/bin/sleep 600", "/bin/sh -c 'trap \"\" TERM; while :; do sleep 1; done'"],
    ids=["a runner that ends on SIGTERM", "a runner that ignores SIGTERM"],
)
def test_shutting_down_ends_a_model_runner_lectern_left_behind(tmp_path, installed, runner_command):
    folder, bundle = installed
    lectern = folder / "app/venv/bin/lectern"
    # A Lectern that starts a runner and, told to stop, goes without stopping it.
    with_runner = f'{runner_command} &\necho $! > "$0.runner"\ntrap '
    lectern.write_text(STAND_IN.replace("trap ", with_runner))
    runner_file = lectern.with_name("lectern.runner")
    log = tmp_path / "home/Library/Logs/Lectern/lectern.log"
    with contextlib.ExitStack() as stack:
        app = stack.enter_context(_app(tmp_path, bundle))
        _until(runner_file.exists, "Lectern started its runner")
        runner = int(runner_file.read_text())
        stack.callback(lambda: _alive(runner) and os.kill(runner, signal.SIGKILL))
        _until(lambda: "an ordinary line" in _text(log), "Lectern is up")

        app.send_signal(signal.SIGTERM)  # Shut Down
        assert app.wait(timeout=20) == 0
        # Gone before the app left, not some time after. Failing, the log says why: with no
        # "ended … left running" line the runner was never counted; with one, it outlived its kill.
        assert not _alive(runner), _text(log)
    assert "ended 1 process(es) Lectern left running" in log.read_text()


# A Lectern whose runner is still starting when it's told to stop: the runner becomes its
# program (exec) once "$0.go" is there, and Lectern ends only after that, without stopping it.
STARTING_RUNNER = """#!/bin/sh
echo "Lectern is running at http://127.0.0.1:1/#token=SECRET"
echo "an ordinary line"
/bin/sh -c 'trap "" TERM; while [ ! -f "$0.go" ]; do sleep 0.05; done; exec /bin/sleep 600' "$0" &
runner=$!
echo "$runner" > "$0.runner"
trap 'echo interrupted > "$0.stopped"
while [ "$(ps -o comm= -p "$runner")" != /bin/sleep ]; do sleep 0.05; done
exit 0' INT
while :; do sleep 1; done
"""


@needs_a_desktop
def test_shutting_down_ends_a_runner_that_was_still_starting(tmp_path, installed):
    """Seen once in 19 runs of the test above: a runner the app noted in the moment between
    the two programs its start goes through (/bin/sh hands over to /bin/bash) was then a
    different program, so it wasn't taken for the one noted, and was left running."""
    folder, bundle = installed
    lectern = folder / "app/venv/bin/lectern"
    lectern.write_text(STARTING_RUNNER)
    runner_file = lectern.with_name("lectern.runner")
    log = tmp_path / "home/Library/Logs/Lectern/lectern.log"
    with contextlib.ExitStack() as stack:
        app = stack.enter_context(_app(tmp_path, bundle))
        _until(runner_file.exists, "Lectern started its runner")
        runner = int(runner_file.read_text())
        stack.callback(lambda: _alive(runner) and os.kill(runner, signal.SIGKILL))
        _until(lambda: "an ordinary line" in _text(log), "Lectern is up")

        app.send_signal(signal.SIGTERM)  # Shut Down: the app notes the runner, then interrupts
        _until(lectern.with_name("lectern.stopped").exists, "Lectern was interrupted")
        lectern.with_name("lectern.go").touch()  # the runner becomes /bin/sleep; Lectern ends
        assert app.wait(timeout=20) == 0
        assert not _alive(runner), _text(log)
    assert "ended 1 process(es) Lectern left running" in log.read_text()


@needs_a_desktop
@pytest.mark.parametrize("caught", ["running", "still starting"])
def test_the_app_takes_over_a_lectern_left_running_and_says_its_log_is_paused(
    tmp_path, installed, caught
):
    folder, bundle = installed
    pid_file = folder / "app/lectern.pid"
    log = tmp_path / "home/Library/Logs/Lectern/lectern.log"
    if caught == "still starting":
        # The app dies while Lectern is still its start script, a moment before it becomes
        # app/venv/bin/lectern. Here the moment is made long enough to land in.
        serve = folder / "bin/lectern-serve"
        serve.write_text(serve.read_text().replace("exec ", "sleep 3\nexec "))
    with _app(tmp_path, bundle) as crashed:
        _until(pid_file.exists, "the app started Lectern")
        lectern = int(pid_file.read_text())
        # The app's own first line, written just after the pid file: killed before it, the
        # app would leave no log, and no "started" line to count below.
        _until(lambda: "---- Lectern 1.0.0 started" in _text(log), "the app's first log line")
        if caught == "running":
            _until(lambda: "an ordinary line" in _text(log), "Lectern is up")
        crashed.kill()  # the app is force-quit: Lectern lives on
        crashed.wait()
    assert _alive(lectern)

    with _app(tmp_path, bundle) as app:
        _until(lambda: f"took over Lectern (process {lectern})" in _text(log), "the take-over")
        assert "isn't kept until Restart" in log.read_text()
        assert int(pid_file.read_text()) == lectern  # no second Lectern was started
        assert log.read_text().count("---- Lectern 1.0.0 started") == 1

        stopped = folder / "app/venv/bin/lectern.stopped"
        if caught == "still starting":  # let it become Lectern, so SIGINT is its to handle
            _until(
                lambda: "app/venv/bin/lectern" in _ps(lectern, "args="), "the start script ended"
            )
        app.send_signal(signal.SIGTERM)
        assert app.wait(timeout=20) == 0
    if caught == "running":  # (caught earlier, the stand-in may not have set its trap yet)
        assert stopped.exists()
    assert not _alive(lectern) and not pid_file.exists()


@needs_a_desktop
def test_the_app_leaves_alone_a_process_that_only_shares_the_number_in_the_pid_file(
    tmp_path, installed
):
    folder, bundle = installed
    pid_file = folder / "app/lectern.pid"
    with contextlib.ExitStack() as stack:
        other = subprocess.Popen(["/bin/sleep", "600"])  # noqa: S603
        stack.callback(other.wait)
        stack.callback(other.kill)
        pid_file.write_text(f"{other.pid}\n")  # a stale file; its number is another's now
        app = stack.enter_context(_app(tmp_path, bundle))
        _until(
            lambda: pid_file.exists() and pid_file.read_text().strip() != str(other.pid),
            "the app started its own Lectern",
        )
        app.send_signal(signal.SIGTERM)
        assert app.wait(timeout=20) == 0
        assert other.poll() is None  # never signalled


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
    # FAIL_CODESIGN: signing fails, saying why.
    "codesign": 'echo "codesign $*" >> "$LOG"; '
    'if [ -n "${FAIL_CODESIGN:-}" ]; then echo "errSecInternalComponent" >&2; exit 1; fi',
    "open": 'echo "open $*" >> "$LOG"',
    # FAIL_MV_NEW: the new app can't be moved into place (the swap is cut short).
    "mv": 'if [ -n "${FAIL_MV_NEW:-}" ] && [ "$1" != "${1%.app.new}" ]; then '
    'echo "mv: rename failed" >&2; exit 1; fi; exec /bin/mv "$@"',
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
        "LANG": "en_US.UTF-8",  # as on a real Mac: its bash 3.2 reads scripts differently in it
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
    assert (home / "bin/lectern").stat().st_mode & 0o111  # `lectern user …`, settings loaded
    assert f"{home}/bin/lectern user init <name>" in done.stdout

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


def test_an_app_whose_icon_cant_be_drawn_is_still_installed(tmp_path):
    # The fake swiftc's output can't be run, as a real one's couldn't on a Mac with no display.
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    assert "couldn't draw the app's icon" in done.stderr
    bundle = _bundle(tmp_path)
    assert (bundle / "Contents/MacOS/Lectern").exists()
    assert not (bundle / "Contents/Resources").exists()


def test_a_signing_failure_says_why(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"), FAIL_CODESIGN="1")
    assert done.returncode != 0
    assert "signing the app failed" in done.stderr and "errSecInternalComponent" in done.stderr


def test_an_install_in_another_folder_doesnt_take_over_the_app(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    other = tmp_path / "home/elsewhere"
    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"), LECTERN_DIR=str(other))
    assert done.returncode != 0
    assert f"runs the Lectern in {_home(tmp_path)}, not {other}" in done.stderr
    assert _version(_bundle(tmp_path)) == "0.1.0" and not other.exists()


def test_the_same_folder_with_a_trailing_slash_is_the_same_install(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"), LECTERN_DIR=f"{_home(tmp_path)}/")
    assert done.returncode == 0, done.stderr
    assert _version(_bundle(tmp_path)) == "0.2.0"


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


def test_the_line_offered_for_models_links_them_only_into_an_empty_place(tmp_path):
    _env(tmp_path)
    old = tmp_path / "home/Library/Application Support/Lectern/models"
    old.mkdir(parents=True)
    (old / "chat.gguf").touch()
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    offered = done.stdout.strip().splitlines()[-1]  # the command it says to run
    assert "ln -s" in offered
    models = _home(tmp_path) / "data/models"

    def run_it() -> str:
        ran = subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", offered], capture_output=True, text=True, timeout=60
        )
        assert ran.returncode == 0, ran.stderr
        return ran.stdout

    # Lectern has downloaded a model of its own since: nothing is linked, least of all inside.
    models.mkdir()
    (models / "own.gguf").touch()
    assert "not linked" in run_it()
    assert not models.is_symlink() and [p.name for p in models.iterdir()] == ["own.gguf"]

    (models / "own.gguf").unlink()  # the empty folder Lectern makes when it first starts
    assert run_it() == ""
    assert models.is_symlink() and (models / "chat.gguf").exists()


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
        # Asked to quit with SIGTERM, which the app treats as its own Shut Down.
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


def test_a_step_failing_after_the_app_was_quit_opens_it_again(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    url = _home(tmp_path) / "bin/lectern-url"
    url.chmod(0o444)  # it can't be written again: a step after the switch fails
    try:
        with _open_app(tmp_path) as app:
            done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
            assert app.wait(timeout=10) == -15  # it was quit for the switch
    finally:
        url.chmod(0o755)
    assert done.returncode != 0
    assert "opened again" in done.stderr
    assert _opens(tmp_path) == 2  # the first install's, and this one's on the way out
    assert _bundle(tmp_path).exists()


def test_a_swap_cut_short_puts_the_old_app_back_and_opens_it(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    bundle = _bundle(tmp_path)
    with _open_app(tmp_path) as app:
        done = _install(tmp_path, _wheel(tmp_path, "0.2.0"), FAIL_MV_NEW="1")
        assert app.wait(timeout=10) == -15
    assert done.returncode != 0 and "opened again" in done.stderr
    assert _version(bundle) == "0.1.0" and not bundle.with_name("Lectern.app.old").exists()
    assert _opens(tmp_path) == 2


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


def test_uninstall_stops_a_lectern_left_running_without_the_app_and_nothing_else(tmp_path):
    """The app was force-quit and Lectern lived on: its files mustn't go from under it, and it
    is stopped as the app would (SIGINT, so it stops its models too)."""
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home = _home(tmp_path)
    marker, ready = tmp_path / "stopped", tmp_path / "ready"
    lectern = home / "app/venv/bin/lectern"
    lectern.write_text(
        f"#!/bin/sh\ntrap 'echo interrupted > \"{marker}\"; exit 0' INT\n"
        f': > "{ready}"\nwhile :; do sleep 0.1; done\n'
    )
    lectern.chmod(0o755)
    left = subprocess.Popen([str(lectern), "serve"])  # noqa: S603
    try:
        _until(ready.exists, "Lectern is running, its SIGINT handled")
        (home / "app/lectern.pid").write_text(f"{left.pid}\n")
        done = _uninstall(tmp_path)
        assert done.returncode == 0, done.stderr
        assert left.wait(timeout=10) == 0 and marker.read_text().strip() == "interrupted"
        assert not (home / "app").exists()
    finally:
        left.kill()
        left.wait()


def test_uninstall_with_a_lectern_that_wont_stop_removes_nothing(tmp_path):
    """SIGINT, then SIGTERM, and it's still there: nothing goes from under it."""
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home, ready = _home(tmp_path), tmp_path / "ready"
    lectern = home / "app/venv/bin/lectern"
    lectern.write_text(
        f"#!/bin/sh\ntrap '' INT TERM\n: > \"{ready}\"\nwhile :; do sleep 0.1; done\n"
    )
    lectern.chmod(0o755)
    left = subprocess.Popen([str(lectern), "serve"])  # noqa: S603
    try:
        _until(ready.exists, "Lectern is running, ignoring SIGINT and SIGTERM")
        (home / "app/lectern.pid").write_text(f"{left.pid}\n")
        done = _uninstall(tmp_path, LECTERN_QUIT_WAIT="1")
        assert done.returncode == 1
        assert f"Lectern (process {left.pid}) didn't stop; nothing was removed." in done.stderr
        assert left.poll() is None
        assert _bundle(tmp_path).exists() and (home / "app/venv").exists()
    finally:
        left.kill()
        left.wait()


def test_uninstall_never_signals_another_process_with_the_number_in_the_pid_file(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    other = subprocess.Popen(["/bin/sleep", "600"])  # noqa: S603
    try:
        (_home(tmp_path) / "app/lectern.pid").write_text(f"{other.pid}\n")
        assert _uninstall(tmp_path).returncode == 0
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


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


# --- MLX --------------------------------------------------------------------------------------


def test_on_a_mac_lectern_is_installed_with_mlx(tmp_path):
    wheel = _wheel(tmp_path, "0.1.0")
    assert _install(tmp_path, wheel).returncode == 0
    (installed,) = [line for line in _log(tmp_path) if line.startswith("uv pip install")]
    # The wheel with its `mlx` extra: MLX's text server, which Models & memory can switch on.
    assert installed.endswith(f"{wheel}[mlx]")
