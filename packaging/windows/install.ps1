# Install or update Lectern for you on this Windows PC (x64): Lectern in %USERPROFILE%\.lectern,
# and its tray app, which starts and stops it, in the Start menu and the Startup folder (another
# LECTERN_DIR: the Start menu only, under its folder's name, install_paths.ps1). No
# administrator rights:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1        (from a copy of the repository)
#
# It builds the web UI and the wheel from this copy (that needs Node.js), and the tray app
# with the C# compiler that ships with Windows. A wheel can also be given:
#   install.ps1 -Wheel lectern_ai-<version>-py3-none-any.whl
#
#   %USERPROFILE%\.lectern\src          where the README says to clone (any copy works)
#   %USERPROFILE%\.lectern\app          uv (pinned, checksum checked), its Python 3.12, the
#                                       installed versions, the tray app
#   %USERPROFILE%\.lectern\data         Lectern's data (LECTERN_HOME): database, models, projects
#   %USERPROFILE%\.lectern\lectern.env  settings, written only when missing
#   %USERPROFILE%\.lectern\bin          lectern-url.cmd (the access link), lectern.cmd
#
# As on Linux and the Mac, each install goes into app\versions\<version>-..., and app\venv (a
# directory junction) points at the one in use; the tray app is built beside the old one.
# Nothing that runs is touched until both are ready: a failure before that leaves the running
# Lectern and its version as they were. An update is the same command after `git pull`: a
# running tray app is asked to quit (it stops Lectern first), and is started again.
#
# LECTERN_DIR moves %USERPROFILE%\.lectern; LECTERN_UV_BIN names a uv to use instead of
# fetching one; LECTERN_HEALTH_WAIT and LECTERN_QUIT_WAIT are seconds (60 each).
# (This file is ASCII: Windows PowerShell 5.1 reads a script without a byte-order mark as the
# system's code page.)
# CmdletBinding: an argument it doesn't know (-Wheell) is refused, not silently ignored.
[CmdletBinding()]
param(
    [string] $Wheel = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

. (Join-Path $PSScriptRoot 'pins.ps1')
. (Join-Path $PSScriptRoot 'install_paths.ps1')

function Exit-Install([string] $Message) {
    [Console]::Error.WriteLine("install.ps1: $Message")
    exit 1
}

# Folders go with cmd's rmdir: it removes a junction or a link as itself, where Windows
# PowerShell 5.1's Remove-Item -Recurse follows one and deletes what it points at.
function Invoke-Rmdir([string[]] $Paths) {
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) { cmd /c rmdir /s /q "$path" }
    }
}

function Get-Setting([string] $Name, [string] $Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

# --- where, and as whom ------------------------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Exit-Install 'run it as yourself, not as administrator: Lectern installs into your user folder.'
}
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    Exit-Install "this installer is for 64-bit Intel and AMD PCs; this is $env:PROCESSOR_ARCHITECTURE."
}

$dir = [IO.Path]::GetFullPath((Get-Setting 'LECTERN_DIR' (Join-Path $env:USERPROFILE '.lectern'))).TrimEnd('\')
$app = Join-Path $dir 'app'
$data = Join-Path $dir 'data'
$bin = Join-Path $dir 'bin'
$settings = Join-Path $dir 'lectern.env'
$tray = Join-Path $app 'tray'
$trayExe = Join-Path $tray 'Lectern.exe'
$paths = Get-InstallLocation $dir
# The tray app's events, named as it names them (packaging\windows\tray\Lectern.cs).
$events = 'Local\Lectern-' + (Get-InstallKey $dir)

foreach ($folder in @($app, (Join-Path $app 'versions'), (Join-Path $app 'bin'), $data, $bin)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}

# --- uv -----------------------------------------------------------------------------------
function Get-Uv {
    $given = Get-Setting 'LECTERN_UV_BIN' ''
    if ($given) { return $given }
    $uv = Join-Path $app 'bin\uv.exe'
    if (Test-Path -LiteralPath $uv) {
        $have = (& $uv --version 2>$null) -split ' '
        if ($have.Count -ge 2 -and $have[1] -eq $UvVersion) { return $uv }
    }
    Write-Host "Getting uv $UvVersion..."
    $scratch = Join-Path ([IO.Path]::GetTempPath()) ("lectern-uv-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    try {
        $zip = Join-Path $scratch 'uv.zip'
        $ProgressPreference = 'SilentlyContinue'  # the progress bar slows the download a lot
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $UvWindowsX64Url -OutFile $zip
        } catch {
            Exit-Install "couldn't download uv (is the network up?): $($_.Exception.Message)"
        }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($got -ne $UvWindowsX64Sha256) {
            Exit-Install "uv's checksum is wrong ($got); not installing it."
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $scratch -Force
        Copy-Item -LiteralPath (Join-Path $scratch 'uv.exe') -Destination $uv -Force
    } finally {
        Invoke-Rmdir $scratch
    }
    return $uv
}
$uv = Get-Uv
$env:UV_PYTHON_INSTALL_DIR = Join-Path $app 'python'
$env:UV_CACHE_DIR = Join-Path $app 'cache'
$env:UV_NO_CONFIG = '1'

# --- the wheel ----------------------------------------------------------------------------
function Test-WebUi([string] $Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -eq 'lectern/web_dist/index.html') { return $true }
        }
        return $false
    } finally {
        $zip.Dispose()
    }
}

