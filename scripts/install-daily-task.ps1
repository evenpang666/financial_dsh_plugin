[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $projectRoot 'scripts\run-scheduled-daily-push.ps1'
$taskName = 'DSH A-Share Pre-open Research'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$webServer = Join-Path $projectRoot 'scripts\report_web_server.py'
$webTaskName = 'DSH A-Share Report Site'
$factorTaskName = 'DSH A-Share Weekly Factor Training'
$factorScheduler = Join-Path $projectRoot 'scripts\factor_scheduler.py'
$powerShellExe = Join-Path $PSHOME 'powershell.exe'
$arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments -WorkingDirectory $projectRoot
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At '09:20'
    New-ScheduledTaskTrigger -Daily -At '14:30'
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Generate and push stock research report 1 at 09:20 and report 2 with intraday review at 14:30.' -Force | Out-Null
Write-Host "Installed Windows scheduled task: $taskName (daily 09:20 and 14:30; non-trading days are skipped)."

$webAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$webServer`"" -WorkingDirectory $projectRoot
$webTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$webSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $webTaskName -Action $webAction -Trigger $webTrigger -Settings $webSettings -Principal $principal -Description 'Serve archived twice-daily stock reports to this computer and its local network.' -Force | Out-Null
Write-Host "Installed Windows scheduled task: $webTaskName (starts at sign-in)."

$factorAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$factorScheduler`" --once" -WorkingDirectory $projectRoot
$factorTrigger = New-ScheduledTaskTrigger -Daily -At '12:00'
$factorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $factorTaskName -Action $factorAction -Trigger $factorTrigger -Settings $factorSettings -Principal $principal -Description 'Check each weekday at 12:00; train AI factors once per ISO week, retry on the next weekday after failure.' -Force | Out-Null
Write-Host "Installed Windows scheduled task: $factorTaskName (weekdays checked at 12:00; one successful run per week)."
