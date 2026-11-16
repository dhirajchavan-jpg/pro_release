[CmdletBinding()]
param([switch]$Once, [switch]$DryRun)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot
$runtime = Get-RuntimeDir
$logDir = Join-Path $runtime 'logs'
$log = Join-Path $logDir 'supervisor.log'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 10MB) {
    Move-Item -LiteralPath $log -Destination "$log.previous" -Force
}

function Write-SupervisorLog([string]$Message) {
    $line = "$(Get-Date -Format o) $Message"
    [IO.File]::AppendAllText($log, "$line`r`n", [Text.UTF8Encoding]::new($false))
    if ($DryRun -or $Once) { Write-Host $line }
}

function Test-Ready {
    $port = Get-EffectiveValue -Key 'API_PORT'
    if (-not $port) { $port = '8000' }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/ready" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Test-WorkerHeartbeat {
    $path = Get-EffectiveValue -Key 'WORKER_HEARTBEAT_FILE'
    if (-not $path) { $path = 'runtime\worker.heartbeat.json' }
    if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $repo $path }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $maximumAge = 30
    $configuredAge = Get-EffectiveValue -Key 'WORKER_HEARTBEAT_MAX_AGE_SECONDS'
    if ($configuredAge) {
        $parsedAge = 0
        if ([int]::TryParse($configuredAge, [ref]$parsedAge)) {
            $maximumAge = [Math]::Max(5, $parsedAge)
        }
    }
    return ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $path).LastWriteTimeUtc).TotalSeconds -le $maximumAge
}

function Invoke-Launcher([string]$ScriptName) {
    Get-ChildItem -LiteralPath $runtime -Filter 'supervisor-*.tmp' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $token = [Guid]::NewGuid().ToString('N')
    $stdout = Join-Path $runtime "supervisor-$ScriptName-$token.out.tmp"
    $stderr = Join-Path $runtime "supervisor-$ScriptName-$token.err.tmp"
    $script = Join-Path $PSScriptRoot $ScriptName
    $process = $null
    $tracksStartup = $ScriptName -eq 'Start-All.ps1'
    $timeoutSeconds = 180
    $configuredTimeout = Get-EffectiveValue -Key 'SUPERVISOR_LAUNCHER_TIMEOUT_SECONDS'
    if ($configuredTimeout) {
        $parsedTimeout = 0
        if ([int]::TryParse($configuredTimeout, [ref]$parsedTimeout)) {
            $timeoutSeconds = [Math]::Max(1, [Math]::Min(900, $parsedTimeout))
        }
    }
    try {
        $process = Start-Process powershell.exe -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$script`""
        ) -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr -PassThru
        if ($tracksStartup) {
            try {
                Write-ServicePidFile -Name 'startup' -ProcessId $process.Id -Executable 'powershell.exe' -LogPath $stdout
            } catch {
                Stop-ProcessTree -ProcessId $process.Id
                throw
            }
        }
        # Start-Process -Wait follows the API/worker descendants and never returns.
        [void]$process.Handle
        if (-not $process.WaitForExit($timeoutSeconds * 1000)) {
            Write-SupervisorLog "$ScriptName exceeded $timeoutSeconds seconds; stopping its owned process tree."
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try { & taskkill.exe /PID $process.Id /T /F *> $null }
            finally { $ErrorActionPreference = $previousPreference }
            [void]$process.WaitForExit(10000)
            if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
                Write-SupervisorLog "ERROR: $ScriptName process tree survived forced shutdown; retaining its PID file."
                return 6
            }
            return 124
        }
        $process.Refresh()
        return $process.ExitCode
    } finally {
        if ($tracksStartup -and $process -and -not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
            Remove-ServicePidFile -Name 'startup'
        }
        foreach ($path in @($stdout, $stderr)) {
            if (Test-Path -LiteralPath $path) {
                $stream = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
                $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false), $true)
                try { $lines = $reader.ReadToEnd() -split "\r?\n" }
                finally { $reader.Dispose(); $stream.Dispose() }
                foreach ($line in $lines) {
                    if ($line) { Write-SupervisorLog "launcher: $($line -replace "`0", '')" }
                }
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Set-RecoveryBackoff([string]$Message) {
    $script:recoveryFailures++
    $script:sleepSeconds = [int][Math]::Min(
        300,
        15 * [Math]::Pow(2, [Math]::Min(5, $script:recoveryFailures))
    )
    Write-SupervisorLog "$Message Next attempt in $($script:sleepSeconds) seconds."
}

