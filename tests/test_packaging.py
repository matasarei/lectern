"""Packaging for a host install (M4): the wheel, and packaging/linux/install.sh."""

from __future__ import annotations

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


# --- install.sh / uninstall.sh against a fake root and fake system commands ---------------

LINUX = ROOT / "packaging" / "linux"
FAKES = {
    # Every call is logged as "<name> <args>"; a few answer like the real thing would.
    "systemctl": 'echo "systemctl $*" >> "$LOG"; [ "$1" != is-active ] || [ -f "$STATE/active" ]',
    "useradd": 'echo "useradd $*" >> "$LOG"; touch "$STATE/user"',
    "userdel": 'echo "userdel $*" >> "$LOG"; rm -f "$STATE/user"',
    "usermod": 'echo "usermod $*" >> "$LOG"',
    "chown": 'echo "chown $*" >> "$LOG"',
    "getent": 'case "$1 $2" in "passwd lectern") [ -f "$STATE/user" ];; "group video") true;; '
    "*) false;; esac",
    # uv: `venv` makes the folder with a python and a lectern; `pip install` can be told to
    # fail (FAIL_PIP).
    "uv": 'echo "uv $*" >> "$LOG"; '
    'if [ "$1" = venv ]; then d="${@: -1}"; mkdir -p "$d/bin"; touch "$d/bin/python"; '
    'printf "#!/bin/sh\\n" > "$d/bin/lectern"; '
    'elif [ "$1" = pip ] && [ -n "${FAIL_PIP:-}" ]; then echo "no network" >&2; exit 1; '
    'elif [ "$1" = --version ]; then echo "uv 0.12.23"; fi',
}


def _install(tmp_path: Path, wheel: Path, *, fail_pip: bool = False, script: str = "install.sh",
             args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:  # fmt: skip
    bin_dir, state = tmp_path / "fakebin", tmp_path / "state"
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LECTERN_INSTALL_ROOT": str(tmp_path / "root"),
        "LECTERN_UV_BIN": str(bin_dir / "uv"),
        "LOG": str(tmp_path / "log"),
        "STATE": str(state),
        **({"FAIL_PIP": "1"} if fail_pip else {}),
    }
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


def test_a_first_install_makes_the_user_the_app_the_settings_and_the_service(tmp_path):
    done = _install(tmp_path, _wheel(tmp_path, "0.1.0"))
    assert done.returncode == 0, done.stderr
    root = tmp_path / "root"
    venv = (root / "opt/lectern/venv").resolve()
    assert venv.parent == (root / "opt/lectern/versions").resolve()
    assert venv.name.startswith("0.1.0-") and (venv / "bin/lectern").exists()
    env = (root / "etc/lectern/lectern.env").read_text()
    assert "LECTERN_HOME=/var/lib/lectern" in env and "LECTERN_PUBLIC_URL=" in env
    unit = (root / "etc/systemd/system/lectern.service").read_text()
    assert "User=lectern" in unit and "KillSignal=SIGINT" in unit
    assert (root / "opt/lectern/bin/lectern-url").stat().st_mode & 0o111
    log = _log(tmp_path)
    assert any(c.startswith("useradd --system") for c in log)
    assert "usermod -aG video lectern" in log  # its GPU; render doesn't exist here
    assert any(c.startswith("uv venv --quiet --python 3.12") for c in log)
    assert "systemctl enable --now lectern" in log
    assert "Lectern 0.1.0 is installed and running." in done.stdout


