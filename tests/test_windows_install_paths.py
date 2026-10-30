"""A second Windows install (another LECTERN_DIR) and the default one's shortcuts and log.

Seen on the M4 laptop: a second install pointed the Start-menu and Startup shortcuts at
itself, its uninstall removed them and tried to delete the log the first was writing, and
both wrote one log (docs/acceptance/m4.md). These run the real scripts against a profile in
tmp_path: its APPDATA, LOCALAPPDATA and USERPROFILE.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts and scripts")

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "packaging/windows"


def _profile(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name, folder in (
        ("USERPROFILE", "home"),
        ("APPDATA", "roaming"),
        ("LOCALAPPDATA", "local"),
    ):
        (tmp_path / folder).mkdir(exist_ok=True)
        env[name] = str(tmp_path / folder)
    (tmp_path / "roaming/Microsoft/Windows/Start Menu/Programs/Startup").mkdir(parents=True)
    return env


def _ps(env: dict[str, str], script: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",  # noqa: S607
         f"$ErrorActionPreference = 'Stop'; . '{WINDOWS / 'install_paths.ps1'}'; {script}"],
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _install(env: dict[str, str], folder: Path) -> None:
    """The shortcuts install.ps1 writes for an install in `folder`."""
    tray = folder / "app/tray/Lectern.exe"
    _ps(env, f"[void](Add-InstallShortcut (Get-InstallLocation '{folder}') '{tray}' '{folder}')")


def _links(env: dict[str, str]) -> dict[str, str]:
    """Every shortcut in the Start menu and the Startup folder: its path below Programs,
    and what it starts."""
    shown = _ps(
        env,
        "$p = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs'; "
        "Get-ChildItem -LiteralPath $p -Recurse -Filter *.lnk | ForEach-Object { "
        "$_.FullName.Substring($p.Length + 1) + '|' + (Get-LinkTarget $_.FullName) }",
    )
    return dict(line.split("|") for line in shown.splitlines() if line)


def _uninstall(env: dict[str, str], folder: Path) -> None:
    (folder / "app/versions").mkdir(parents=True, exist_ok=True)  # an install, to uninstall.ps1
    done = subprocess.run(  # noqa: S603
        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",  # noqa: S607
         str(WINDOWS / "uninstall.ps1")],
        env={**env, "LECTERN_DIR": str(folder)}, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


def test_a_second_install_gets_its_own_start_menu_entry_and_leaves_the_defaults(tmp_path):
    env = _profile(tmp_path)
    default, other = tmp_path / "home/.lectern", tmp_path / "elsewhere/lectern-m4"
    _install(env, default)
    _install(env, other)

    assert _links(env) == {
        "Lectern.lnk": str(default / "app/tray/Lectern.exe"),
        "Startup\\Lectern.lnk": str(default / "app/tray/Lectern.exe"),
        "Lectern (lectern-m4).lnk": str(other / "app/tray/Lectern.exe"),  # no Startup one
    }


def test_the_default_install_takes_its_shortcuts_back(tmp_path):
    """From a second install made before this fix, which had taken them over."""
    env = _profile(tmp_path)
    default, other = tmp_path / "home/.lectern", tmp_path / "elsewhere/lectern-m4"
    taken = tmp_path / "roaming/Microsoft/Windows/Start Menu/Programs/Lectern.lnk"
    _ps(env, f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{taken}'); "
             f"$s.TargetPath = '{other / 'app/tray/Lectern.exe'}'; $s.Save()")  # fmt: skip
    _install(env, default)
    assert _links(env)["Lectern.lnk"] == str(default / "app/tray/Lectern.exe")


def test_a_second_install_leaves_a_shortcut_of_its_name_that_isnt_its_own(tmp_path):
    env = _profile(tmp_path)
    one, two = tmp_path / "a/lectern-m4", tmp_path / "b/lectern-m4"
    _install(env, one)
    _install(env, two)
    assert _links(env) == {"Lectern (lectern-m4).lnk": str(one / "app/tray/Lectern.exe")}


def test_uninstalling_the_second_install_removes_only_its_own(tmp_path):
    env = _profile(tmp_path)
    default, other = tmp_path / "home/.lectern", tmp_path / "elsewhere/lectern-m4"
    _install(env, default)
    _install(env, other)
    logs = tmp_path / "local/Lectern/Logs"
    own_log = Path(_ps(env, f"(Get-InstallLocation '{other}').Log"))
    for log in (logs / "lectern.log", own_log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("a line\n", encoding="utf-8")

    _uninstall(env, other)

    assert set(_links(env)) == {"Lectern.lnk", "Startup\\Lectern.lnk"}
    assert (logs / "lectern.log").exists() and not own_log.parent.exists()
    assert not (other / "app").exists()


def test_uninstalling_the_default_keeps_the_other_installs_log(tmp_path):
    env = _profile(tmp_path)
    default, other = tmp_path / "home/.lectern", tmp_path / "elsewhere/lectern-m4"
    _install(env, default)
    _install(env, other)
    logs = tmp_path / "local/Lectern/Logs"
    other_log = Path(_ps(env, f"(Get-InstallLocation '{other}').Log"))
    for log in (logs / "lectern.log", logs / "lectern.log.1", other_log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("a line\n", encoding="utf-8")

    _uninstall(env, default)

    assert set(_links(env)) == {"Lectern (lectern-m4).lnk"}
    assert sorted(p.name for p in logs.rglob("*") if p.is_file()) == ["lectern.log"]
    assert other_log.exists()


def test_the_log_of_another_install_is_in_its_own_folder(tmp_path):
    env = _profile(tmp_path)
    default, other = tmp_path / "home/.lectern", tmp_path / "elsewhere/lectern-m4"
    logs = tmp_path / "local/Lectern/Logs"
    assert _ps(env, f"(Get-InstallLocation '{default}').Log") == str(logs / "lectern.log")
    key = str(other).lower().replace("\\", "_").replace(":", "_")
    assert _ps(env, f"(Get-InstallLocation '{other}').Log") == str(logs / key / "lectern.log")


def test_the_tray_app_names_an_installs_log_folder_as_the_scripts_do():
    tray = (ROOT / "packaging/windows/tray/Lectern.cs").read_text(encoding="utf-8")
    scripts = (WINDOWS / "install_paths.ps1").read_text(encoding="utf-8")
    # The scripts' folder has no trailing \ (install.ps1 trims it): the tray's is trimmed too.
    assert "dir.TrimEnd('\\\\').ToLowerInvariant().Replace('\\\\', '_').Replace(':', '_')" in tray
    assert "$Dir.ToLowerInvariant().Replace('\\', '_').Replace(':', '_')" in scripts
    assert 'Path.Combine(logs, Key(dir), "lectern.log")' in tray