function Invoke-Recovery([string]$ApiStatus, [string]$WorkerStatus) {
    Write-SupervisorLog "Recovering api=$ApiStatus, worker=$WorkerStatus."
    $stopExit = Invoke-Launcher 'Stop-All.ps1'
    if ($stopExit -ne 0) {
        Set-RecoveryBackoff "Recovery shutdown failed with exit code $stopExit; startup was not attempted."
        return
    }
    $startExit = Invoke-Launcher 'Start-All.ps1'
    Write-SupervisorLog "Recovery finished with exit code $startExit."
    if ($startExit -ne 0) {
        Set-RecoveryBackoff "Recovery failed $($script:recoveryFailures + 1) consecutive time(s)."
    } else {
        $script:sleepSeconds = 15
    }
}

function Get-RestartRequest {
    $path = Join-Path $runtime 'restart.request.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $request = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $requestedAt = [DateTime]::MinValue
        if (
            [int]$request.version -ne 1 -or
            [string]$request.repoRoot -ne $repo -or
            -not [DateTime]::TryParse(
                [string]$request.requestedAtUtc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$requestedAt
            ) -or
            ((Get-Date).ToUniversalTime() - $requestedAt.ToUniversalTime()).TotalMinutes -gt 10
        ) {
            throw 'invalid or expired restart request'
        }
        Remove-Item -LiteralPath $path -Force
        return [string]$request.requestId
    } catch {
        Write-SupervisorLog "Discarding invalid restart request: $($_.Exception.Message)."
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        return $null
    }
}

$lockStream = $null
if (-not $DryRun) {
    $lockPath = Join-Path $runtime 'supervisor.lock'
    try {
        $lockStream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [IO.IOException] {
        Write-SupervisorLog 'Another supervisor owns this installation; duplicate supervisor is exiting.'
        exit 0
    }
    Write-ServicePidFile -Name 'supervisor' -ProcessId $PID -Executable 'powershell.exe' -LogPath $log
}

try {
    Write-SupervisorLog "Supervisor started for $repo."
    $healthyCycles = 0
    $failedReadyCycles = 0
    $script:recoveryFailures = 0
    $script:sleepSeconds = 15
    do {
        try {
            $api = Get-ServiceState -Name 'api' -Marker 'procurement-api'
            $worker = Get-ServiceState -Name 'worker' -Marker 'procurement-worker'
            $restartRequestId = Get-RestartRequest
            if ($restartRequestId) {
                $healthyCycles = 0
                $failedReadyCycles = 0
                Write-SupervisorLog "Processing local restart request $restartRequestId."
                if (-not $DryRun) { Invoke-Recovery $api.Status $worker.Status }
            } elseif ($api.Status -eq 'Foreign' -or $worker.Status -eq 'Foreign') {
                $healthyCycles = 0
                Set-RecoveryBackoff 'Recovery refused: a PID file points to a process not owned by this installation.'
            } elseif ($api.Status -eq 'Running' -and $worker.Status -eq 'Running' -and -not (Test-Ready)) {
                $healthyCycles = 0
                if (Test-WorkerHeartbeat) {
                    $failedReadyCycles = 0
                    Write-SupervisorLog 'Readiness is degraded but both processes and the worker heartbeat are healthy; restart suppressed.'
                } else {
                    $failedReadyCycles++
                    Write-SupervisorLog "Worker heartbeat is stale ($failedReadyCycles/3); services remain running."
                    if ($failedReadyCycles -ge 3 -and -not $DryRun) {
                        Invoke-Recovery $api.Status $worker.Status
                        $failedReadyCycles = 0
                    }
                }
            } elseif ($api.Status -ne 'Running' -or $worker.Status -ne 'Running') {
                $healthyCycles = 0
                $failedReadyCycles = 0
                if ($DryRun) {
                    Write-SupervisorLog "DRY RUN: would recover api=$($api.Status), worker=$($worker.Status)."
                } else {
                    Invoke-Recovery $api.Status $worker.Status
                }
            } else {
                $failedReadyCycles = 0
                $healthyCycles++
                $script:recoveryFailures = 0
                $script:sleepSeconds = 15
                if ($healthyCycles % 4 -eq 0) {
                    Write-SupervisorLog "Healthy: api PID $($api.ProcessId), worker PID $($worker.ProcessId), readiness ready."
                }
            }
        } catch {
            Set-RecoveryBackoff "Supervisor cycle failed: $($_.Exception.GetType().Name): $($_.Exception.Message)."
        }
        if (-not $Once) { Start-Sleep -Seconds $script:sleepSeconds }
    } while (-not $Once)
} finally {
    if (-not $DryRun) { Remove-ServicePidFile -Name 'supervisor' }
    if ($lockStream) { $lockStream.Dispose() }
}