function Find-Wheel([string] $Given) {
    $top = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    if ($Given) {
        if (-not (Test-Path -LiteralPath $Given)) { Exit-Install "no such file: $Given" }
        return (Resolve-Path -LiteralPath $Given).Path
    }
    if ((Test-Path (Join-Path $top 'pyproject.toml')) -and (Test-Path (Join-Path $top 'web'))) {
        # A working copy: build its wheel, the web UI first (it writes node_modules and dist here).
        # Their output goes to the console: left in the pipeline, it would be returned with the path.
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Exit-Install 'building the web UI needs Node.js (npm): winget install OpenJS.NodeJS.LTS, then open a new window.'
        }
        Write-Host "Building Lectern from $top..."
        $web = Join-Path $top 'web'
        & npm --prefix $web ci | Out-Host
        if ($LASTEXITCODE -ne 0) { Exit-Install 'installing the web UI''s packages failed (see above).' }
        & npm --prefix $web run build | Out-Host
        if ($LASTEXITCODE -ne 0) { Exit-Install 'building the web UI failed (see above).' }
        $dist = Join-Path $top 'dist'
        Remove-Item -Path (Join-Path $dist 'lectern_ai-*.whl') -ErrorAction SilentlyContinue
        & $uv build --quiet --wheel --out-dir $dist $top | Out-Host
        if ($LASTEXITCODE -ne 0) { Exit-Install 'building the app failed (see above).' }
        $built = Get-ChildItem -Path (Join-Path $dist 'lectern_ai-*.whl') | Select-Object -First 1
        if (-not $built) { Exit-Install 'the build made no wheel.' }
        if (-not (Test-WebUi $built.FullName)) {
            Exit-Install 'the wheel has no web UI (lectern/web_dist/index.html); the build went wrong.'
        }
        return $built.FullName
    }
    $packaged = Get-ChildItem -Path (Join-Path $top 'lectern_ai-*.whl'), (Join-Path $PSScriptRoot 'lectern_ai-*.whl') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $packaged) { Exit-Install "no Lectern wheel next to this script, and this isn't a working copy of the repository." }
    return $packaged.FullName
}
$wheelPath = Find-Wheel $Wheel
$wheelName = Split-Path -Leaf $wheelPath
if ($wheelName -notlike 'lectern_ai-*.whl') {
    Exit-Install "$wheelPath isn't a Lectern wheel (lectern_ai-<version>-py3-none-any.whl)"
}
$version = $wheelName.Split('-')[1]

