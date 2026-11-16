[CmdletBinding()]
param(
    [string]$ProjectId = '',
    [string]$OperationId = ''
)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot
$python = Get-VenvPython
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host 'Cost summary unavailable until Setup.bat completes.'
    exit 3
}

Push-Location $repo
try {
    $arguments = @('-m', 'procurement.usage_summary')
    if ($ProjectId) { $arguments += @('--project-id', $ProjectId) }
    if ($OperationId) { $arguments += @('--operation-id', $OperationId) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Cost summary unavailable. The database may not be running yet.'
        exit 3
    }
} finally {
    Pop-Location
}
