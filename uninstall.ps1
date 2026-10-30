# Remove Lectern from this Windows PC; -Purge removes its models, projects and settings too
# (it asks first):
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-Purge]
# The work is in packaging\windows\uninstall.ps1.
# An argument it refuses is an error here, not an exit code: without the catch this exited 0.
$ErrorActionPreference = 'Stop'
try {
    & (Join-Path $PSScriptRoot 'packaging\windows\uninstall.ps1') @args
} catch {
    [Console]::Error.WriteLine("uninstall.ps1: $($_.Exception.Message)")
    exit 1
}
exit $LASTEXITCODE
