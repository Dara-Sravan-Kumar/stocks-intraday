# Registers the weekday 08:55 IST EQUITY paper session, alongside the options
# task. Run once:  powershell -ExecutionPolicy Bypass -File scripts\register_task_equity.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run_bot_equity.ps1"

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

Register-ScheduledTask -TaskName "Intraday StockBot Equity" -Action $action `
    -Trigger $trigger -Settings $settings -Force
Write-Host "Registered 'Intraday StockBot Equity' to run weekdays at $($localTime.TimeOfDay) local (08:55 IST)."
