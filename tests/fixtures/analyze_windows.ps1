# PSScriptAnalyzer over the Windows scripts, warnings and errors, and their syntax checked
# against Windows PowerShell 5.1. Run in a PowerShell container by tests/test_packaging.py
# (the repository at /src), and on Windows by CI's windows job (-Root the checkout).
# Write-Host is the installers' way to talk: allowed. Exits with the number of findings.
param([string] $Root = '/src')
$ErrorActionPreference = 'Stop'
Install-Module PSScriptAnalyzer -RequiredVersion 1.23.0 -Force -Scope CurrentUser
$settings = @{
    Severity = @('Error', 'Warning')
    ExcludeRules = @('PSAvoidUsingWriteHost')
    Rules = @{ PSUseCompatibleSyntax = @{ Enable = $true; TargetVersions = @('5.1') } }
}
$found = Invoke-ScriptAnalyzer -Path (Join-Path $Root 'packaging/windows') -Recurse -Settings $settings
$found += Invoke-ScriptAnalyzer -Path (Join-Path $Root 'install.ps1') -Settings $settings
$found += Invoke-ScriptAnalyzer -Path (Join-Path $Root 'uninstall.ps1') -Settings $settings
$found | Format-Table -AutoSize -Wrap RuleName, ScriptName, Line, Message | Out-String -Width 200
exit $found.Count
