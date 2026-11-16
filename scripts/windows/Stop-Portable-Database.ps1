[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$app = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = Join-Path $app 'runtime\postgres\data'
$pgCtl = Join-Path $app 'postgresql\bin\pg_ctl.exe'
if (-not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    Write-Host 'Database is not initialized.'
    exit 0
}
& $pgCtl status -D $data *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Database is not running.'
    exit 0
}
& $pgCtl -D $data -m fast -w stop
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host 'Database stopped.'
