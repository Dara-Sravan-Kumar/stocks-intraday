# Launches the intraday bot EQUITY paper session (mode PAPER, the ₹20L book:
# the six 5m strategies + the DISCOVERED_EQ channel). Runs alongside the options
# session — its OWN single-instance mutex so the two never block each other.
# Uses the free yfinance feed so it doesn't contend with the options Fyers feed.
# Deliberately no --live: live trading requires editing this by hand.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

# single-instance guard (distinct from the options launcher's mutex)
$mutex = New-Object System.Threading.Mutex($false, "Global\stocks-intraday-bot-equity")
if (-not $mutex.WaitOne(0)) {
    Write-Host "Equity bot already running - exiting."
    exit 0
}
try {
    Set-Location $root
    $env:PYTHONUTF8 = "1"
    & $python run_live.py --no-dashboard --feed yf
} finally {
    $mutex.ReleaseMutex()
}
