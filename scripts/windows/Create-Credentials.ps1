[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot
$python = Get-VenvPython
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host 'ERROR: application runtime is not ready. Run Setup.bat first.'
    exit 2
}

Push-Location $repo
try {
    & $python -m procurement.manage_credentials --env (Join-Path $repo '.env')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host 'Restart the application with Restart_All.bat before using the new credentials.'
} finally {
    Pop-Location
}
