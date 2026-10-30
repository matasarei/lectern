# Install or update Lectern for you on this Windows PC, in %USERPROFILE%\.lectern. Run it as
# yourself, not as administrator:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# The work is in packaging\windows\install.ps1 (README "Install on Windows").
# An argument it refuses (-Wheel with no value) is an error here, not an exit code: without
# the catch this exited 0.
$ErrorActionPreference = 'Stop'
try {
    & (Join-Path $PSScriptRoot 'packaging\windows\install.ps1') @args
} catch {
    [Console]::Error.WriteLine("install.ps1: $($_.Exception.Message)")
    exit 1
}
exit $LASTEXITCODE
