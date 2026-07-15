# stocks-intraday — Real-Time NSE Intraday Trading Bot

Event-driven intraday bot for **Nifty 50 + Bank Nifty constituent stocks**.
Six competing strategies paper-trade a ₹1,00,000 virtual book on 1-minute data;
strategies that prove themselves are **manually** promoted to small live orders
through Dhan. The same strategy code runs unchanged in backtest, replay, paper,
and live — only the feed and broker are swapped.

## Quick start

```powershell
.venv\Scripts\Activate.ps1            # deps already installed
pytest -q                              # 75 tests should pass

# 1) Backfill recent history + backtest (works immediately, no accounts needed)
python run_backtest.py --fetch --from 2026-06-22 --to 2026-07-08

# 2) Off-hours dry run of the full live pipeline from cached bars
python run_live.py --replay 2026-07-08

# 3) Real paper session during market hours (09:15-15:30 IST)
python run_live.py                     # auto feed: Dhan if token present, else yfinance

# 4) Reports / dashboards
python run_report.py                   # EOD + promotion-readiness table
scripts\run_dashboard.ps1              # web dashboard on :8503 (or launch it from the 8787 hub)

# 5) Auto-start every weekday at 08:55 IST
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

## Data feeds

| Feed | Cost | Latency | Setup |
|------|------|---------|-------|
| `yfinance` (default) | free | ~1–2 min | none |
| Dhan websocket | ₹499+GST/mo (Data API) | real-time ticks | `DHAN_CLIENT_ID` + `DHAN_ACCESS_TOKEN` in `.env` |

Dhan access tokens expire every **24 h** (regenerate on web.dhan.co). If the
websocket dies mid-session the bot degrades to yfinance automatically and keeps
trading — it never crashes the session.

### Fyers login reminder — SEPARATE from the other bots

This bot has its **own** Fyers token (`data/cache/fyers_tokens.json`) and its own
login: `python -m bot.fyers_auth` (run each trading morning). When
`ensure_access_token()` finds no fresh token it posts a Discord nudge via
`alerts.send_login_reminder()`, which is **session-gated** (only on a trading-day
session, never nights/weekends/holidays) and **throttled to one post per hour**.

⚠️ This throttle is **intentionally NOT shared** with stockbot/mcxbot. Those two
share one token and coordinate a single throttle file; this bot's login is
distinct, so its nudge must stay on its own throttle
(`data/.alert_login_reminder`). Do **not** point it at their shared file — that
would let their reminder suppress this bot's "run `bot.fyers_auth`" nudge and you'd
never learn intraday needs its own login. (Previously this nudge was unthrottled
and fired once per calling subsystem — feed/broker/history/options — so a single
stale token produced several identical pings within a minute.)

## Strategies (params in `config.STRATEGY_PARAMS`)

| Name | Idea | Stop / Target |
|------|------|---------------|
| `orb` | 15-min opening-range breakout w/ volume | OR midpoint / 2R |
| `vwap_reversion` | fade closes beyond VWAP±2σ on range days | 1σ / back to VWAP |
| `vwap_pullback` | trend day, buy pullback that holds VWAP | under pullback / 2R, BE at +1R |
| `momentum_breakout` | prev-day-high/low break w/ RVOL≥2 | bar low / 2R, EMA20 trail |
| `gap` | gap-and-go continuation or gap-fill fade | first-5m bar / 2R or prev close |
| `rsi2_scalp` | RSI(2) extreme, with-VWAP-trend scalp | 0.4% / +0.5% or RSI recovery |

All strategies trade long **and** short, signal on 5-minute bar closes, fill at
the next 1-minute open with slippage, and pay the full Indian MIS cost stack
(brokerage, STT, exchange, SEBI, stamp, GST).

## Risk engine (`config.py`)

- 0.5% of equity risked per trade; positions capped at 60% notional / 5× MIS margin
- **2% max daily loss** → everything squared off, no more entries that day
- Max 6 concurrent positions, 3 per strategy, 1 per symbol, 10 trades/day/strategy
- 3 consecutive losses benches a strategy for the day
- Index circuit breaker: NIFTY/BANKNIFTY ±1% in 15 min (or ±2.5% from open) pauses entries
- No entries before 09:20 or after 14:45; forced square-off 15:12
- Every rejected signal is logged to `skips` with the reason

## Going live (never automatic)

Watch `python run_report.py` until a strategy shows **READY** (≥30 trades,
PF ≥1.3, positive expectancy after costs, max DD ≤5%, worst day ≥−1.5% over the
trailing 30 sessions). Then, and only then, open all four gates by hand:

1. `config.py` → `LIVE_TRADING_ENABLED = True`
2. `config.py` → `LIVE_STRATEGY_ALLOWLIST = {"orb"}` (the proven strategy)
3. `.env` → `DHAN_LIVE_CONFIRM=YES-I-UNDERSTAND-REAL-MONEY`
4. launch with `python run_live.py --live`

Live orders mirror the paper decisions at reduced size (`LIVE_CAPITAL`,
0.25% risk/trade, max 2 live positions). The paper book remains the book of
record; every real order + raw broker response lands in the `orders` table.
Any missing gate silently keeps you in paper. **This is real money — start with
one strategy and the smallest size.**

## Layout

```
config.py            every tunable (single source of truth)
run_live.py          paper/live session   run_backtest.py   history replay
run_report.py        EOD + readiness      dashboard_web.py  Streamlit UI
bot/
  engine.py          the one event loop (backtest = paper = live)
  clock.py           IST phases + NSE holiday calendar
  bars.py            tick→1m→5m aggregation   indicators.py  incremental VWAP/RSI/ATR/RVOL
  state.py           SymbolState/MarketState  risk.py        sizing + limits + halts
  costs.py           MIS cost model           history.py     1m backfill + prev-day levels
  instruments.py     universe + Dhan scrip master
  feeds/             yf_feed, dhan_feed (ws), replay_feed
  strategies/        the six strategies
  execution/         paper_broker, dhan_broker (hard-gated live mirror)
  reports.py / dashboard.py / alerts.py / db.py (only SQLite module)
data/bot.db          all state (bars cache, trades ledger, equity, skips)
```

> Suggestions only becomes real money only through the gates above.
> Markets can and will take money from any strategy — respect the risk limits.
