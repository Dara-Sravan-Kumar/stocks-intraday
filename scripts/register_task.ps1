# Registers the Windows Task Scheduler job that starts the bot each weekday
# before market open (08:55 IST). Run once from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run_bot.ps1"

# 08:55 IST expressed in local time
$ist = [System.TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$today = (Get-Date).Date
$istTime = [datetime]::SpecifyKind($today.AddHours(8).AddMinutes(55), 'Unspecified')
$localTime = [System.TimeZoneInfo]::ConvertTime($istTime, $ist, [System.TimeZoneInfo]::Local)

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $localTime.TimeOfDay.ToString()
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Intraday StockBot" -Action $action `
    -Trigger $trigger -Settings $settings -Force
Write-Host "Registered 'Intraday StockBot' to run weekdays at $($localTime.TimeOfDay) local (08:55 IST)."
