# Starts the intraday bot dashboard on http://localhost:8503 (idempotent: exits if already up).
# Launchable on demand from the win-task-dashboard hub (http://127.0.0.1:8787) via the
# "Intraday StockBot Dashboard" scheduled task — see scripts\register_dashboard_task.ps1.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$existing = Get-NetTCPConnection -LocalPort 8503 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Dashboard already running on http://localhost:8503"
    exit 0
}
Set-Location $root
$env:PYTHONUTF8 = "1"
& "$root\.venv\Scripts\streamlit.exe" run dashboard_web.py
