# PSScriptAnalyzer over the Windows scripts, warnings and errors, and their syntax checked
# against Windows PowerShell 5.1. Run in a PowerShell container by tests/test_packaging.py
# (the repository at /src). Write-Host is the installers' way to talk: allowed. Exits with the
# number of findings.
$ErrorActionPreference = 'Stop'
Install-Module PSScriptAnalyzer -RequiredVersion 1.23.0 -Force -Scope CurrentUser
$settings = @{
    Severity = @('Error', 'Warning')
    ExcludeRules = @('PSAvoidUsingWriteHost')
    Rules = @{ PSUseCompatibleSyntax = @{ Enable = $true; TargetVersions = @('5.1') } }
}
$found = Invoke-ScriptAnalyzer -Path /src/packaging/windows -Recurse -Settings $settings
$found += Invoke-ScriptAnalyzer -Path /src/install.ps1 -Settings $settings
$found += Invoke-ScriptAnalyzer -Path /src/uninstall.ps1 -Settings $settings
$found | Format-Table -AutoSize -Wrap RuleName, ScriptName, Line, Message | Out-String -Width 200
exit $found.Count
