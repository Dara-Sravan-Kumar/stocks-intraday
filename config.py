"""Single source of truth for every tunable in the intraday bot.

Logic modules must never hardcode thresholds — everything lives here.
Secrets come from .env (loaded once, below); accessor helpers wrap os.getenv.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"

TIMEZONE = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# Session times (IST). Phases derived in bot/clock.py.
# ---------------------------------------------------------------------------
SESSION = {
    "preopen_start": "09:00",
    "market_open": "09:15",
    "entries_start": "09:20",      # no entries in the first noisy minutes
    "no_new_entries": "14:45",     # last time a fresh entry may fill
    "square_off": "15:12",         # bot force-exits everything (broker MIS cutoff ~15:15-15:20)
    "market_close": "15:30",
}

# NSE equity trading holidays 2026 (verified vs cleartax/NSE circular, 2026-07-09).
# clock.py also treats "weekday but zero bars arriving" as a soft holiday signal.
NSE_HOLIDAYS = [
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
]

# ---------------------------------------------------------------------------
# Universe: Nifty 50 + Bank Nifty constituents.
# Primary source: niftyindices CSVs (refreshed weekly, cached in DB).
# The static lists below are a fallback snapshot only.
# ---------------------------------------------------------------------------
UNIVERSE_CSV_URLS = {
    "NIFTY50": "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "BANKNIFTY": "https://niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
}
UNIVERSE_CSV_MIRRORS = {
    "NIFTY50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "BANKNIFTY": "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
}
UNIVERSE_REFRESH_DAYS = 7
UNIVERSE_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}

FALLBACK_NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]
FALLBACK_BANKNIFTY = [
    "AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK",
    "HDFCBANK", "ICICIBANK", "IDFCFIRSTB", "INDUSINDBK", "KOTAKBANK",
    "PNB", "SBIN",
]

# Index symbols tracked for the circuit breaker (not traded).
INDEX_SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}

# ---------------------------------------------------------------------------
# Data feeds
# ---------------------------------------------------------------------------
FEED_PREFERENCE = "auto"          # auto -> dhan if token present & socket auths, else yf
YF_POLL_SECONDS = 60              # one batched 1m download per minute
YF_SUFFIX = ".NS"
BAR_INTERVAL_MIN = 1              # base bars; strategies mostly consume 5m rollups
STRATEGY_INTERVAL_MIN = 5
WARMUP_DAYS = 12                  # 1m history backfill for RVOL / prev-day levels
MIN_BARS_FOR_SIGNALS = 2          # 5m bars needed before a strategy may fire

DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DHAN_SCRIP_CACHE_DAYS = 7
# Index security ids on Dhan (IDX_I segment); verified against the scrip master.
DHAN_INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25"}
DHAN_FEED_MAX_ERRORS = 5          # consecutive fatal errors before degrading to yfinance

# Fyers (free data + order APIs). Symbols: NSE:<SYMBOL>-EQ; indices below.
FYERS_INDEX_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX"}
FYERS_TOKENS_FILE = CACHE_DIR / "fyers_tokens.json"
FYERS_HISTORY_CHUNK_DAYS = 60     # minute-resolution history request window
FYERS_FEED_MAX_ERRORS = 5

# ---------------------------------------------------------------------------
# Capital, sizing, risk (paper)
# ---------------------------------------------------------------------------
# PAPER-TEST PROFILE (2026-07): ₹20L book, deliberately SMALL trades, and every
# strategy allowed to trade the SAME symbol in parallel so they can be compared
# head-to-head on identical setups. Costs/brokerage are fully modelled (bot/costs.py)
# so small trades show their true fee drag. Tighten these before any live use.
PAPER_STARTING_CASH = 2_000_000.0   # ₹20 lakh paper equity book
RISK_PER_TRADE_PCT = 0.25         # small: ~₹5,000 risk per trade on a ₹20L book
MAX_NOTIONAL_PCT = 5.0            # small: single position notional cap ~₹1L
INTRADAY_LEVERAGE = 5.0           # MIS margin = notional / leverage
MAX_MARGIN_PCT = 90.0             # total margin in use, % of equity
MIN_QTY = 1

MAX_DAILY_LOSS_PCT = 5.0          # testing headroom (was 2.0) so a bad day doesn't halt data collection
MAX_ENTRIES_PER_DAY = 200         # testing: let every strategy fire (was 4)
REGIME_FILTER_ENABLED = True      # longs need NIFTY >= day open; shorts need NIFTY <= open
MAX_CONCURRENT_POSITIONS = 40     # many small parallel test positions (was 6)
MAX_POSITIONS_PER_STRATEGY = 10   # was 3
MAX_POSITIONS_PER_SYMBOL = 8      # allow up to 8 different strategies on ONE symbol (was 1, first-wins)
MAX_TRADES_PER_DAY_PER_STRATEGY = 30   # was 10
CONSECUTIVE_LOSSES_TO_BENCH = 99  # testing: don't bench a strategy mid-test (was 3)

# Circuit breaker on index shock: pause new entries.
CIRCUIT_INDEX_MOVE_15M_PCT = 1.0
CIRCUIT_INDEX_MOVE_OPEN_PCT = 2.5
CIRCUIT_PAUSE_MINUTES = 30

# ---------------------------------------------------------------------------
# Intraday (MIS) cost model — Dhan equity intraday, verified 2026-07-09.
# ---------------------------------------------------------------------------
COSTS = {
    "brokerage_pct": 0.03,        # min(=pct of order value, cap) per executed order
    "brokerage_cap": 20.0,
    "stt_sell_pct": 0.025,        # sell side only for intraday
    "exchange_txn_pct": 0.00297,  # NSE, both sides
    "sebi_pct": 0.0001,           # both sides
    "stamp_buy_pct": 0.003,       # buy side only
    "gst_pct": 18.0,              # on brokerage + exchange txn + sebi
}
SLIPPAGE_BPS = 3.0                # per side, applied to paper fills

# ---------------------------------------------------------------------------
# Strategy parameters. Keys are strategy names registered in bot/strategies.
# Every number a strategy uses must come from here.
# ---------------------------------------------------------------------------
STRATEGY_PARAMS = {
    "orb": {
        "enabled": True,
        "or_minutes": 15,                 # 09:15-09:30
        "min_or_range_pct": 0.25,
        "max_or_range_pct": 2.5,
        "breakout_vol_mult": 2.0,         # vs mean OR-bar volume
        "entry_deadline": "12:00",
        "target_r": 2.0,
        "max_trades_per_direction": 1,
    },
    # Backtested PF 0.13-0.37 (edge doesn't clear costs) — ON now only to gather
    # forward paper-test data alongside the others. Not a profit expectation.
    "vwap_reversion": {
        "enabled": True,
        "entry_start": "10:00",
        "entry_end": "14:30",
        "band_sigma": 2.5,
        "rsi_period": 14,
        "rsi_overbought": 72.0,
        "rsi_oversold": 28.0,
        "max_day_change_pct": 1.2,        # skip trend days
        "stop_sigma": 1.0,
        "stop_floor_pct": 0.45,
        "time_stop_min": 60,
        "max_trades_per_day": 1,
    },
    "vwap_pullback": {
        "enabled": True,
        "entry_start": "10:00",
        "entry_end": "14:30",
        "min_side_minutes": 45,           # time spent on one side of VWAP
        "vwap_slope_bars": 6,
        "min_day_change_pct": 0.5,
        "touch_tolerance_pct": 0.10,
        "stop_buffer_pct": 0.10,
        "max_risk_pct": 0.6,
        "target_r": 2.0,
        "breakeven_at_r": 1.0,
        "max_trades_per_day": 1,
    },
    "momentum_breakout": {
        "enabled": True,
        "entry_start": "09:30",
        "entry_end": "14:30",
        "rvol_min": 2.5,
        "max_range_vs_avg": 2.0,          # day range vs 10d avg daily range
        "stop_max_pct": 0.45,             # stop distance floor rule
        "max_risk_pct": 0.6,
        "target_r": 2.0,
        "trail_ema_period": 20,
        "trail_after_r": 1.0,
        "max_trades_per_direction": 1,
    },
    "gap": {
        "enabled": True,
        "entry_start": "09:20",
        "entry_end": "10:30",
        "go_gap_min_pct": 0.75,
        "go_gap_max_pct": 3.0,
        "go_hold_frac": 0.4,              # must hold above prev_close + frac*gap
        "fade_gap_min_pct": 0.30,
        "fade_gap_max_pct": 0.75,
        "target_r": 2.0,
        "fade_stop_buffer_pct": 0.10,
        "max_trades_per_day": 1,
    },
    # --- 15-minute research strategies (run with --interval 15) ---
    # Disabled by default until backtests prove them; see DAYTYPE thresholds below.
    "trend_day": {
        "enabled": False,
        "entry_start": "10:15",
        "entry_end": "12:30",
        "rvol_min": 1.5,
        "stop_vwap_buffer_pct": 0.3,      # stop = max(day extreme, VWAP -/+ buffer)
        "max_risk_pct": 1.2,
        "trail_after_r": 1.0,             # then trail stop along VWAP
        "max_trades_per_day": 1,
    },
    "range_fade": {
        "enabled": False,
        "entry_start": "11:00",
        "entry_end": "14:15",
        "edge_zone": 0.2,                 # close within this fraction of day range edge
        "rsi7_long_below": 35.0,
        "rsi7_short_above": 65.0,
        "min_day_range_pct": 0.5,         # too-narrow ranges aren't worth the costs
        "min_reward_pct": 0.30,           # distance to VWAP target must clear costs
        "stop_buffer_pct": 0.30,          # beyond the day extreme
        "time_stop_min": 90,
        "max_trades_per_day": 1,
    },
    # --- Index options strategies (run_live --options; signals from index bars) ---
    "opt_orb": {
        "enabled": True,
        "entry_deadline": "11:00",
        "min_or_range_pct": 0.15,
        "premium_stop_pct": 35.0,     # stop on the option premium
        "target_r": 2.0,
        "max_trades_per_direction": 1,
    },
    "opt_trend_day": {
        "enabled": True,
        "entry_start": "11:00",
        "entry_end": "13:30",
        "min_day_change_pct": 0.5,    # index move to call it a trend day
        "range_pos": 0.7,             # index closing near its extreme
        "premium_stop_pct": 40.0,
        "exit_time": "15:00",
        "max_trades_per_day": 1,
    },
    "opt_straddle": {
        "enabled": True,
        "entry_time": "09:25",        # sell ATM CE+PE after the first 5m bars
        "entry_latest": "09:45",
        "leg_stop_pct": 30.0,         # per-leg premium stop-loss
        "exit_time": "15:00",
        "max_trades_per_day": 1,
    },
    # Backtested 4-17% win rate (costs dwarf the edge) — ON now only to gather
    # forward paper-test data alongside the others. Not a profit expectation.
    "rsi2_scalp": {
        "enabled": True,
        "entry_start": "09:45",
        "entry_end": "14:30",
        "rsi_period": 2,
        "long_below": 5.0,
        "short_above": 95.0,
        "exit_rsi_long": 60.0,
        "exit_rsi_short": 40.0,
        "take_profit_pct": 0.7,
        "stop_pct": 0.5,
        "time_stop_min": 45,
        "max_trades_per_day": 1,
    },
}

# ---------------------------------------------------------------------------
# Index options (NIFTY / BANKNIFTY). Contracts, lot sizes, and expiries are
# read from the Fyers NSE_FO symbol master — never hardcoded.
# ---------------------------------------------------------------------------
OPTIONS = {
    "underlyings": ["NIFTY", "BANKNIFTY"],
    "master_url": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "master_cache_hours": 12,
    "n_strikes_each_side": 6,         # chain subscribed around ATM
    "paper_capital": 350_000.0,       # options paper book (mode PAPER-OPT, own ledger)
                                      # sized so a 2-leg short straddle (~2.8L margin)
                                      # fits under the 90% margin cap
    # Approximate SPAN+exposure margin per SHORT lot (paper simulation only;
    # real margins vary daily — verified against broker before any live use).
    "short_margin_per_lot": {"NIFTY": 140_000.0, "BANKNIFTY": 120_000.0},
}
OPTION_COSTS = {
    "brokerage_flat": 20.0,           # Fyers per executed order
    "stt_sell_pct": 0.15,             # of premium, sell side (hiked Apr 2026)
    "exchange_txn_pct": 0.035,        # NSE, of premium, both sides
    "sebi_pct": 0.0001,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,
}
OPTION_SLIPPAGE_PCT = 0.25            # of premium per side (spreads are wider)

# ---------------------------------------------------------------------------
# Day-type classification thresholds (bot/daytype.py)
# ---------------------------------------------------------------------------
DAYTYPE = {
    "trend_min_change_pct": 0.9,      # |day change| to call it a trend day
    "trend_range_pos": 0.7,           # close in the top/bottom 30% of day range
    "trend_nifty_min_pct": 0.25,      # index must agree by this much
    "vwap_slope_bars": 4,
    "range_max_vs_avg": 0.7,          # day range vs 10d avg to call it a range day
    "range_max_change_pct": 0.35,
    "range_min_side_minutes": 45,     # time spent on BOTH sides of VWAP
}

# ---------------------------------------------------------------------------
# Liquidity / sanity gates applied to every signal
# ---------------------------------------------------------------------------
MIN_PRICE = 50.0                  # skip penny-ish names
MAX_PRICE = 100_000.0
MIN_AVG_1M_TURNOVER = 500_000.0   # rupees/minute, 10d average — keeps fills realistic
# Cost-awareness: round-trip costs run ~0.10% of notional, so stops tighter
# than this get eaten alive by fees regardless of hit rate.
MIN_STOP_DISTANCE_PCT = 0.35

# ---------------------------------------------------------------------------
# LIVE trading gates. The bot NEVER edits these. All must pass:
#   1. LIVE_TRADING_ENABLED = True                 (edit this file)
#   2. strategy in LIVE_STRATEGY_ALLOWLIST         (edit this file)
#   3. .env DHAN_LIVE_CONFIRM == LIVE_CONFIRM_STRING
#   4. run_live.py launched with --live
# ---------------------------------------------------------------------------
LIVE_TRADING_ENABLED = False
LIVE_STRATEGY_ALLOWLIST: set[str] = set()
LIVE_CONFIRM_STRING = "YES-I-UNDERSTAND-REAL-MONEY"
LIVE_CAPITAL = 25_000.0
LIVE_RISK_PER_TRADE_PCT = 0.25
LIVE_MAX_CONCURRENT_POSITIONS = 2

# Promotion-readiness criteria (paper stats, trailing window) shown by reports.
PROMOTION_CRITERIA = {
    "window_sessions": 30,
    "min_trades": 30,
    "min_profit_factor": 1.3,
    "min_expectancy_rs": 0.0,     # net per trade after costs
    "max_drawdown_pct": 5.0,
    "worst_day_pct": -1.5,
}

# ---------------------------------------------------------------------------
# Reporting / ops
# ---------------------------------------------------------------------------
LOG_KEEP_DAYS = 30
EQUITY_MARK_SECONDS = 60          # equity_log cadence during session
DASHBOARD_REFRESH_SECONDS = 2


def dhan_settings() -> dict:
    return {
        "client_id": os.getenv("DHAN_CLIENT_ID", "").strip(),
        "access_token": os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
        "live_confirm": os.getenv("DHAN_LIVE_CONFIRM", "").strip(),
    }


def fyers_settings() -> dict:
    return {
        "app_id": os.getenv("FYERS_APP_ID", "").strip(),          # e.g. AB12345-100
        "secret_id": os.getenv("FYERS_SECRET_ID", "").strip(),
        "redirect_uri": os.getenv("FYERS_REDIRECT_URI",
                                  "https://trade.fyers.in/api-login/redirect-uri/index.html").strip(),
        "pin": os.getenv("FYERS_PIN", "").strip(),
    }


def live_confirm() -> str:
    """Broker-agnostic live confirmation string (gate 2)."""
    return (os.getenv("LIVE_CONFIRM", "") or os.getenv("DHAN_LIVE_CONFIRM", "")).strip()


def discord_settings() -> dict:
    return {
        "webhook_url": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        "bot_token": os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        "channel_id": os.getenv("DISCORD_CHANNEL_ID", "").strip(),
    }


# ---------------------------------------------------------------------------
# Self-improving discovered strategy fleet (bot/discovery/)
#   Strategies represented as data (a boolean entry_expr), gated by an
#   in-sample/out-of-sample backtest on cached 1m bars, run as extra variants
#   in two DISCOVERED channels. INTRADAY-only; all paper.
# ---------------------------------------------------------------------------
DISCOVERY_ENABLED = True
MAX_DISCOVERED_PICKS_PER_DAY = 20      # per-channel entry budget within a day
DISCOVERED_FLEET_MAX = 40              # active specs per channel (bounds fleet size)
DISCOVERED_EQ_MAX_SYMBOLS = 6          # stocks a gate replay samples (speed)
DISCOVERED_ENTRY_DEADLINE = "13:30"    # no discovered entries after this (square-off room)

# Intraday target/stop derivation reused by every discovered spec.
DISCOVERED_EQ_STOP_PCT = 0.5           # equity: stop this % from entry
DISCOVERED_OPT_PREMIUM_STOP_PCT = 35.0 # option: premium stop, like opt_orb

# Backtest gate — the overfitting defense. The OUT-OF-SAMPLE window is the test.
BACKTEST_GATE = {
    "gate_sessions": 60,        # sessions of recent history the daily gate replays
    "oos_fraction": 0.35,       # most-recent 35% of sessions = out-of-sample
    "min_oos_trades": 8,        # OOS must produce at least this many trades
    "min_win_rate": 45.0,       # percent, OOS
    "min_profit_factor": 1.2,   # OOS
    "eq_stop_pct": 0.5,         # equity replay stop distance (%)
    "opt_stop_pct": 0.25,       # index-move stop for the OPT proxy (%)
    "friction_pct": 0.12,       # equity round-trip drag charged per replay trade
    "opt_friction_pct": 1.0,    # option round-trip is far heavier on premium
    "hold_bars_max": 12,        # 5m bars max hold before a time-exit (intraday)
    "entry_deadline": "13:30",  # replay stops taking entries after this
}
# A live spec is retired once it has a real forward-paper track record that is
# net-negative (the gate can be fooled; the live ledger can't).
DISCOVERED_RETIRE_MIN_TRADES = 15
DISCOVERED_RETIRE_MODES = ("PAPER", "PAPER-OPT", "BACKTEST")