# --- the new version and the new tray app, before anything that runs is touched -----------
$target = Join-Path $app ("versions\$version-" + (Get-Date -Format 'yyyyMMddHHmmss') + "-$PID")
Write-Host "Installing Lectern $version..."
& $uv venv --quiet --python 3.12 $target
if ($LASTEXITCODE -eq 0) {
    & $uv pip install --quiet --refresh-package lectern-ai --python (Join-Path $target 'Scripts\python.exe') $wheelPath
}
if ($LASTEXITCODE -ne 0) {
    Invoke-Rmdir $target
    Exit-Install "installing Lectern $version failed (see above); the version in use is unchanged."
}
$trayNew = Join-Path $app 'tray.new'
Invoke-Rmdir $trayNew
try {
    & (Join-Path $PSScriptRoot 'build_tray.ps1') -Out (Join-Path $trayNew 'Lectern.exe') -Version $version
} catch {
    Invoke-Rmdir $trayNew, $target
    Exit-Install "building the tray app failed: $($_.Exception.Message) The version in use is unchanged."
}

# --- the switch ---------------------------------------------------------------------------
# A running tray app is asked to quit, which stops Lectern cleanly. It's started again below.
# One that isn't running isn't started by an update.
function Get-Tray {
    Get-Process -Name 'Lectern' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -ieq $trayExe) }
}
$start = -not (Test-Path -LiteralPath $trayExe)  # a first install
$wasOpen = $false
if (Get-Tray) {
    $start = $true
    $wasOpen = $true
    Write-Host 'Shutting Lectern down...'
    $quit = $null
    if ([Threading.EventWaitHandle]::TryOpenExisting("$events-quit", [ref] $quit)) {
        [void] $quit.Set()
        $quit.Dispose()
    }
    $wait = [int] (Get-Setting 'LECTERN_QUIT_WAIT' '60')
    for ($i = 0; $i -lt $wait -and (Get-Tray); $i++) { Start-Sleep -Seconds 1 }
    if (Get-Tray) {
        Invoke-Rmdir $trayNew, $target
        Exit-Install "Lectern didn't quit. Quit it from its tray icon and run this again; the version in use is unchanged."
    }
}

