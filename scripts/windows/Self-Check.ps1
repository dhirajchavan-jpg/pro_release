[CmdletBinding()]
param()

# Self-check for the launcher package: parses every PowerShell script in this folder
# and exercises Start/Stop in -DryRun mode. Safe to run at any time; starts nothing
# and touches no processes.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$here = $PSScriptRoot
$failures = 0

Write-Host '== Parse check =='
foreach ($file in (Get-ChildItem -Path $here -Filter '*.ps1' | Sort-Object Name)) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors -and $parseErrors.Count -gt 0) {
        Write-Host "FAIL $($file.Name)"
        foreach ($parseError in $parseErrors) { Write-Host "  $($parseError.Message)" }
        $failures++
    } else {
        Write-Host "OK   $($file.Name)"
    }
}

Write-Host ''
Write-Host '== Dry-run check =='
& (Join-Path $here 'Start-All.ps1') -DryRun
$startExit = $LASTEXITCODE
Write-Host "Start-All -DryRun exit code: $startExit"
if ($startExit -ne 0) { $failures++ }
Write-Host ''
& (Join-Path $here 'Stop-All.ps1') -DryRun
$stopExit = $LASTEXITCODE
Write-Host "Stop-All -DryRun exit code: $stopExit"
# Exit 7 is the expected fail-closed result when a non-admin operator cannot
# inspect SYSTEM-owned command lines. The dry run has touched no process.
if ($stopExit -notin @(0, 7)) { $failures++ }
Write-Host ''
& (Join-Path $here 'Watch-Logs.ps1') -DryRun
$logsExit = $LASTEXITCODE
Write-Host "Watch-Logs -DryRun exit code: $logsExit"
if ($logsExit -ne 0) { $failures++ }

Write-Host ''
Write-Host '== Supervisor single-instance check =='
$testRuntime = Get-RuntimeDir
New-Item -ItemType Directory -Path $testRuntime -Force | Out-Null
$lock = $null
try {
    try {
        $lock = [IO.File]::Open((Join-Path $testRuntime 'supervisor.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [IO.IOException] {
        # A running supervisor already owns the same lock, which is also a valid probe state.
    }
    $singleInstanceOutput = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'Supervisor.ps1') -Once 2>&1 | Out-String)
    $singleInstanceExit = $LASTEXITCODE
    if ($singleInstanceExit -ne 0 -or $singleInstanceOutput -notmatch 'duplicate supervisor is exiting') {
        Write-Host 'FAIL duplicate supervisor was not rejected safely.'
        $failures++
    } else {
        Write-Host 'OK   duplicate supervisor exits without touching services.'
    }
} finally {
    if ($lock) { $lock.Dispose() }
}

Write-Host ''
if ($failures -gt 0) {
    Write-Host "SELF-CHECK FAILED: $failures check(s) failed."
    exit 1
}
Write-Host 'SELF-CHECK PASSED.'
exit 0