def test_an_upgrade_keeps_the_settings_and_restarts_and_drops_the_old_version(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    env_file = tmp_path / "root/etc/lectern/lectern.env"
    public = "LECTERN_PUBLIC_URL=http://lectern.lan"
    env_file.write_text(env_file.read_text().replace("LECTERN_PUBLIC_URL=", public))
    (tmp_path / "state/active").touch()

    done = _install(tmp_path, _wheel(tmp_path, "0.2.0"))
    assert done.returncode == 0, done.stderr
    assert "LECTERN_PUBLIC_URL=http://lectern.lan" in env_file.read_text()  # kept
    versions = [p.name for p in (tmp_path / "root/opt/lectern/versions").iterdir()]
    assert len(versions) == 1 and versions[0].startswith("0.2.0-")
    assert "systemctl restart lectern" in _log(tmp_path)
    assert _log(tmp_path).count("useradd --system --home-dir /var/lib/lectern --no-create-home "
                                "--shell /usr/sbin/nologin lectern") == 1  # fmt: skip


def test_a_failed_upgrade_leaves_the_version_in_use_as_it_was(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    before = (tmp_path / "root/opt/lectern/venv").resolve()

    failed = _install(tmp_path, _wheel(tmp_path, "0.2.0"), fail_pip=True)
    assert failed.returncode == 1 and "the version in use is unchanged" in failed.stderr
    assert (tmp_path / "root/opt/lectern/venv").resolve() == before and before.exists()
    versions = list((tmp_path / "root/opt/lectern/versions").iterdir())
    assert versions == [before]  # nothing half-made left behind


def test_a_wrong_uv_checksum_is_refused(tmp_path):
    fake = tmp_path / "uv.tar.gz"
    fake.write_bytes(b"not uv")
    bin_dir = tmp_path / "fakebin"
    _install(tmp_path, _wheel(tmp_path, "0.1.0"))  # makes the fakes
    env_vars = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                "LECTERN_INSTALL_ROOT": str(tmp_path / "root2"), "LOG": str(tmp_path / "log"),
                "STATE": str(tmp_path / "state"), "LECTERN_UV_URL": fake.as_uri()}  # fmt: skip
    done = subprocess.run(  # noqa: S603
        ["bash", str(LINUX / "install.sh"), str(_wheel(tmp_path, "0.1.0"))],  # noqa: S607
        env=env_vars, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and "uv's checksum is wrong" in done.stderr
    assert not (tmp_path / "root2/opt/lectern/bin/uv").exists()


def test_a_wheel_that_isnt_lecterns_is_refused(tmp_path):
    other = tmp_path / "something-1.0-py3-none-any.whl"
    other.write_bytes(b"PK")
    done = _install(tmp_path, other)
    assert done.returncode == 1 and "isn't a Lectern wheel" in done.stderr


def test_uninstall_keeps_the_data_unless_purged(tmp_path):
    assert _install(tmp_path, _wheel(tmp_path, "0.1.0")).returncode == 0
    (tmp_path / "root/var/lib/lectern/models").mkdir(parents=True)

    kept = _install(tmp_path, Path("-"), script="uninstall.sh")
    assert kept.returncode == 0, kept.stderr
    assert not (tmp_path / "root/opt/lectern").exists()
    assert (tmp_path / "root/var/lib/lectern/models").exists()
    assert (tmp_path / "root/etc/lectern/lectern.env").exists()
    assert "systemctl disable --now lectern" in _log(tmp_path)

    purged = _install(tmp_path, Path("-"), script="uninstall.sh", args=("--purge",))
    assert purged.returncode == 0, purged.stderr
    assert not (tmp_path / "root/var/lib/lectern").exists()
    assert "userdel lectern" in _log(tmp_path)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck isn't installed")
def test_the_scripts_pass_shellcheck():
    scripts = [
        LINUX / "install.sh",
        LINUX / "uninstall.sh",
        ROOT / "packaging/build_wheel.sh",
        ROOT / "packaging/make_package.sh",
        ROOT / "install.sh",
        ROOT / "uninstall.sh",
    ]
    checked = subprocess.run(["shellcheck", "-x", *map(str, scripts)],  # noqa: S603, S607
                             capture_output=True, text=True, cwd=ROOT)  # fmt: skip
    assert checked.returncode == 0, checked.stdout


def _copy_scripts(to: Path) -> None:
    """The install scripts as a package or a working copy has them."""
    (to / "packaging").mkdir(parents=True)
    shutil.copytree(LINUX, to / "packaging/linux")
    shutil.copy(ROOT / "packaging/pins.sh", to / "packaging/pins.sh")
    shutil.copy(ROOT / "install.sh", to / "install.sh")


def _run_top(tmp_path: Path, top: Path, **extra: str) -> subprocess.CompletedProcess:
    _install(tmp_path, _wheel(tmp_path, "0.0.1"))  # makes the fakes and the log
    (tmp_path / "log").write_text("")
    env = {
        "PATH": f"{tmp_path / 'fakebin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LECTERN_INSTALL_ROOT": str(tmp_path / "pkgroot"),
        "LECTERN_UV_BIN": str(tmp_path / "fakebin/uv"),
        "LOG": str(tmp_path / "log"),
        "STATE": str(tmp_path / "state"),
        "HOME": str(tmp_path),
        **extra,
    }
    return subprocess.run(["bash", str(top / "install.sh")], env=env,  # noqa: S603, S607
                          capture_output=True, text=True, timeout=60)  # fmt: skip


def test_a_package_installs_the_wheel_beside_it_with_no_argument(tmp_path):
    package = tmp_path / "lectern-0.3.0"
    _copy_scripts(package)
    (package / "lectern_ai-0.3.0-py3-none-any.whl").write_bytes(b"PK")

    done = _run_top(tmp_path, package)
    assert done.returncode == 0, done.stderr
    venv = (tmp_path / "pkgroot/opt/lectern/venv").resolve()
    assert venv.name.startswith("0.3.0-")
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
    assert not (tmp_path / "pkgroot/opt/lectern").exists()


def test_a_folder_with_neither_a_wheel_nor_the_source_says_so(tmp_path):
    empty = tmp_path / "somewhere"
    _copy_scripts(empty)
    done = _run_top(tmp_path, empty)
    assert done.returncode == 1 and "no Lectern wheel next to this script" in done.stderr
