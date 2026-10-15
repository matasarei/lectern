# The uv release a Windows install uses: pinned, with the sha256 from astral-sh/uv's release
# data (gh api repos/astral-sh/uv/releases/tags/0.12.23, 2026-10-15; the downloaded file and
# the release's .sha256 hash to it). The same version as packaging/pins.sh, which
# tests/test_packaging.py checks. Dot-sourced by packaging/windows/install.ps1.
# Read by the script that dot-sources this file, which the analyser can't see.
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', '')]
param()
$UvVersion = '0.12.23'
$UvWindowsX64Url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvWindowsX64Sha256 = '75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90'
