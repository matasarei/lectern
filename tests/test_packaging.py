"""Packaging for a host install (M4): the wheel, and packaging/linux/install.sh."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT / "src/lectern/web_dist/index.html").exists(),
                    reason="the web UI isn't built (npm --prefix web run build)")  # fmt: skip
def test_the_wheel_carries_the_web_ui_and_the_model_catalog(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv isn't installed")
    built = subprocess.run(  # noqa: S603
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(ROOT)],  # noqa: S607
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    if built.returncode != 0:
        pytest.skip(f"uv couldn't build offline: {built.stderr.strip()[-200:]}")
    (wheel,) = tmp_path.glob("lectern_ai-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "lectern/web_dist/index.html" in names
    assert any(n.startswith("lectern/web_dist/assets/") for n in names)
    assert "lectern/catalog/models.yaml" in names


# --- install.sh / uninstall.sh in a fake home, with fake system commands ------------------

LINUX = ROOT / "packaging" / "linux"
FAKES = {
    # Every call is logged as "<name> <args>"; a few answer like the real thing would.
    "systemctl": 'echo "systemctl $*" >> "$LOG"; case " $* " in '
    '*" is-active "*) [ -f "$STATE/active" ];; *" show-environment "*) [ ! -f "$STATE/nobus" ];; '
    "esac",
    # loginctl: lingering is off until enabled; enabling can be refused (STATE/linger-denied).
    "loginctl": 'echo "loginctl $*" >> "$LOG"; case "$1" in '
    'show-user) cat "$STATE/linger" 2>/dev/null || echo no;; esac; case " $* " in '
    '*" enable-linger "*) [ ! -f "$STATE/linger-denied" ] && echo yes > "$STATE/linger";; esac',
    # The tests run on macOS too: the installer is told it's on Linux x86_64.
    "uname": 'case "$1" in -s) echo Linux;; -m) echo x86_64;; *) /usr/bin/uname "$@";; esac',
    # uv: `venv` makes the folder with a python and a lectern; `pip install` can be told to
    # fail (FAIL_PIP).
    "uv": 'echo "uv $*" >> "$LOG"; '
    'if [ "$1" = venv ]; then d="${@: -1}"; mkdir -p "$d/bin"; touch "$d/bin/python"; '
    'printf "#!/bin/sh\\n" > "$d/bin/lectern"; '
    'elif [ "$1" = pip ] && [ -n "${FAIL_PIP:-}" ]; then echo "no network" >&2; exit 1; '
    'elif [ "$1" = --version ]; then echo "uv 0.12.23"; fi',
    # curl: the service's /health answers unless told not to (STATE/down); anything else is
    # the real curl (the uv download in the checksum test).
    "curl": 'case "$*" in *127.0.0.1:*/health*) echo "curl $*" >> "$LOG"; '
    '[ ! -f "$STATE/down" ];; *) exec /usr/bin/curl "$@";; esac',
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
        "HOME": str(tmp_path / "home"),  # ~/.lectern and ~/.config are the defaults
        "USER": "someone",
        "LECTERN_UV_BIN": str(bin_dir / "uv"),
        "LECTERN_DRI": str(tmp_path / "no-dri"),
        "LECTERN_HEALTH_WAIT": "2",
        "LOG": str(tmp_path / "log"),
        "STATE": str(state),
        **extra,
    }


def _install(tmp_path: Path, wheel: Path, *, fail_pip: bool = False, script: str = "install.sh",
             args: tuple[str, ...] = (), **extra: str) -> subprocess.CompletedProcess:  # fmt: skip
    env = _env(tmp_path, **({"FAIL_PIP": "1"} if fail_pip else {}), **extra)
    return subprocess.run(  # noqa: S603
        ["bash", str(LINUX / script), *([str(wheel)] if script == "install.sh" else []), *args],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def _wheel(tmp_path: Path, version: str) -> Path:
    wheel = tmp_path / f"lectern_ai-{version}-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    return wheel


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "log").read_text().splitlines()


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home/.lectern"


def test_a_first_install_puts_everything_in_the_home_folder_and_starts_a_user_service(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    home = _home(tmp_path)
    venv = (home / "app/venv").resolve()
    assert venv.parent == (home / "app/versions").resolve()
    assert venv.name.startswith("0.1.0-") and (venv / "bin/lectern").exists()
    env = (home / "lectern.env").read_text()
    assert f"LECTERN_HOME={home}/data" in env and "LECTERN_PUBLIC_URL=" in env
    assert "\n# LECTERN_HOST=0.0.0.0\n" in env  # offered, never on by default
    assert (home / "data").is_dir()
    unit = (tmp_path / "home/.config/systemd/user/lectern.service").read_text()
    assert f"ExecStart={home}/app/venv/bin/lectern serve" in unit
    assert f"EnvironmentFile={home}/lectern.env" in unit
    assert "WantedBy=default.target" in unit and "User=" not in unit and "@LECTERN_DIR@" not in unit
    assert "KillSignal=SIGINT" in unit
    assert (home / "bin/lectern-url").stat().st_mode & 0o111
    assert f"{home}/lectern.env" in (home / "bin/lectern-url").read_text()
    log = _log(tmp_path)
    systemctl = [c for c in log if c.startswith("systemctl ")]
    assert systemctl and all(c.startswith("systemctl --user ") for c in systemctl)
    assert "systemctl --user enable --now lectern" in log
    assert not any(c.split()[0] in ("sudo", "useradd", "usermod", "chown") for c in log)
    assert any(c.startswith("uv venv --quiet --python 3.12") for c in log)
    assert "Lectern 0.1.0 is installed and running." in done.stdout
    # Nothing written outside the home folder (beyond the test's own fakes and wheels).
    outside = {p.name for p in tmp_path.iterdir()} - {"home", "fakebin", "state", "log"}
    assert all(name.endswith(".whl") for name in outside), outside


def test_the_installed_lectern_command_loads_the_settings_and_passes_its_arguments_on(tmp_path):
    """Run bare, the installed `lectern` keeps its data in the OS's app-data folder: `user
    init` there would turn accounts on in a database the service never reads."""
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    home = _home(tmp_path)
    # A stand-in for the installed `lectern` that says how it was run.
    (home / "app/venv/bin/lectern").write_text(
        '#!/bin/sh\necho "home: $LECTERN_HOME"\nfor a in "$@"; do echo "arg: $a"; done\nexit 3\n'
    )
    (home / "app/venv/bin/lectern").chmod(0o755)
    command = home / "bin/lectern"
    assert command.stat().st_mode & 0o111
    ran = subprocess.run(  # noqa: S603
        [str(command), "user", "add", "two words"], env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert ran.stdout.splitlines() == [
        f"home: {home}/data", "arg: user", "arg: add", "arg: two words",
    ]  # fmt: skip
    assert ran.returncode == 3  # its answer is the command's own
    assert f"{command} user init <name>" in done.stdout  # and the installer names it


def test_on_linux_lectern_is_installed_without_extras(tmp_path):
    wheel = _wheel(tmp_path, "0.1.0")
    assert _install(tmp_path, wheel).returncode == 0
    (installed,) = [line for line in _log(tmp_path) if line.startswith("uv pip install")]
    assert installed.endswith(str(wheel))  # the bare wheel: MLX is the Mac installer's


def test_run_as_root_it_refuses(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "fakebin/id").write_text(
        '#!/usr/bin/env bash\nif [ "$1" = -u ]; then echo 0; else /usr/bin/id "$@"; fi\n'
    )
    (tmp_path / "fakebin/id").chmod(0o755)
    done = subprocess.run(  # noqa: S603
        ["bash", str(LINUX / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "run it as yourself" in done.stderr
    assert not _home(tmp_path).exists()


def test_without_a_user_session_it_says_how_to_get_one(tmp_path):
    _env(tmp_path)
    (tmp_path / "state/nobus").touch()
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 1 and "systemctl --user" in done.stderr
    assert "machinectl shell" in done.stderr


def test_without_user_set_it_still_names_you(tmp_path):
    env = _env(tmp_path)
    del env["USER"]
    (tmp_path / "state/nobus").touch()
    done = subprocess.run(  # noqa: S603
        ["bash", str(LINUX / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "unbound variable" not in done.stderr
    assert f"machinectl shell {getpass.getuser()}@" in done.stderr


def test_lingering_is_turned_on_so_it_starts_at_boot(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    linger = "loginctl --no-ask-password enable-linger"  # never a password prompt
    assert linger in _log(tmp_path)
    assert "sudo loginctl enable-linger" not in done.stdout

    again = _install(tmp_path, _wheel(tmp_path, "0.1.0"))  # already on: left alone
    assert _log(tmp_path).count(linger) == 1, again.stdout


def test_lingering_refused_prints_the_sudo_line(tmp_path):
    _env(tmp_path)
    (tmp_path / "state/linger-denied").touch()
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    assert "sudo loginctl enable-linger someone" in done.stdout


def test_a_gpu_render_node_without_the_render_group_prints_the_usermod_line(tmp_path):
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").touch()
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"), LECTERN_DRI=str(dri))
    assert done.returncode == 0, done.stderr
    assert "sudo usermod -aG render,video someone" in done.stdout
    assert "then reboot" in done.stdout and "log out and back in" not in done.stdout


def test_an_upgrade_keeps_the_settings_and_restarts_and_keeps_one_version_back(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.0.9")).returncode == 0
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    env_file = _home(tmp_path) / "lectern.env"
    public = "LECTERN_PUBLIC_URL=http://lectern.lan"
    env_file.write_text(env_file.read_text().replace("LECTERN_PUBLIC_URL=", public))
    (tmp_path / "state/active").touch()

    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
    assert done.returncode == 0, done.stderr
    assert "LECTERN_PUBLIC_URL=http://lectern.lan" in env_file.read_text()  # kept
    versions = sorted(p.name for p in (_home(tmp_path) / "app/versions").iterdir())
    assert [v.split("-")[0] for v in versions] == ["0.1.0", "0.2.0"]  # 0.0.9 dropped
    assert "systemctl --user restart lectern" in _log(tmp_path)
    assert any(c.endswith("/health") for c in _log(tmp_path) if c.startswith("curl"))
    assert any("--refresh-package lectern-ai" in c for c in _log(tmp_path) if "pip install" in c)


def test_a_failed_upgrade_leaves_the_version_in_use_as_it_was(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    before = (_home(tmp_path) / "app/venv").resolve()

    failed = _install(tmp_path, _wheel(tmp_path, "0.2.0"), fail_pip=True)
    assert failed.returncode == 1 and "the version in use is unchanged" in failed.stderr
    assert (_home(tmp_path) / "app/venv").resolve() == before and before.exists()
    versions = list((_home(tmp_path) / "app/versions").iterdir())
    assert versions == [before]  # nothing half-made left behind


def test_an_upgrade_that_doesnt_start_says_so_and_keeps_the_previous_version(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    before = (_home(tmp_path) / "app/venv").resolve()
    (tmp_path / "state/active").touch()
    (tmp_path / "state/down").touch()

    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
    assert done.returncode == 1
    assert "Lectern 0.2.0 didn't start" in done.stderr
    assert "journalctl --user -u lectern" in done.stderr
    assert f"ln -sfn {before}" in done.stderr and "systemctl --user restart lectern" in done.stderr
    assert before.exists() and "Upgraded" not in done.stdout


def test_a_wrong_uv_checksum_is_refused(tmp_path):
    fake = tmp_path / "uv.tar.gz"
    fake.write_bytes(b"not uv")
    env = _env(tmp_path, LECTERN_UV_URL=fake.as_uri())
    del env["LECTERN_UV_BIN"]
    done = subprocess.run(  # noqa: S603
        ["bash", str(LINUX / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "uv's checksum is wrong" in done.stderr
    assert not (_home(tmp_path) / "app/bin/uv").exists()


def test_a_wheel_that_isnt_lecterns_is_refused(tmp_path):
    other = tmp_path / "something-1.0-py3-none-any.whl"
    other.write_bytes(b"PK")
    done = _install(tmp_path, other)
    assert done.returncode == 1 and "isn't a Lectern wheel" in done.stderr


def test_uninstall_keeps_the_data_unless_purged(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home = _home(tmp_path)
    (home / "data/models").mkdir(parents=True)
    (home / "src").mkdir()
    unit = tmp_path / "home/.config/systemd/user/lectern.service"

    kept = _install(tmp_path, Path("-"), script="uninstall.sh")
    assert kept.returncode == 0, kept.stderr
    assert not (home / "app").exists() and not (home / "bin").exists() and not unit.exists()
    assert (home / "data/models").exists() and (home / "lectern.env").exists()
    assert (home / "src").exists()
    assert "systemctl --user disable --now lectern" in _log(tmp_path)

    purged = _install(tmp_path, Path("-"), script="uninstall.sh", args=("--purge",))
    assert purged.returncode == 0, purged.stderr
    assert not (home / "data").exists() and not (home / "lectern.env").exists()
    assert (home / "src").exists()  # the copy it may be running from


@pytest.mark.parametrize("args", [(), ("--purge",)], ids=["uninstall", "purge"])
def test_uninstall_under_a_folder_that_isnt_an_install_removes_nothing(tmp_path, args):
    """LECTERN_DIR=~/work: its app, bin and data are someone else's."""
    work = tmp_path / "home/work"
    for name in ("app", "bin", "data"):
        (work / name).mkdir(parents=True)
        (work / name / "keep.txt").write_text("not Lectern's")

    done = _install(tmp_path, Path("-"), script="uninstall.sh", args=args, LECTERN_DIR=str(work))

    assert done.returncode == 1 and "no Lectern installed" in done.stderr
    assert all((work / name / "keep.txt").exists() for name in ("app", "bin", "data"))
    assert not any("disable" in line for line in _log(tmp_path))  # the service is left alone


def test_uninstall_of_an_install_without_its_settings_still_removes_it(tmp_path):
    """Either sign of an install is enough: here only its versions folder is left."""
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    home = _home(tmp_path)
    (home / "lectern.env").unlink()

    done = _install(tmp_path, Path("-"), script="uninstall.sh")

    assert done.returncode == 0, done.stderr
    assert not (home / "app").exists() and not (home / "bin").exists()


def test_uninstall_without_a_user_session_removes_nothing(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    (tmp_path / "state/nobus").touch()
    done = _install(tmp_path, Path("-"), script="uninstall.sh")
    assert done.returncode == 1 and "systemctl --user" in done.stderr
    assert (_home(tmp_path) / "app/venv").exists()
    assert (tmp_path / "home/.config/systemd/user/lectern.service").exists()


def test_compose_passes_on_every_setting_serve_reads():
    import yaml

    compose = yaml.safe_load((ROOT / "packaging/docker/compose.yaml").read_text())
    environment = compose["services"]["lectern"]["environment"]
    assert {"LECTERN_PUBLIC_URL", "LECTERN_PORT", "LECTERN_HOST"} <= set(environment)


DOCKERFILES = (
    ROOT / "packaging/docker/Dockerfile",
    ROOT / "src/lectern/modules/sandbox/image/Dockerfile",
)


def _pin(name: str) -> str:
    """A value from packaging/pins.sh, read as text (it's sourced by shell, not run here)."""
    found = re.search(rf'^{name}="([^"]+)"$', (ROOT / "packaging/pins.sh").read_text(), re.M)
    assert found, f"{name} is missing from packaging/pins.sh"
    return found.group(1)


def test_the_python_image_pin_is_a_digest():
    assert re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", _pin("PYTHON_IMAGE"))


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_every_dockerfile_builds_from_the_pinned_python_image(dockerfile):
    froms = re.findall(r"^FROM (\S+)", dockerfile.read_text(), re.M)
    assert froms == [_pin("PYTHON_IMAGE")]


def _windows_pin(name: str) -> str:
    """A value from packaging/windows/pins.ps1, read as text."""
    text = (ROOT / "packaging/windows/pins.ps1").read_text()
    found = re.search(rf"^\${name} = ['\"]([^'\"]+)['\"]$", text, re.M)
    assert found, f"${name} is missing from packaging/windows/pins.ps1"
    return found.group(1)


def test_windows_uses_the_same_uv_as_linux_and_the_mac():
    assert _windows_pin("UvVersion") == _pin("UV_VERSION")
    url = _windows_pin("UvWindowsX64Url").replace("$UvVersion", _windows_pin("UvVersion"))
    assert url == (
        f"https://github.com/astral-sh/uv/releases/download/{_pin('UV_VERSION')}/"
        "uv-x86_64-pc-windows-msvc.zip"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", _windows_pin("UvWindowsX64Sha256"))


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck isn't installed")
def test_the_scripts_pass_shellcheck():
    scripts = [
        LINUX / "install.sh",
        LINUX / "uninstall.sh",
        ROOT / "packaging/build_wheel.sh",
        ROOT / "packaging/make_package.sh",
        ROOT / "packaging/pins.sh",  # sourced, but checked on its own too
        ROOT / "packaging/install_lib.sh",  # the same
        ROOT / "packaging/macos/install.sh",
        ROOT / "packaging/macos/uninstall.sh",
        ROOT / "packaging/macos/build_app.sh",
        ROOT / "install.sh",
        ROOT / "uninstall.sh",
    ]
    checked = subprocess.run(["shellcheck", "-x", *map(str, scripts)],  # noqa: S603, S607
                             capture_output=True, text=True, cwd=ROOT)  # fmt: skip
    assert checked.returncode == 0, checked.stdout


def test_no_script_puts_a_non_ascii_character_right_after_a_variable():
    """macOS's /bin/bash (3.2), in a UTF-8 locale, reads the first byte of "…" as part of the
    name before it: "$version…" is then an unbound variable and the install stops. ${version}…
    is safe. The tests' bare environment has no such locale, so they can't catch it by running."""
    scripts = [ROOT / "install.sh", ROOT / "uninstall.sh", *(ROOT / "packaging").rglob("*.sh")]
    found = [
        f"{script.relative_to(ROOT)}:{number}: {line.strip()}"
        for script in scripts
        for number, line in enumerate(script.read_text().splitlines(), 1)
        if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line)
    ]
    assert not found, "\n".join(found)


def test_the_package_carries_every_file_the_installer_sources():
    """make_package.sh copies by name, so a file install.sh sources and the package lacks
    would only show on the server it's unpacked on."""
    sourced = re.findall(r'^\. "\$here/\.\./([\w.]+)"', (LINUX / "install.sh").read_text(), re.M)
    assert "pins.sh" in sourced
    script = (ROOT / "packaging/make_package.sh").read_text()
    (copied,) = re.findall(r'^cp -R (.+) "\$stage/\$name/packaging/"$', script, re.M)
    missing = [name for name in sourced if f"packaging/{name}" not in copied.split()]
    assert not missing, f"make_package.sh doesn't copy {missing}"


def _copy_scripts(to: Path) -> None:
    """The install scripts as a package or a working copy has them."""
    (to / "packaging").mkdir(parents=True)
    shutil.copytree(LINUX, to / "packaging/linux")
    shutil.copy(ROOT / "packaging/pins.sh", to / "packaging/pins.sh")
    shutil.copy(ROOT / "packaging/install_lib.sh", to / "packaging/install_lib.sh")
    shutil.copy(ROOT / "install.sh", to / "install.sh")


def _run_top(tmp_path: Path, top: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(top / "install.sh")], env=_env(tmp_path, **extra),  # noqa: S603, S607
                          capture_output=True, text=True, timeout=60)  # fmt: skip


def test_a_package_installs_the_wheel_beside_it_with_no_argument(tmp_path):
    package = tmp_path / "lectern-0.3.0"
    _copy_scripts(package)
    (package / "lectern_ai-0.3.0-py3-none-any.whl").write_bytes(b"PK")

    done = _run_top(tmp_path, package)
    assert done.returncode == 0, done.stderr
    assert (_home(tmp_path) / "app/venv").resolve().name.startswith("0.3.0-")
    assert "Lectern runs on this machine at http://127.0.0.1:8765" in done.stdout


def test_a_working_copy_without_node_says_how_to_get_it(tmp_path):
    copy = tmp_path / "aihub"
    _copy_scripts(copy)
    (copy / "pyproject.toml").write_text("[project]\nname = 'lectern-ai'\n")
    (copy / "web").mkdir()

    done = _run_top(tmp_path, copy, LECTERN_NPM="no-such-npm")
    assert done.returncode == 1
    assert "building the web UI needs Node.js (npm)" in done.stderr
    assert "packaging/make_package.sh" in done.stderr
    assert not (_home(tmp_path) / "app/venv").exists()


def test_a_folder_with_neither_a_wheel_nor_the_source_says_so(tmp_path):
    empty = tmp_path / "somewhere"
    _copy_scripts(empty)
    done = _run_top(tmp_path, empty)
    assert done.returncode == 1 and "no Lectern wheel next to this script" in done.stderr


def test_lectern_dir_moves_the_whole_install(tmp_path):
    elsewhere = tmp_path / "disk2/lectern"
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"), LECTERN_DIR=str(elsewhere))
    assert done.returncode == 0, done.stderr
    assert (elsewhere / "app/venv/bin/lectern").exists()
    assert f"LECTERN_HOME={elsewhere}/data" in (elsewhere / "lectern.env").read_text()
    unit = (tmp_path / "home/.config/systemd/user/lectern.service").read_text()
    assert f"ExecStart={elsewhere}/app/venv/bin/lectern serve" in unit
    assert not _home(tmp_path).exists()


@pytest.mark.parametrize("where", ["has space/lectern", "a|b/lectern", "a&b/lectern"])
def test_an_install_folder_the_unit_cant_hold_is_refused(tmp_path, where):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"), LECTERN_DIR=str(tmp_path / where))
    assert done.returncode == 1 and "can't hold spaces or special characters" in done.stderr
    assert not (tmp_path / where).exists()


def test_an_update_with_the_same_version_replaces_the_code_and_keeps_the_data(tmp_path):
    """A small fix built without a version bump: the new code goes in, the data stays."""
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    data = _home(tmp_path) / "data"
    (data / "lectern.sqlite").write_text("the app's database")
    (data / "users/local/projects/Lease").mkdir(parents=True)
    (data / "token").write_text("t")
    first = (_home(tmp_path) / "app/venv").resolve()
    (tmp_path / "state/active").touch()

    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    second = (_home(tmp_path) / "app/venv").resolve()
    assert second != first and first.exists()  # a fresh install; the old one kept to go back
    assert (data / "lectern.sqlite").read_text() == "the app's database"
    assert (data / "users/local/projects/Lease").is_dir() and (data / "token").read_text() == "t"
    assert "Upgraded to Lectern 0.1.0" in done.stdout


# --- the Makefile: shorthands for the scripts above ----------------------------------------------


@pytest.mark.parametrize(
    ("args", "runs"),
    [
        (["install"], "./install.sh"),
        (["uninstall"], "./uninstall.sh"),
        (["uninstall", "PURGE=1"], "./uninstall.sh --purge"),
        (["uninstall", "PURGE=0"], "./uninstall.sh"),  # anything but yes keeps the data
        (["purge"], "./uninstall.sh --purge"),
    ],
)
def test_make_runs_the_script_at_the_root(args, runs):
    if shutil.which("make") is None:
        pytest.skip("make isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["make", "-n", "-C", str(ROOT), *args],  # noqa: S607
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    # Stripped: the wipe is the last line of a longer command, indented under the question.
    assert runs in [line.strip() for line in shown.stdout.splitlines()]


def test_a_purge_left_in_the_environment_doesnt_wipe():
    """Make takes the environment's variables as its own: a PURGE=1 exported for something
    else, or left from a test, mustn't turn a plain uninstall into a wipe of the models."""
    if shutil.which("make") is None:
        pytest.skip("make isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["make", "-n", "-C", str(ROOT), "uninstall"],  # noqa: S607
        capture_output=True, text=True, timeout=30, env={**os.environ, "PURGE": "1"},
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    # Not the whole output: GNU Make 4 adds "Entering directory" lines with -C, 3.81 doesn't.
    assert "./uninstall.sh" in shown.stdout.splitlines()
    assert "--purge" not in shown.stdout


@pytest.mark.parametrize(
    ("args", "answer", "runs"),
    [
        (["purge"], "y\n", "--purge"),
        (["purge"], "yes\n", "--purge"),
        (["purge"], "n\n", None),
        (["purge"], "\n", None),  # Enter alone keeps everything
        (["purge"], "", None),  # nobody at the keyboard: no answer, no wipe
        (["uninstall", "PURGE=1"], "y\n", "--purge"),
        (["uninstall", "PURGE=1"], "n\n", None),
        (["uninstall"], "", ""),  # keeps the data, so it doesn't ask
        # -i (ignore errors) goes on past a failed command: a No must still stop the wipe
        (["-i", "purge"], "n\n", None),
        (["-i", "uninstall", "PURGE=1"], "n\n", None),
    ],
)
def test_make_asks_before_a_full_wipe(tmp_path, args, answer, runs):
    """A wipe deletes every model and project, and nothing brings them back. Run for real, on
    a copy of the Makefile beside an uninstall.sh that only writes down what it was given."""
    if shutil.which("make") is None:
        pytest.skip("make isn't installed")
    shutil.copy(ROOT / "Makefile", tmp_path)
    ran = tmp_path / "ran"
    (tmp_path / "uninstall.sh").write_text(f"#!/bin/sh\nprintf %s \"$*\" > '{ran}'\n")
    (tmp_path / "uninstall.sh").chmod(0o755)
    home = tmp_path / "Lectern's home"  # a quote and a space mustn't break the question
    done = subprocess.run(  # noqa: S603
        ["make", *args, f"LECTERN_DIR={home}"],  # noqa: S607
        capture_output=True, text=True, timeout=30, cwd=tmp_path, input=answer,
    )  # fmt: skip
    if runs is None:
        assert done.returncode != 0 or "-i" in args  # -i also ignores the No's exit status
        assert not ran.exists()
        assert "Nothing removed" in done.stdout + done.stderr
    else:
        assert done.returncode == 0, done.stderr
        assert ran.read_text() == runs
    if "--purge" in (runs, None):
        assert str(home) in done.stdout  # it says which folder goes


def test_make_from_another_folder_says_where_to_run_it(tmp_path):
    """Its commands are relative to the repository: run from elsewhere with -f, they would
    fail one by one ("./install.sh: No such file or directory"), or build in the wrong place."""
    if shutil.which("make") is None:
        pytest.skip("make isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["make", "-n", "-f", str(ROOT / "Makefile"), "install"],  # noqa: S607
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )  # fmt: skip
    assert shown.returncode != 0
    assert f"make -C {ROOT}" in shown.stderr
    assert "install.sh" not in shown.stdout


def test_make_alone_builds_for_development_and_installs_nothing():
    if shutil.which("make") is None:
        pytest.skip("make isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["make", "-n", "-C", str(ROOT)], capture_output=True, text=True, timeout=30  # noqa: S607
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    assert "uv sync" in shown.stdout and "npm --prefix web run build" in shown.stdout
    assert "install.sh" not in shown.stdout


# --- the justfile: the Makefile's shorthands, on Windows too ----------------------------------

WINDOWS_HOST = os.name == "nt"
POWERSHELL_FILE = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File"


@pytest.mark.parametrize(
    ("recipe", "unix", "windows"),
    [
        ("install", "./install.sh", f"{POWERSHELL_FILE} install.ps1"),
        ("uninstall", "./uninstall.sh", f"{POWERSHELL_FILE} uninstall.ps1"),
        # On Unix the wipe is the last line of the script that asks first.
        ("purge", "exec ./uninstall.sh --purge", f"{POWERSHELL_FILE} uninstall.ps1 -Purge"),
    ],
)
def test_just_runs_the_script_at_the_root(recipe, unix, windows):
    if shutil.which("just") is None:
        pytest.skip("just isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["just", "--dry-run", "--justfile", str(ROOT / "justfile"), recipe],  # noqa: S607
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    # just shows what it would run on stderr.
    runs = windows if WINDOWS_HOST else unix
    assert runs in [line.strip() for line in shown.stderr.splitlines()]


def test_just_alone_builds_for_development_and_installs_nothing():
    if shutil.which("just") is None:
        pytest.skip("just isn't installed")
    shown = subprocess.run(  # noqa: S603
        ["just", "--dry-run", "--justfile", str(ROOT / "justfile")],  # noqa: S607
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    assert "uv sync" in shown.stderr and "npm --prefix web run build" in shown.stderr
    assert "install.sh" not in shown.stderr and "install.ps1" not in shown.stderr


def test_just_from_another_folder_runs_the_scripts_at_the_root(tmp_path):
    """The recipes call ./install.sh and install.ps1: just runs them in the justfile's folder,
    wherever it is called from. Run for real, on a copy beside an installer that only writes
    down that it ran."""
    if shutil.which("just") is None:
        pytest.skip("just isn't installed")
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    repo.mkdir()
    elsewhere.mkdir()
    shutil.copy(ROOT / "justfile", repo)
    ran = tmp_path / "ran"
    if WINDOWS_HOST:
        (repo / "install.ps1").write_text(f"Set-Content -LiteralPath '{ran}' -Value ran\n")
    else:
        (repo / "install.sh").write_text(f"#!/bin/sh\ntouch '{ran}'\n")
        (repo / "install.sh").chmod(0o755)
    done = subprocess.run(  # noqa: S603
        ["just", "--justfile", str(repo / "justfile"), "install"],  # noqa: S607
        capture_output=True, text=True, timeout=60, cwd=elsewhere,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert ran.exists()


def test_just_passes_a_failed_script_on():
    """An installer that fails must fail the recipe, not end in a success."""
    if shutil.which("just") is None:
        pytest.skip("just isn't installed")
    if not WINDOWS_HOST:
        pytest.skip("the PowerShell recipes run on Windows only")
    done = subprocess.run(  # noqa: S603
        ["just", "--justfile", str(ROOT / "justfile"), "uninstall"],  # noqa: S607
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "LECTERN_DIR": str(ROOT / "no-such-install")},
    )  # fmt: skip
    assert done.returncode != 0
    assert "nothing was removed" in done.stderr


@pytest.mark.parametrize(
    ("answer", "runs"),
    [
        ("y\n", "--purge"),
        ("yes\n", "--purge"),
        ("n\n", None),
        ("\n", None),  # Enter alone keeps everything
        ("", None),  # nobody at the keyboard: no answer, no wipe
    ],
)
def test_just_asks_before_a_full_wipe(tmp_path, answer, runs):
    """As make purge: run for real, on a copy of the justfile beside an uninstall.sh that only
    writes down what it was given. (On Windows uninstall.ps1 -Purge asks for itself.)"""
    if shutil.which("just") is None:
        pytest.skip("just isn't installed")
    if WINDOWS_HOST:
        pytest.skip("the question is the Unix recipe's; on Windows uninstall.ps1 asks")
    shutil.copy(ROOT / "justfile", tmp_path)
    ran = tmp_path / "ran"
    (tmp_path / "uninstall.sh").write_text(f"#!/bin/sh\nprintf %s \"$*\" > '{ran}'\n")
    (tmp_path / "uninstall.sh").chmod(0o755)
    home = tmp_path / "Lectern's home"  # a quote and a space mustn't break the question
    done = subprocess.run(  # noqa: S603
        ["just", "purge"],  # noqa: S607
        capture_output=True, text=True, timeout=30, cwd=tmp_path, input=answer,
        env={**os.environ, "LECTERN_DIR": str(home)},
    )  # fmt: skip
    if runs is None:
        assert done.returncode != 0
        assert not ran.exists()
        assert "Nothing removed" in done.stdout + done.stderr
    else:
        assert done.returncode == 0, done.stderr
        assert ran.read_text() == runs
    assert str(home) in done.stdout  # it says which folder goes


# --- Windows, checked from here: compiled and linted in containers ---------------------------

WINDOWS = ROOT / "packaging/windows"
# Mono's C# compiler stands in for the csc.exe that ships with Windows (.NET Framework 4,
# C# 5): the same language version, and the WinForms and Win32 calls the app makes.
MONO_IMAGE = "mono:6.12@sha256:34d816779b1248b5cfd095770b64ecbaf1798e2aca693a91c11a018dce9c7ad5"


def _image_here(image: str) -> bool:
    """Docker answers and has the image already: these checks never pull hundreds of MB."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    found = subprocess.run([docker, "image", "inspect", image], capture_output=True, timeout=60)  # noqa: S603
    return found.returncode == 0


@pytest.mark.skipif(not _image_here(MONO_IMAGE), reason=f"needs Docker with {MONO_IMAGE}")
def test_the_windows_tray_app_compiles_as_csharp_5():
    built = subprocess.run(  # noqa: S603 (fixed argv)
        [shutil.which("docker"), "run", "--rm", "--tmpfs", "/work", "-w", "/work",
         "-v", f"{WINDOWS / 'tray'}:/src:ro", MONO_IMAGE,
         "mcs", "-langversion:5", "-target:winexe", "-codepage:65001", "-warnaserror",
         "-r:System.Windows.Forms.dll", "-r:System.Drawing.dll", "-out:/work/Lectern.exe",
         "/src/Lectern.cs"],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert built.returncode == 0, built.stdout + built.stderr


def test_the_windows_icon_has_the_sizes_windows_asks_for():
    from PIL import Image

    with Image.open(WINDOWS / "tray/lectern.ico") as icon:
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(icon.info["sizes"])


POWERSHELL_IMAGE = (  # 7.4-ubuntu-22.04
    "mcr.microsoft.com/powershell"
    "@sha256:62300a213a9293916333df2b014cd3a8f22fb0b0b65f2bb446aaf436bcf8c868"
)
WINDOWS_SCRIPTS = [
    ROOT / "install.ps1",
    ROOT / "uninstall.ps1",
    *sorted(WINDOWS.glob("*.ps1")),
]


@pytest.mark.parametrize("script", WINDOWS_SCRIPTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_the_windows_scripts_are_ascii(script):
    """Windows PowerShell 5.1 reads a script with no byte-order mark in the system's code page:
    anything but ASCII comes out garbled."""
    script.read_bytes().decode("ascii")


@pytest.mark.parametrize("script", WINDOWS_SCRIPTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_the_windows_scripts_never_delete_a_folder_with_remove_item(script):
    """Remove-Item -Recurse in Windows PowerShell 5.1 follows a junction and deletes what it
    points at (a models folder linked in from elsewhere): folders go with cmd's rmdir."""
    for line in script.read_text().splitlines():
        code = line.split("#", 1)[0]  # what a comment says doesn't count
        assert not ("Remove-Item" in code and "-Recurse" in code), line


@pytest.mark.skipif(
    not _image_here(POWERSHELL_IMAGE), reason=f"needs Docker with {POWERSHELL_IMAGE}"
)
def test_the_windows_scripts_pass_psscriptanalyzer():
    """Fetches PSScriptAnalyzer from the PowerShell Gallery: run where the image was pulled."""
    found = subprocess.run(  # noqa: S603 (fixed argv)
        [shutil.which("docker"), "run", "--rm", "-v", f"{ROOT}:/src:ro", POWERSHELL_IMAGE,
         "pwsh", "-NoProfile", "-File", "/src/tests/fixtures/analyze_windows.ps1"],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert found.returncode == 0, found.stdout + found.stderr
