[CmdletBinding()]
param([switch]$DryRun)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot

function Resolve-Problem {
    # Hard failure on a real run; warning only during -DryRun so Self-Check stays green on unconfigured machines.
    param([string]$Message, [int]$Code)
    if ($DryRun) { Write-Host "WARN: $Message" }
    else { Write-Host "ERROR: $Message"; exit $Code }
}

# --- configuration checks (values are never printed) -------------------------------
$envFile = Join-Path $repo '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Resolve-Problem '.env not found. Run Setup.bat first, then edit .env.' 2
}

$dbUrl = Get-EffectiveValue -Key 'DATABASE_URL'
if (-not $dbUrl -or $dbUrl -match 'replace_') {
    Resolve-Problem 'DATABASE_URL is missing or still a placeholder in .env (value is never printed).' 2
}
$bundledPostgres = Join-Path $repo 'postgresql\bin\postgres.exe'
$localPostgresEnabled = Get-EffectiveValue -Key 'LOCAL_POSTGRES_ENABLED'
if ((Test-Path -LiteralPath $bundledPostgres) -and $localPostgresEnabled -ne '0') {
    if ($DryRun) {
        Write-Host 'DRY RUN: would prepare the bundled local PostgreSQL database.'
    } else {
        Write-Host 'Preparing database...'
        & (Join-Path $PSScriptRoot 'Portable-Database.ps1')
        if ($LASTEXITCODE -ne 0) { exit 4 }
    }
}

$port = 8000
$portText = Get-EffectiveValue -Key 'API_PORT'
if ($portText) {
    try { $port = [int]$portText } catch { Resolve-Problem 'API_PORT is not a valid number.' 2 }
}
if ($port -lt 1 -or $port -gt 65535) { Resolve-Problem 'API_PORT must be between 1 and 65535.' 2 }

$python = Get-VenvPython
if (-not (Test-Path -LiteralPath $python)) {
    Resolve-Problem 'Python environment is missing. Run Setup.bat first.' 4
}
foreach ($entry in @('procurement-api', 'procurement-worker', 'procurement-verify-config')) {
    if (-not (Test-Path -LiteralPath (Get-EntryExe -Name $entry))) {
        Resolve-Problem "Entry point $entry.exe missing. Run Setup.bat first." 4
    }
}

$runtimeDir = Get-RuntimeDir
$logDir = Join-Path $runtimeDir 'logs'
if (-not $DryRun) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$launchStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')

function Archive-Log {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $archiveDir = Join-Path $logDir 'archive'
    New-Item -ItemType Directory -Path $archiveDir -Force | Out-Null
    $destination = Join-Path $archiveDir "$launchStamp-$([IO.Path]::GetFileName($Path))"
    try {
        Move-Item -LiteralPath $Path -Destination $destination -ErrorAction Stop
    } catch {
        Write-Host "WARN: could not archive $Path because it is open; continuing with the existing log."
    }
}

# --- already-running / stale / foreign PID detection --------------------------------
$services = @(
    @{ Name = 'api';    Marker = 'procurement-api' },
    @{ Name = 'worker'; Marker = 'procurement-worker' }
)
$ownedApiRunning = $false
foreach ($service in $services) {
    $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
    switch ($state.Status) {
        'Running' {
            if ($service.Name -eq 'api') { $ownedApiRunning = $true }
            Resolve-Problem "$($service.Name) is already running (PID $($state.ProcessId)). Use Restart_All.bat or Stop_All.bat." 3
        }
        'Foreign' {
            Resolve-Problem "PID file for $($service.Name) points at PID $($state.ProcessId), which is not owned by this repo (command line check failed). Refusing to touch it; remove $(Get-ServicePidFile -Name $service.Name) manually if stale." 7
        }
        'Stale' {
            Write-Host "Removing stale PID file for $($service.Name) (PID $($state.ProcessId) is gone)."
            if (-not $DryRun) { Remove-ServicePidFile -Name $service.Name }
        }
    }
}

# A killed/interrupted Windows console launcher can lose its PID file while a
# child Python process remains alive. Clean up only processes whose command
# line proves they belong to this installation and expected entry point.
foreach ($service in $services) {
    $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
    if ($state.Status -eq 'Running') { continue }
    $orphanIds = @(Get-OwnedProcessIds -Marker $service.Marker)
    foreach ($orphanId in $orphanIds) {
        if ($DryRun) {
            Write-Host "DRY RUN: would stop orphaned owned $($service.Name) process $orphanId."
            continue
        }
        Write-Host "Stopping orphaned owned $($service.Name) process $orphanId..."
        Stop-ProcessTree -ProcessId $orphanId
    }
}

# --- API port must be free (any listener here is not ours; state check above passed) -
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener -and -not ($DryRun -and $ownedApiRunning)) {
    Resolve-Problem "TCP port $port is already listening and not owned by this repo's api service. Free it or change API_PORT in .env." 3
}

