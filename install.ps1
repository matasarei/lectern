# Install or update Lectern for you on this Windows PC, in %USERPROFILE%\.lectern. Run it as
# yourself, not as administrator:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# The work is in packaging\windows\install.ps1 (README "Install on Windows").
& (Join-Path $PSScriptRoot 'packaging\windows\install.ps1') @args
exit $LASTEXITCODE
