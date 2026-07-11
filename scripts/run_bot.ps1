# Launches the intraday bot paper session. Called by Task Scheduler on weekday
# mornings. Exits instantly on holidays/weekends (run_live.py checks the calendar).
# NOTE: deliberately does NOT pass --live; live trading requires editing this
# by hand after strategies prove themselves on paper.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

# single-instance guard
$mutex = New-Object System.Threading.Mutex($false, "Global\stocks-intraday-bot")
if (-not $mutex.WaitOne(0)) {
    Write-Host "Bot already running - exiting."
    exit 0
}
try {
    Set-Location $root
    $env:PYTHONUTF8 = "1"
    # Options paper session (the current focus). For the equity paper book,
    # run manually in another terminal:  python run_live.py
    & $python run_live.py --options --no-dashboard
} finally {
    $mutex.ReleaseMutex()
}
