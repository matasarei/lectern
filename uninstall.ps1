# Remove Lectern from this Windows PC; -Purge removes its models, projects and settings too
# (it asks first):
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-Purge]
# The work is in packaging\windows\uninstall.ps1.
& (Join-Path $PSScriptRoot 'packaging\windows\uninstall.ps1') @args
exit $LASTEXITCODE
