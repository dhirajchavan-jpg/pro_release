[CmdletBinding()]
param([int]$TimeoutSeconds = 300)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot
$runtime = Get-RuntimeDir
$supervisorScript = Join-Path $PSScriptRoot 'Supervisor.ps1'
$supervisorRecord = Read-ServicePidFile -Name 'supervisor'
$supervisorProcess = $null
if ($supervisorRecord -and $supervisorRecord.pid) {
    $supervisorProcess = Get-Process -Id ([int]$supervisorRecord.pid) -ErrorAction SilentlyContinue
}
$supervisorRunning = (
    $supervisorProcess -and
    [string]$supervisorRecord.repoRoot -eq $repo -and
    [string]$supervisorRecord.executable -eq 'powershell.exe' -and
    $supervisorProcess.ProcessName -eq 'powershell'
)
if (-not $supervisorRunning) {
    Write-Host 'Supervisor is not running; a direct restart is required.'
    exit 10
}

$startedAt = [DateTime]::MinValue
if (
    -not [DateTime]::TryParse(
        [string]$supervisorRecord.startedAtUtc,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$startedAt
    ) -or
    $startedAt.ToUniversalTime() -lt (Get-Item -LiteralPath $supervisorScript).LastWriteTimeUtc
) {
    Write-Host 'The running supervisor predates restart-request support; a direct restart is required.'
    exit 10
}

$api = Get-ServiceState -Name 'api' -Marker 'procurement-api'
$worker = Get-ServiceState -Name 'worker' -Marker 'procurement-worker'
if ($api.Status -ne 'Running' -or $worker.Status -ne 'Running') {
    Write-Host "Services are not both healthy (api=$($api.Status), worker=$($worker.Status)); supervisor recovery will continue automatically."
    exit 3
}

$requestPath = Join-Path $runtime 'restart.request.json'
$temporaryPath = "$requestPath.tmp"
$request = [ordered]@{
    version        = 1
    requestId      = [Guid]::NewGuid().ToString('N')
    repoRoot       = $repo
    requestedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    apiPid         = [int]$api.ProcessId
    workerPid      = [int]$worker.ProcessId
}
($request | ConvertTo-Json) | Set-Content -LiteralPath $temporaryPath -Encoding ascii
Move-Item -LiteralPath $temporaryPath -Destination $requestPath -Force
Write-Host "Restart requested; waiting for replacement API and worker processes..."

$port = Get-EffectiveValue -Key 'API_PORT'
if (-not $port) { $port = '8000' }
try { $port = [int]$port } catch { Write-Host 'ERROR: API_PORT is not a valid number.'; exit 2 }
if ($port -lt 1 -or $port -gt 65535) { Write-Host 'ERROR: API_PORT must be between 1 and 65535.'; exit 2 }
$deadline = (Get-Date).AddSeconds([Math]::Max(30, [Math]::Min(900, $TimeoutSeconds)))
do {
    Start-Sleep -Seconds 2
    $newApi = Get-ServiceState -Name 'api' -Marker 'procurement-api'
    $newWorker = Get-ServiceState -Name 'worker' -Marker 'procurement-worker'
    $replaced = (
        $newApi.Status -eq 'Running' -and
        $newWorker.Status -eq 'Running' -and
        [int]$newApi.ProcessId -ne [int]$api.ProcessId -and
        [int]$newWorker.ProcessId -ne [int]$worker.ProcessId
    )
    $ready = $false
    if ($replaced) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/ready" -UseBasicParsing -TimeoutSec 3
            $ready = $response.StatusCode -eq 200
        } catch { }
    }
} until ($ready -or (Get-Date) -ge $deadline)

if (-not $ready) {
    Write-Host "ERROR: supervisor restart did not become ready within the bounded wait. See runtime\logs\supervisor.log."
    exit 6
}

Write-Host "Restart complete: api PID $($newApi.ProcessId), worker PID $($newWorker.ProcessId)."
exit 0
