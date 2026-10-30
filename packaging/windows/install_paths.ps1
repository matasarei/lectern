# An install's shortcuts and log, for install.ps1 and uninstall.ps1 (dot-sourced). The default
# install (%USERPROFILE%\.lectern) has the plain names: Lectern in the Start menu and the
# Startup folder, %LOCALAPPDATA%\Lectern\Logs\lectern.log. Another LECTERN_DIR has its own,
# so that it can't take over the default's: "Lectern (<folder>)" in the Start menu, no Startup
# shortcut, and its log in Logs\<key> (the tray app names it the same way, Lectern.cs).
# (ASCII: see install.ps1.)

# The install folder as the tray app's events, mutex and log folder name it.
function Get-InstallKey([string] $Dir) {
    return $Dir.ToLowerInvariant().Replace('\', '_').Replace(':', '_')
}

function Get-InstallLocation([string] $Dir) {
    $default = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.lectern')).TrimEnd('\')
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $logs = Join-Path $env:LOCALAPPDATA 'Lectern\Logs'
    if ($Dir -ieq $default) {
        return @{
            Default = $true
            StartMenu = Join-Path $programs 'Lectern.lnk'
            Startup = Join-Path $programs 'Startup\Lectern.lnk'
            LogFolder = $logs
            Log = Join-Path $logs 'lectern.log'
        }
    }
    $folder = Join-Path $logs (Get-InstallKey $Dir)
    return @{
        Default = $false
        StartMenu = Join-Path $programs "Lectern ($(Split-Path -Leaf $Dir)).lnk"
        Startup = $null
        LogFolder = $folder
        Log = Join-Path $folder 'lectern.log'
    }
}

# What a shortcut starts, or $null when there's no shortcut there.
function Get-LinkTarget([string] $Link) {
    if (-not (Test-Path -LiteralPath $Link)) { return $null }
    return (New-Object -ComObject WScript.Shell).CreateShortcut($Link).TargetPath
}

# Writes the install's shortcuts. The default install's are its own whatever they point at
# now (an older second install took them over); another's only when they're missing or
# already its own: two folders of one name don't share a shortcut. Returns the ones written.
function Add-InstallShortcut([hashtable] $Paths, [string] $TrayExe, [string] $Dir) {
    $written = @()
    $shell = New-Object -ComObject WScript.Shell
    foreach ($link in @(@($Paths.StartMenu, ''), @($Paths.Startup, '--background'))) {
        if (-not $link[0]) { continue }
        $now = Get-LinkTarget $link[0]
        if (-not $Paths.Default -and $now -and ($now -ine $TrayExe)) {
            [Console]::Error.WriteLine("$($link[0]) is another Lectern's ($now); it's left as it is.")
            continue
        }
        $shortcut = $shell.CreateShortcut($link[0])
        $shortcut.TargetPath = $TrayExe
        $shortcut.Arguments = $link[1]
        $shortcut.WorkingDirectory = $Dir
        $shortcut.Description = 'Lectern: a local AI workbench'
        $shortcut.Save()
        $written += $link[0]
    }
    return $written
}

# Removes the shortcuts that start this install's tray app, under its own names and the plain
# ones (which an older second install took over), and leaves every other.
function Clear-InstallShortcut([hashtable] $Paths, [string] $TrayExe) {
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $links = @($Paths.StartMenu, $Paths.Startup, (Join-Path $programs 'Lectern.lnk'),
        (Join-Path $programs 'Startup\Lectern.lnk')) | Where-Object { $_ } | Select-Object -Unique
    foreach ($link in $links) {
        if ((Get-LinkTarget $link) -ieq $TrayExe) {
            Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        }
    }
}
