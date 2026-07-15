# Registers an on-demand Windows Task Scheduler job that launches the intraday
# bot's web dashboard (Streamlit on http://localhost:8503). No trigger — it is
# meant to be started from the win-task-dashboard launcher hub
# (http://127.0.0.1:8787) with its ▶ Run button, exactly like "StockBot Web
# Dashboard". run_dashboard.ps1 is idempotent, so clicking Run twice is a no-op.
#
# Run once from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\register_dashboard_task.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run_dashboard.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
# On-demand only: no time/weekly trigger, so it just sits idle until launched.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Intraday StockBot Dashboard" -Action $action `
    -Settings $settings -Force | Out-Null
Write-Host "Registered 'Intraday StockBot Dashboard' (on-demand)."
Write-Host "Launch it from http://127.0.0.1:8787 (▶ Run) -> opens http://localhost:8503"
