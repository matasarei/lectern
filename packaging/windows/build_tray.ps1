# Build Lectern's tray app (tray\Lectern.cs) into -Out with the C# compiler that ships with
# Windows (.NET Framework 4): no SDK, no Visual Studio. -Version is what its menu shows.
#
#   powershell -ExecutionPolicy Bypass -File build_tray.ps1 -Out <folder>\Lectern.exe -Version 0.1.0
param(
    [Parameter(Mandatory = $true)] [string] $Out,
    [string] $Version = ''
)
$ErrorActionPreference = 'Stop'

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) {
    throw "building the tray app needs .NET Framework 4, which comes with Windows 10 and 11: $csc isn't there."
}
$tray = Join-Path $PSScriptRoot 'tray'
$folder = Split-Path -Parent $Out
New-Item -ItemType Directory -Force -Path $folder | Out-Null

# /codepage:65001: the source is UTF-8 ("Starting…"), which csc doesn't assume.
& $csc /nologo /target:winexe /codepage:65001 /optimize+ `
    "/win32icon:$(Join-Path $tray 'lectern.ico')" `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    "/out:$Out" (Join-Path $tray 'Lectern.cs')
if ($LASTEXITCODE -ne 0) {
    throw 'compiling the tray app failed (see above).'
}
Set-Content -LiteralPath (Join-Path $folder 'Lectern.version') -Value $Version -Encoding ASCII
