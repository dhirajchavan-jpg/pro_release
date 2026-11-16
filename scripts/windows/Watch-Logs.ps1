[CmdletBinding()]
param(
    [ValidateRange(0, 1000)][int]$Tail = 80,
    [switch]$DryRun,
    [switch]$Once
)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$runtime = Get-RuntimeDir
$logDir = Join-Path $runtime 'logs'
$port = Get-EffectiveValue -Key 'API_PORT'
if (-not $port) { $port = '8000' }
$logs = @(
    'migrate.log',
    'api.out.log',
    'api.err.log',
    'worker.out.log',
    'worker.err.log',
    'supervisor.log'
) | ForEach-Object { Join-Path $logDir $_ }
$logs += Join-Path $runtime 'postgres\postgres.log'

Write-Host '=== Procurement Evaluation live logs ==='
Write-Host "Runtime: $runtime"
foreach ($service in @(
    @{ Name = 'api'; Marker = 'procurement-api' },
    @{ Name = 'worker'; Marker = 'procurement-worker' }
)) {
    $state = Get-ServiceState -Name $service.Name -Marker $service.Marker
    $displayStatus = $state.Status
    if ($state.Status -eq 'Foreign' -and $state.ProcessId) {
        $process = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -eq $service.Marker) {
            $displayStatus = 'Running (SYSTEM details restricted)'
        }
    }
    Write-Host "$($service.Name): $displayStatus$(if ($state.ProcessId) { " (PID $($state.ProcessId))" })"
}
try {
    $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$port/ready" -TimeoutSec 3
    Write-Host "Readiness: $($ready.status)"
} catch {
    Write-Host 'Readiness: unavailable'
}
Write-Host 'Press Ctrl+C to close this viewer. Services will continue running.'
Write-Host ''

if ($DryRun) {
    $logs | ForEach-Object { Write-Host "DRY RUN: would follow $_" }
    exit 0
}
if (-not (Test-Path -LiteralPath $logDir)) {
    Write-Host "ERROR: log directory does not exist: $logDir"
    Write-Host 'Run Deploy.bat or Start_All.bat first.'
    exit 6
}
$existing = @($logs | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
if (-not $existing) {
    Write-Host "ERROR: no launcher logs exist in $logDir"
    Write-Host 'Run Deploy.bat or Start_All.bat first.'
    exit 6
}

$positions = @{}
foreach ($path in $existing) {
    Write-Host "--- $([IO.Path]::GetFileName($path)) ---"
    Get-Content -LiteralPath $path -Tail $Tail | ForEach-Object { Write-Host ($_ -replace "`0", '') }
    $positions[$path] = (Get-Item -LiteralPath $path).Length
}
if ($Once) { exit 0 }

while ($true) {
    foreach ($path in $existing) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $length = (Get-Item -LiteralPath $path).Length
        if ($length -lt $positions[$path]) { $positions[$path] = 0 }
        if ($length -le $positions[$path]) { continue }
        $stream = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite,Delete')
        try {
            [void]$stream.Seek($positions[$path], 'Begin')
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8, $true, 4096, $true)
            try {
                foreach ($line in ($reader.ReadToEnd() -split "`r?`n")) {
                    if ($line) { Write-Host "[$([IO.Path]::GetFileName($path))] $line" }
                }
                $positions[$path] = $stream.Length
            } finally { $reader.Dispose() }
        } finally { $stream.Dispose() }
    }
    Start-Sleep -Milliseconds 500
}
