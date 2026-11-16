[CmdletBinding()]
param([switch]$DryRun, [switch]$ElevatedRetry, [switch]$IncludeSupervisor)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$exitCode = 0

# Stop any in-progress startup first, then the worker so it cannot claim new
# operations while the API is going away.
$services = @(
    @{ Name = 'startup'; Marker = 'Start-All.ps1' },
    @{ Name = 'worker'; Marker = 'procurement-worker' },
    @{ Name = 'api';    Marker = 'procurement-api' }
)
if ($IncludeSupervisor) {
    $services = @(@{ Name = 'supervisor'; Marker = 'Supervisor.ps1' }) + $services
}

# Refuse the whole stop before touching anything when any PID belongs elsewhere.
$foreign = @()
foreach ($service in $services) {
    $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
    if ($state.Status -eq 'Foreign') {
        $foreign += "$($service.Name) PID $($state.ProcessId)"
    }
}
if ($foreign.Count -gt 0) {
    Write-Host "ERROR: refusing to stop any service because these PID files are not owned by this repo: $($foreign -join ', ')."
    exit 7
}

# Autostart runs the supervisor as SYSTEM so services survive sign-out and can
# recover after a reboot. Those processes live in Session 0 and cannot be
# stopped by a normal desktop token. Elevate only after PID ownership has been
# verified above; the elevated invocation performs the same checks again.
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$isAdministrator = $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$sessionZeroServices = @(
    foreach ($service in $services) {
        $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
        if ($state.Status -ne 'Running') { continue }
        $process = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
        if ($process -and $process.SessionId -eq 0) { $service.Name }
    }
)
if (-not $DryRun -and -not $isAdministrator -and $sessionZeroServices.Count -gt 0) {
    if ($ElevatedRetry) {
        Write-Host 'ERROR: elevated shutdown was requested but this process is not elevated.'
        exit 6
    }
    Write-Host "Administrator approval is required to stop autostart services: $($sessionZeroServices -join ', ')."
    $supervisorArgument = $(if ($IncludeSupervisor) { ' -IncludeSupervisor' } else { '' })
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -ElevatedRetry$supervisorArgument"
    try {
        $elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments `
            -Wait -PassThru -WindowStyle Hidden
        exit $elevated.ExitCode
    } catch {
        Write-Host 'ERROR: administrator approval was not granted; no service was stopped.'
        exit 6
    }
}

foreach ($service in $services) {
    $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
    switch ($state.Status) {
        'NotStarted' {
            Write-Host "$($service.Name): not running (no PID file)."
        }
        'Stale' {
            Write-Host "$($service.Name): stale PID file (PID $($state.ProcessId) is gone); removing."
            if (-not $DryRun) { Remove-ServicePidFile -Name $service.Name }
        }
        'Foreign' {
            Write-Host "ERROR: $($service.Name) PID $($state.ProcessId) is not owned by this repo (command line/repo path check failed). Refusing to stop it."
            $exitCode = 7
        }
        'Running' {
            if ($DryRun) {
                Write-Host "DRY RUN: would stop $($service.Name) (PID $($state.ProcessId))."
            } else {
                Write-Host "Stopping $($service.Name) (PID $($state.ProcessId))..."
                Stop-ProcessTree -ProcessId $state.ProcessId
                $gone = $false
                for ($attempt = 0; $attempt -lt 10 -and -not $gone; $attempt++) {
                    if (-not (Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue)) { $gone = $true }
                    else { Start-Sleep -Seconds 1 }
                }
                if (-not $gone) {
                    # The console entry-point parent can survive Stop-ProcessTree
                    # while a child is unwinding. Ownership was verified above,
                    # so escalate to Windows' process-tree terminator.
                    Write-Host "$($service.Name) did not stop gracefully; forcing its verified process tree..."
                    # Native stderr becomes a terminating RemoteException under
                    # ErrorActionPreference=Stop. The process check below is the
                    # authoritative result, so keep taskkill output quiet.
                    $previousPreference = $ErrorActionPreference
                    $ErrorActionPreference = 'Continue'
                    try { & taskkill.exe /PID $state.ProcessId /T /F *> $null }
                    finally { $ErrorActionPreference = $previousPreference }
                    for ($attempt = 0; $attempt -lt 10 -and -not $gone; $attempt++) {
                        if (-not (Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue)) { $gone = $true }
                        else { Start-Sleep -Seconds 1 }
                    }
                }
                if (-not $gone) {
                    Write-Host "ERROR: $($service.Name) (PID $($state.ProcessId)) did not exit after graceful and forced shutdown."
                    $exitCode = 6
                } else {
                    Remove-ServicePidFile -Name $service.Name
                    Write-Host "$($service.Name) stopped."
                }
            }
        }
    }
}

$bundledPostgres = Join-Path (Get-RepoRoot) 'postgresql\bin\postgres.exe'
$localPostgresEnabled = Get-EffectiveValue -Key 'LOCAL_POSTGRES_ENABLED'
if ($exitCode -eq 0 -and (Test-Path -LiteralPath $bundledPostgres) -and $localPostgresEnabled -ne '0') {
    if ($DryRun) {
        Write-Host 'DRY RUN: would stop the bundled local PostgreSQL database.'
    } else {
        & (Join-Path $PSScriptRoot 'Stop-Portable-Database.ps1')
        if ($LASTEXITCODE -ne 0) { $exitCode = 6 }
    }
}

exit $exitCode
