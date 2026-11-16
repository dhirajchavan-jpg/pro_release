[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$app = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$envFile = Join-Path $app '.env'
$pgBin = Join-Path $app 'postgresql\bin'
$runtime = Join-Path $app 'runtime\postgres'
$data = Join-Path $runtime 'data'
$logDir = Join-Path $app 'runtime\logs'
$log = Join-Path $logDir 'postgres.log'

function Get-Setting([string]$Name) {
    $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
}

$port = Get-Setting 'LOCAL_POSTGRES_PORT'
$user = Get-Setting 'LOCAL_POSTGRES_USER'
$password = Get-Setting 'LOCAL_POSTGRES_PASSWORD'
$database = Get-Setting 'LOCAL_POSTGRES_DB'
if (-not $port) { $port = '55432' }
if (-not $user) { $user = 'procurement_app' }
if (-not $database) { $database = 'procurement' }

if ($port -notmatch '^\d{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) { throw 'LOCAL_POSTGRES_PORT is invalid.' }
foreach ($value in @($user, $database)) {
    if ($value -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$') { throw 'Local PostgreSQL names may contain only letters, numbers, and underscores.' }
}
if (-not $password -or $password -match 'replace_') { throw 'LOCAL_POSTGRES_PASSWORD is missing.' }

$initdb = Join-Path $pgBin 'initdb.exe'
$pgCtl = Join-Path $pgBin 'pg_ctl.exe'
$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$psql = Join-Path $pgBin 'psql.exe'
$createdb = Join-Path $pgBin 'createdb.exe'
foreach ($file in @($initdb, $pgCtl, $pgIsReady, $psql, $createdb)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "PostgreSQL runtime is incomplete: $file" }
}

New-Item -ItemType Directory -Path $runtime, $logDir -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    New-Item -ItemType Directory -Path $data -Force | Out-Null
    $passwordFile = Join-Path $runtime 'password.tmp'
    try {
        [IO.File]::WriteAllText($passwordFile, $password, [Text.UTF8Encoding]::new($false))
        & $initdb --pgdata=$data --username=$user --pwfile=$passwordFile --auth-host=scram-sha-256 --auth-local=scram-sha-256 --encoding=UTF8 --no-locale
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL initialization failed.' }
    } finally {
        Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue
    }
}

& $pgCtl status -D $data *> $null
$ownedRunning = $LASTEXITCODE -eq 0
if (-not $ownedRunning) {
    & $pgIsReady -h 127.0.0.1 -p $port *> $null
    if ($LASTEXITCODE -eq 0) {
        $previousPassword = $env:PGPASSWORD
        try {
            $env:PGPASSWORD = $password
            $reportedData = (& $psql -h 127.0.0.1 -p $port -U $user -d postgres -tAc 'SHOW data_directory' 2>$null | Out-String).Trim()
        } finally {
            $env:PGPASSWORD = $previousPassword
        }
        if ($LASTEXITCODE -eq 0 -and
            [IO.Path]::GetFullPath($reportedData).TrimEnd('\', '/') -ieq [IO.Path]::GetFullPath($data).TrimEnd('\', '/')) {
            $ownedRunning = $true
        } else {
            throw "Port $port is already used by another PostgreSQL server."
        }
    }
}
if (-not $ownedRunning) {
    $arguments = @(
        '-D', "`"$data`"",
        '-l', "`"$log`"",
        '-o', "`"-p $port -h 127.0.0.1`"",
        '-w', 'start'
    )
    $process = Start-Process -FilePath $pgCtl -ArgumentList $arguments -PassThru -WindowStyle Hidden
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "PostgreSQL did not start. See $log" }
}

$previousPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = $password
    $existsResult = & $psql -h 127.0.0.1 -p $port -U $user -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$database'" 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL authentication failed.' }
    $exists = (($existsResult | Out-String).Trim())
    if ($exists -ne '1') {
        & $createdb -h 127.0.0.1 -p $port -U $user $database
        if ($LASTEXITCODE -ne 0) { throw 'Database creation failed.' }
    }
} finally {
    $env:PGPASSWORD = $previousPassword
}

Write-Host 'Database ready.'