# --- production configuration preflight ----------------------------------------------
if ($DryRun) {
    Write-Host 'DRY RUN: would run production configuration preflight.'
} else {
    Write-Host 'Running production configuration preflight...'
    Push-Location $repo
    try {
        & (Get-EntryExe -Name 'procurement-verify-config')
        $preflightExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($preflightExitCode -ne 0) {
        Write-Host 'ERROR: production configuration preflight failed.'
        exit 2
    }
}

# --- database migrations -------------------------------------------------------------
$migrateLog = Join-Path $logDir 'migrate.log'
if ($DryRun) {
    Write-Host "DRY RUN: would run migrations: $python -m alembic upgrade head (log: $migrateLog)"
} else {
    Archive-Log -Path $migrateLog
    Write-Host 'Running database migrations (alembic upgrade head)...'
    Push-Location $repo
    try {
        $previousErrorAction = $ErrorActionPreference
        try {
            # PowerShell 5 wraps native stderr as ErrorRecord; Alembic writes normal INFO logs there.
            $ErrorActionPreference = 'Continue'
            & $python -m alembic upgrade head 2>&1 |
                ForEach-Object { $_.ToString() } |
                Tee-Object -FilePath $migrateLog
            $migrationExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorAction
        }
        if ($migrationExitCode -ne 0) {
            Write-Host "ERROR: migrations failed (exit $migrationExitCode). See $migrateLog. Is PostgreSQL running and DATABASE_URL correct?"
            exit 5
        }
    } finally { Pop-Location }
}

# --- start api hidden, wait for liveness ----------------------------------------------
function Start-Service {
    param([string]$Name, [string]$Entry)
    $exe = Get-EntryExe -Name $Entry
    $outLog = Join-Path $logDir "$Name.out.log"
    $errLog = Join-Path $logDir "$Name.err.log"
    if ($DryRun) {
        Write-Host "DRY RUN: would start $Name hidden -> $exe (logs: $outLog, $errLog)"
        return 0
    }
    foreach ($path in @($outLog, $errLog)) {
        Archive-Log -Path $path
    }
    $process = Start-Process -FilePath $exe -WorkingDirectory $repo -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Write-ServicePidFile -Name $Name -ProcessId $process.Id -Executable $exe -LogPath $outLog
    Write-Host "Started $Name (PID $($process.Id)); log: $outLog"
    return $process.Id
}

$apiPid = Start-Service -Name 'api' -Entry 'procurement-api'

if ($DryRun) {
    [void](Start-Service -Name 'worker' -Entry 'procurement-worker')
    Write-Host 'DRY RUN: no migrations run, no processes started.'
    exit 0
}

$healthy = $false
for ($attempt = 0; $attempt -lt 20 -and -not $healthy; $attempt++) {
    if (-not (Get-Process -Id $apiPid -ErrorAction SilentlyContinue)) { break }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $healthy = $true }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $healthy) {
    Write-Host "ERROR: api (PID $apiPid) did not become healthy on port $port. See $logDir\api.err.log."
    Stop-Process -Id $apiPid -Force -ErrorAction SilentlyContinue
    Remove-ServicePidFile -Name 'api'
    exit 6
}

# --- start worker hidden, verify it stays alive ---------------------------------------
$workerPid = Start-Service -Name 'worker' -Entry 'procurement-worker'
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: worker (PID $workerPid) exited immediately. See $logDir\worker.err.log."
    Remove-ServicePidFile -Name 'worker'
    $apiState = Get-ServiceState -Name 'api' -Marker 'procurement-api'
    if ($apiState.Status -eq 'Running') {
        Write-Host "Stopping owned api (PID $($apiState.ProcessId)) after worker startup failure..."
        Stop-Process -Id $apiState.ProcessId -Force -ErrorAction SilentlyContinue
        Remove-ServicePidFile -Name 'api'
    } elseif ($apiState.Status -eq 'Stale') {
        Remove-ServicePidFile -Name 'api'
    }
    exit 6
}

$ready = $false
for ($attempt = 0; $attempt -lt 30 -and -not $ready; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/ready" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) {
    Write-Host "ERROR: services started but did not become ready. See $logDir\api.err.log and $logDir\worker.err.log."
    foreach ($processId in @($workerPid, $apiPid)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Remove-ServicePidFile -Name 'worker'
    Remove-ServicePidFile -Name 'api'
    exit 6
}

Write-Host ''
Write-Host "All services running. Runtime dir: $runtimeDir"
Write-Host "API:    http://127.0.0.1:$port (PID $apiPid)"
Write-Host "Worker: PID $workerPid"
Write-Host 'Stop with Stop_All.bat.'
exit 0
