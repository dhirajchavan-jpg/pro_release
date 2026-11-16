[CmdletBinding()]
param([switch]$Disable, [switch]$DryRun)

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$taskName = 'Procurement Evaluation'
$supervisor = Join-Path $PSScriptRoot 'Supervisor.ps1'

if ($DryRun) {
    Write-Host "DRY RUN: would $($(if ($Disable) { 'disable' } else { 'enable' })) Windows startup task '$taskName'."
    exit 0
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $mode = $(if ($Disable) { ' -Disable' } else { '' })
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"$mode"
    try {
        $elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        if ($elevated.ExitCode -ne 0) { exit $elevated.ExitCode }
        $registered = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($Disable) {
            if ($registered) {
                Write-Host "ERROR: Windows startup task '$taskName' is still registered."
                exit 6
            }
        } elseif (-not $registered -or $registered.Actions.Arguments -notlike "*$supervisor*") {
            Write-Host "ERROR: Windows did not register the startup task for this installation."
            exit 6
        }
        exit 0
    } catch {
        Write-Host 'ERROR: administrator approval is required to configure automatic startup.'
        exit 4
    }
}

if ($Disable) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Write-Host "ERROR: Windows startup task '$taskName' is still registered."
        exit 6
    }
    & (Join-Path $PSScriptRoot 'Stop-All.ps1') -IncludeSupervisor
    exit $LASTEXITCODE
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$supervisor`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT1M'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'Keeps the Procurement Evaluation API and worker running.' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Automatic startup and crash recovery enabled: $taskName"