$venv = Join-Path $app 'venv'
$previous = ''
try {
    if (Test-Path -LiteralPath $venv) {
        $previous = (Get-Item -LiteralPath $venv).Target | Select-Object -First 1
        # rmdir on a junction removes the link, never what it points at
        cmd /c rmdir "$venv"
        if ($LASTEXITCODE -ne 0) { throw "couldn't remove the old link $venv" }
    }
    New-Item -ItemType Junction -Path $venv -Target $target | Out-Null
    $trayOld = Join-Path $app 'tray.old'
    Invoke-Rmdir $trayOld
    if (Test-Path -LiteralPath $tray) { Rename-Item -LiteralPath $tray -NewName 'tray.old' }
    Rename-Item -LiteralPath $trayNew -NewName 'tray'
    Invoke-Rmdir $trayOld

    # The way to the access link, and Lectern's own command, each with the settings loaded,
    # and in Python's UTF-8 mode as the tray app runs it: else this PC's code page is the
    # default for text files (cp1251 on a Ukrainian or Russian Windows).
    $load = @(
        '@echo off',
        'setlocal',
        "for /f `"usebackq eol=# tokens=1,* delims==`" %%a in (`"$settings`") do set `"%%a=%%b`"",
        'if not defined LECTERN_PORT set LECTERN_PORT=8765',
        'set PYTHONUTF8=1'
    )
    $lectern = Join-Path $venv 'Scripts\lectern.exe'
    Set-Content -LiteralPath (Join-Path $bin 'lectern-url.cmd') -Encoding ASCII -Value (
        $load + "`"$lectern`" url --port %LECTERN_PORT%")
    Set-Content -LiteralPath (Join-Path $bin 'lectern.cmd') -Encoding ASCII -Value (
        $load + "`"$lectern`" %*")

    # The settings, written only when missing, so your edits survive updates.
    if (-not (Test-Path -LiteralPath $settings)) {
        Set-Content -LiteralPath $settings -Encoding ASCII -Value @(
            "# Lectern's settings (read by its tray app). Restart after a change:",
            "#   Restart, in Lectern's menu (its icon by the clock)",
            "LECTERN_HOME=$data",
            'LECTERN_PORT=8765',
            '# The address(es) people use to reach Lectern, through whatever you put in front of it,',
            '# e.g. https://lectern.example.com (comma-separate several).',
            'LECTERN_PUBLIC_URL='
        )
    }

    # The Start menu, and the Startup folder: Lectern runs while its tray app does.
    $shortcuts = Add-InstallShortcut $paths $trayExe $dir
} catch {
    # The tray app was quit for the switch: it's started again as it is now (the new one, or
    # the old one put back), on whichever version app\venv points at.
    # The old link went but the new one wasn't made: the version in use is linked again.
    if ($previous -and -not (Test-Path -LiteralPath $venv)) {
        try {
            New-Item -ItemType Junction -Path $venv -Target $previous | Out-Null
        } catch {
            [Console]::Error.WriteLine("install.ps1: couldn't link $venv to $previous again: $($_.Exception.Message)")
        }
    }
    if ($wasOpen) {
        if (-not (Test-Path -LiteralPath $trayExe) -and (Test-Path -LiteralPath (Join-Path $app 'tray.old'))) {
            Rename-Item -LiteralPath (Join-Path $app 'tray.old') -NewName 'tray'
        }
        if (Test-Path -LiteralPath $trayExe) {
            Start-Process -FilePath $trayExe -ArgumentList '--background'
            [Console]::Error.WriteLine('install.ps1: Lectern was started again, as it was before this step.')
        }
    }
    Exit-Install "the switch failed: $($_.Exception.Message)"
}

# --- start it -----------------------------------------------------------------------------
if (-not $start) {
    Write-Host "Upgraded to Lectern $version. Lectern wasn't running, so it isn't now: open it from the Start menu."
    exit 0
}
Start-Process -FilePath $trayExe -ArgumentList '--background'

# Only a version that answers counts as installed. Until then nothing older is deleted.
$port = 8765
foreach ($line in (Get-Content -LiteralPath $settings)) {
    if ($line -match '^LECTERN_PORT=(\d+)\s*$') { $port = [int] $Matches[1] }
}
Write-Host "Waiting for Lectern to answer on 127.0.0.1:$port..."
$answered = $false
$wait = [int] (Get-Setting 'LECTERN_HEALTH_WAIT' '60')
for ($i = 0; $i -lt $wait -and -not $answered; $i++) {
    try {
        $answered = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$port/health").StatusCode -eq 200
    } catch {
        Start-Sleep -Seconds 1
    }
}
$logPath = $paths.Log
if (-not $answered) {
    [Console]::Error.WriteLine("install.ps1: Lectern $version didn't start; see: $logPath")
    if ($previous -and (Test-Path -LiteralPath $previous)) {
        [Console]::Error.WriteLine("The previous version is kept in $previous. To go back to it, quit Lectern, then:")
        [Console]::Error.WriteLine("  cmd /c rmdir `"$venv`" & mklink /J `"$venv`" `"$previous`"")
        [Console]::Error.WriteLine("(only if the new version hasn't migrated the database yet: the log says)")
    }
    exit 1
}
if ($previous) {
    Write-Host "Upgraded to Lectern $version and started it again."
} else {
    Write-Host "Lectern $version is installed and running."
}

# The new version works: it and the one before it stay.
Get-ChildItem -LiteralPath (Join-Path $app 'versions') -Directory |
    Where-Object { $_.FullName -ne $target -and $_.FullName -ne $previous } |
    ForEach-Object { Invoke-Rmdir $_.FullName }

$where = 'by the clock (its tray icon)'
if ($shortcuts -contains $paths.StartMenu) {
    $where = "in the Start menu ($([IO.Path]::GetFileNameWithoutExtension($paths.StartMenu))) and $where"
}
if ($paths.Startup) {
    $signIn = 'Lectern starts when you sign in (the Startup folder); remove its shortcut there to stop that.'
} else {
    $signIn = "This Lectern, in $dir, doesn't start when you sign in: only the one in %USERPROFILE%\.lectern does."
}
Write-Host @"

Lectern is $where`: Open Lectern, in its menu.
It runs on this PC at http://127.0.0.1:$port (LECTERN_PORT in $settings).
The link with its access token:  $bin\lectern-url.cmd
If others will use it, turn accounts on (once):  $bin\lectern.cmd user init <name>
Update: git pull, then install.ps1 again.   Log: $logPath
$signIn
"@

# Done: the root install.ps1 exits with this script's status, not a native command's.
exit 0
