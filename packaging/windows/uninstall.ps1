# Remove your Lectern from this PC: its tray app and shortcuts, %USERPROFILE%\.lectern\app and
# \bin, and its log. The data (models, projects, the database) and the settings stay unless
# -Purge is given, which asks first. %USERPROFILE%\.lectern\src, the copy you may be running
# this from, always stays.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-Purge]
#
# LECTERN_DIR as for install.ps1; LECTERN_QUIT_WAIT is seconds (60). (ASCII: see install.ps1.)
param(
    [switch] $Purge
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

function Exit-Uninstall([string] $Message) {
    [Console]::Error.WriteLine("uninstall.ps1: $Message")
    exit 1
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Exit-Uninstall 'run it as yourself, not as administrator: Lectern is installed per user.'
}
$given = [Environment]::GetEnvironmentVariable('LECTERN_DIR')
if ([string]::IsNullOrEmpty($given)) { $given = Join-Path $env:USERPROFILE '.lectern' }
$dir = [IO.Path]::GetFullPath($given).TrimEnd('\')
$app = Join-Path $dir 'app'
$trayExe = Join-Path $app 'tray\Lectern.exe'
$events = 'Local\Lectern-' + $dir.ToLowerInvariant().Replace('\', '_').Replace(':', '_')

# Its folders are app, bin and data: under a LECTERN_DIR that isn't an install (C:\, say)
# those names are someone else's. An install has its settings, or its versions folder.
if (-not ((Test-Path -LiteralPath (Join-Path $dir 'lectern.env')) -or
          (Test-Path -LiteralPath (Join-Path $app 'versions')))) {
    Exit-Uninstall "there's no Lectern installed in $dir (no lectern.env, no app\versions); nothing was removed."
}

# A wipe deletes every model and project, and nothing brings them back: only y or yes goes on.
if ($Purge) {
    $answer = Read-Host "This deletes the models, projects and settings in $dir. Go on? [y/N]"
    if ($answer -notmatch '^(y|yes)$') {
        Write-Output 'Nothing removed.'
        exit 1
    }
}

# A running tray app is asked to quit first, which stops Lectern: their files mustn't go from
# under them.
function Get-Tray {
    Get-Process -Name 'Lectern' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -ieq $trayExe) }
}
if (Get-Tray) {
    Write-Output 'Shutting Lectern down...'
    $quit = $null
    if ([Threading.EventWaitHandle]::TryOpenExisting("$events-quit", [ref] $quit)) {
        [void] $quit.Set()
        $quit.Dispose()
    }
    $wait = [Environment]::GetEnvironmentVariable('LECTERN_QUIT_WAIT')
    if ([string]::IsNullOrEmpty($wait)) { $wait = '60' }
    for ($i = 0; $i -lt [int] $wait -and (Get-Tray); $i++) { Start-Sleep -Seconds 1 }
    if (Get-Tray) {
        Exit-Uninstall "Lectern didn't quit. Quit it from its tray icon and run this again; nothing was removed."
    }
}

foreach ($link in @(
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Lectern.lnk'),
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Lectern.lnk'))) {
    Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
}
# Folders go with cmd's rmdir: it removes a junction or a link as itself, where Windows
# PowerShell 5.1 would follow one and delete what it points at (a models folder linked in
# from elsewhere, say).
$folders = @((Join-Path $app 'venv'), $app, (Join-Path $dir 'bin'), (Join-Path $env:LOCALAPPDATA 'Lectern\Logs'))
if ($Purge) { $folders += Join-Path $dir 'data' }
foreach ($folder in $folders) {  # app\venv first: a junction to the version in use
    if (Test-Path -LiteralPath $folder) { cmd /c rmdir /s /q "$folder" }
}
if ($Purge) {
    Remove-Item -LiteralPath (Join-Path $dir 'lectern.env') -Force -ErrorAction SilentlyContinue
    Write-Output "Lectern is removed, with its models, projects and settings. Kept: $dir\src, if any."
} else {
    Write-Output "Lectern is removed. Kept: $dir\data (models, projects), $dir\lectern.env."
}

# Done: the root uninstall.ps1 exits with this script's status, not a native command's.
exit 0
