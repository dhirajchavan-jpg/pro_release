[CmdletBinding()]
param([switch]$AllowConfiguredEnv)

Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$manifest = Join-Path $repo 'PACKAGE_MANIFEST.sha256'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Host 'ERROR: PACKAGE_MANIFEST.sha256 is missing.'
    exit 1
}

$failures = 0
foreach ($line in [System.IO.File]::ReadAllLines($manifest)) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '  ', 2
    if ($parts.Count -ne 2) {
        Write-Host "ERROR: invalid manifest line: $line"
        $failures++
        continue
    }
    if ($AllowConfiguredEnv -and $parts[1] -eq '.env') { continue }
    $path = Join-Path $repo ($parts[1] -replace '/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Host "MISSING $($parts[1])"
        $failures++
        continue
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $parts[0].ToLowerInvariant()) {
        Write-Host "CHANGED $($parts[1])"
        $failures++
    }
}

if ($failures) {
    Write-Host "PACKAGE VERIFICATION FAILED: $failures problem(s)."
    exit 1
}
Write-Host 'PACKAGE VERIFIED.'
exit 0
