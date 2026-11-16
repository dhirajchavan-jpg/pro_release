# Shared helpers for the Windows no-Docker launcher package (Setup/Start/Stop/Restart).
# Dot-sourced by the other scripts in this folder. Never prints .env values.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Stable exit-code contract used by the .bat wrappers:
#   0 success
#   2 configuration error (.env missing, placeholder values)
#   3 already running, or API port busy
#   4 missing dependency (Python 3.12+, venv, entry points, PostgreSQL 15+)
#   5 database migrations failed
#   6 a service failed to start, become healthy, or stop
#   7 PID file points at a process this repo does not own (refused to touch)

function Get-RepoRoot {
    (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$Path = (Join-Path (Get-RepoRoot) '.env')
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $separator = $trimmed.IndexOf('=')
        if ($separator -lt 1) { continue }
        if ($trimmed.Substring(0, $separator).Trim() -eq $Key) {
            return $trimmed.Substring($separator + 1).Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Import-PortablePackageEnvironment {
    $repo = Get-RepoRoot
    $envFile = Join-Path $repo '.env'
    $bundledPostgres = Join-Path $repo 'postgresql\bin\postgres.exe'
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf) -or
        -not (Test-Path -LiteralPath $bundledPostgres -PathType Leaf)) {
        return
    }
    foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $separator = $trimmed.IndexOf('=')
        if ($separator -lt 1) { continue }
        $key = $trimmed.Substring(0, $separator).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $value = $trimmed.Substring($separator + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

# The self-contained package carries its own PostgreSQL runtime and .env. Keep
# inherited machine variables from silently redirecting any packaged service.
Import-PortablePackageEnvironment

function Get-EffectiveValue {
    # Process environment wins, matching procurement.platform.env load_local_env (os.environ.setdefault).
    param([Parameter(Mandatory = $true)][string]$Key)
    $value = [Environment]::GetEnvironmentVariable($Key)
    if ($value) { return $value }
    return Get-DotEnvValue -Key $Key
}

function Get-RuntimeDir {
    # Configurable via RUNTIME_DIR (environment or .env); defaults to <repo>\runtime.
    $dir = Get-EffectiveValue -Key 'RUNTIME_DIR'
    if (-not $dir) { $dir = 'runtime' }
    if (-not [System.IO.Path]::IsPathRooted($dir)) { $dir = Join-Path (Get-RepoRoot) $dir }
    return $dir
}

function Get-PythonRuntimeDir {
    $configured = Get-EffectiveValue -Key 'PYTHON_RUNTIME_DIR'
    if ($configured) {
        if (-not [IO.Path]::IsPathRooted($configured)) {
            $configured = Join-Path (Get-RepoRoot) $configured
        }
        return [IO.Path]::GetFullPath($configured)
    }
    $repo = Get-RepoRoot
    if ($repo.Length -le 80) { return (Join-Path $repo '.venv') }
    $userProfile = [Environment]::GetFolderPath('UserProfile')
    if (-not $userProfile) { throw 'Windows user profile is unavailable.' }
    return (Join-Path $userProfile '.procurement-evaluation\venv')
}

function Get-PrivatePythonRuntimeDir {
    $venv = Get-PythonRuntimeDir
    if ($venv -eq (Join-Path (Get-RepoRoot) '.venv')) {
        return (Join-Path (Get-RepoRoot) '.python')
    }
    return (Join-Path (Split-Path $venv -Parent) 'python312')
}

function Get-VenvPython { Join-Path (Get-PythonRuntimeDir) 'Scripts\python.exe' }

function Get-EntryExe {
    param([Parameter(Mandatory = $true)][string]$Name)
    Join-Path (Get-PythonRuntimeDir) "Scripts\$Name.exe"
}

function Get-ServicePidFile {
    param([Parameter(Mandatory = $true)][string]$Name)
    Join-Path (Get-RuntimeDir) "$Name.pid.json"
}

function Read-ServicePidFile {
    param([Parameter(Mandatory = $true)][string]$Name)
    $path = Get-ServicePidFile -Name $Name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json) }
    catch {
        Write-Host "WARN: PID file $path is unreadable ($($_.Exception.Message)); treating service as not started. Remove it manually if stale."
        return $null
    }
}

function Write-ServicePidFile {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$LogPath
    )
    $record = [ordered]@{
        name         = $Name
        pid          = $ProcessId
        executable   = $Executable
        repoRoot     = Get-RepoRoot
        logPath      = $LogPath
        startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    }
    $dir = Get-RuntimeDir
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($record | ConvertTo-Json) | Set-Content -LiteralPath (Get-ServicePidFile -Name $Name) -Encoding ascii
}

function Remove-ServicePidFile {
    param([Parameter(Mandatory = $true)][string]$Name)
    $path = Get-ServicePidFile -Name $Name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $children = @(
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.ProcessId }
    )
    foreach ($childId in $children) { Stop-ProcessTree -ProcessId $childId }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-ProcessCommandLine {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $row = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($row) { return $row.CommandLine }
    return $null
}

function Test-OwnedProcess {
    # A process counts as ours only if its command line points inside this repo
    # and names the expected service entry point.
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Marker
    )
    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    if (-not $commandLine) { return $false }
    $lowered = $commandLine.ToLowerInvariant()
    $ownedRoots = @((Get-RepoRoot), (Get-PythonRuntimeDir))
    return (
        $lowered.Contains($Marker.ToLowerInvariant()) -and
        @($ownedRoots | Where-Object { $lowered.Contains($_.ToLowerInvariant()) }).Count -gt 0
    )
}

function Get-OwnedProcessIds {
    # Recover descendants left behind when a console-script wrapper exits or
    # a launcher is interrupted before its PID file is committed.
    param([Parameter(Mandatory = $true)][string]$Marker)
    $ownedRoots = @((Get-RepoRoot), (Get-PythonRuntimeDir)) |
        ForEach-Object { $_.ToLowerInvariant() }
    $wanted = $Marker.ToLowerInvariant()
    @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $commandLine = [string]$_.CommandLine
                $commandLine -and
                @($ownedRoots | Where-Object { $commandLine.ToLowerInvariant().Contains($_) }).Count -gt 0 -and
                $commandLine.ToLowerInvariant().Contains($wanted)
            } |
            ForEach-Object { [int]$_.ProcessId }
    )
}

function Get-ServiceState {
    # NotStarted | Running | Stale (PID file but process gone) | Foreign (PID alive but not ours)
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Marker
    )
    $record = Read-ServicePidFile -Name $Name
    if (-not $record -or -not $record.pid) { return @{ Status = 'NotStarted'; ProcessId = 0 } }
    $processId = [int]$record.pid
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) { return @{ Status = 'Stale'; ProcessId = $processId } }
    if (Test-OwnedProcess -ProcessId $processId -Marker $Marker) { return @{ Status = 'Running'; ProcessId = $processId } }
    $expectedExecutable = Get-EntryExe -Name $Marker
    if (
        $record.repoRoot -eq (Get-RepoRoot) -and
        $record.executable -eq $expectedExecutable -and
        $process.ProcessName -eq $Marker
    ) {
        return @{ Status = 'Running'; ProcessId = $processId }
    }
    return @{ Status = 'Foreign'; ProcessId = $processId }
}
