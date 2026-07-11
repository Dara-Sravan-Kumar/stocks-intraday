# Starts the intraday bot dashboard on port 8503 (idempotent: exits if already up).
# Register at logon:  schtasks or Task Scheduler -> run this script.
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
