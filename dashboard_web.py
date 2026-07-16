"""Read-only web dashboard for the intraday NSE-equities bot — mirrors the MCX
bot's layout: TWO top-level views in the sidebar, each with its own neat tab bar
over the same SQLite state (data/bot.db).

  🟢 Live  — REAL-money trading readiness & broker gate. Live routing is OFF
             until a strategy graduates off the paper book. Tabs: Readiness,
             Broker & Gate.
  📝 Paper — the paper book (equity PAPER + index-options PAPER-OPT). Tabs:
               • Summary    — realized & unrealized P&L headline + engine status.
               • Open Book  — every open position with margin, current price, P&L.
               • Closed Book— realized trades + per-strategy/-symbol attribution.
               • Fleet      — every strategy variant (classic + discovered + bred).
               • History    — equity curve, daily P&L, run log.
               • Backtest   — replay cached 1m bars through the engine on demand.
               • Feed & Status — engine heartbeat, feed provenance, skips.

Run:  .venv\\Scripts\\python.exe -m streamlit run dashboard_web.py   (http://localhost:8503)

Read-only: opens the DB in mode=ro so it can never write to or lock the file the
live engine is using — purely a viewer. Figures are "as of the last mark".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

import config

st.set_page_config(page_title="Intraday Bot", page_icon="📈", layout="wide")

# Validated categorical palette (fixed slot per strategy — never re-ranked).
STRATEGY_COLORS = {
    "orb": "#2a78d6",
    "vwap_reversion": "#1baf7a",
    "vwap_pullback": "#eda100",
    "momentum_breakout": "#4a3aa7",
    "gap": "#e87ba4",
    "rsi2_scalp": "#eb6834",
    "trend_day": "#256abf",
    "range_fade": "#199e70",
    "opt_orb": "#104281",
    "opt_trend_day": "#c98500",
    "opt_straddle": "#9085e9",
}
GOOD, BAD = "#008300", "#e34948"      # reserved P&L polarity colors
EQUITY_BLUE = "#2a78d6"

# Starting capital per underlying DB mode (used to turn the ledger into equity).
START_CASH = {
    "PAPER": config.PAPER_STARTING_CASH,
    "PAPER-OPT": config.OPTIONS["paper_capital"],
    "LIVE": config.LIVE_CAPITAL,
    "REPLAY": config.PAPER_STARTING_CASH,
    "BACKTEST": config.PAPER_STARTING_CASH,
}


@st.cache_resource
def get_conn():
    """Read-only connection — never writes to or locks the live DB."""
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True,
                               check_same_thread=False)
    except sqlite3.OperationalError:
        # DB not created yet (no run has happened) — a normal connection lets the
        # viewer render empty rather than crash. Still read-only in practice.
        conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, get_conn(), params=params)
    except Exception:
        return pd.DataFrame()


def inr(x: float) -> str:
    return f"₹{x:,.0f}"


def heartbeat() -> dict | None:
    raw = q("SELECT value FROM kv WHERE key='engine_heartbeat'")
    if raw.empty:
        return None
    try:
        return json.loads(raw.iloc[0, 0])
    except Exception:
        return None


def latest_marks(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    df = q(f"SELECT symbol, close FROM bars_1m b WHERE symbol IN ({ph}) "
           f"AND ts = (SELECT MAX(ts) FROM bars_1m WHERE symbol = b.symbol)",
           tuple(symbols))
    return dict(zip(df["symbol"], df["close"])) if not df.empty else {}


# ------------------------------------------------------- sidebar nav (2 views)
# Mirrors the MCX dashboard: a two-option sidebar radio, each view owning its
# own tab bar. Paper spans both paper books (equity + index-options).

MODE_OPTIONS = {
    "🟢 Live": ["LIVE"],
    "📝 Paper": ["PAPER", "PAPER-OPT"],
}

st.sidebar.title("📈 Intraday Bot")
st.sidebar.caption("Nifty 50 + Bank Nifty · live-tick paper trading")
view = st.sidebar.radio("View", list(MODE_OPTIONS), index=1,
                        label_visibility="collapsed")
if st.sidebar.button("🔄 Refresh data"):
    st.rerun()

db_modes = MODE_OPTIONS[view]              # underlying DB modes this view covers
multi = len(db_modes) > 1                  # spans >1 book → show the mode column
start_cash = sum(START_CASH.get(m, 0.0) for m in db_modes)

_hb = heartbeat()
if _hb:
    beat = _hb.get("wall_ts", "")[:16]
    st.sidebar.caption(
        f"**Engine** {_hb.get('mode')} · phase {_hb.get('phase')}\n\n"
        f"feed `{_hb.get('feed')}` · equity {inr(_hb.get('equity', 0))} "
        f"({_hb.get('day_pnl', 0):+,.0f})\n\nlast beat {beat}")
else:
    st.sidebar.caption("No engine heartbeat — start a session with run_live.py.")
st.sidebar.caption("Real orders "
                   f"{'ON ⚠️' if config.LIVE_TRADING_ENABLED else 'OFF'} · read-only viewer.")


def mode_where(col: str = "mode") -> tuple[str, tuple]:
    """WHERE fragment + params matching the selected view's books."""
    ph = ",".join("?" * len(db_modes))
    return f"{col} IN ({ph})", tuple(db_modes)


MW, MP = mode_where()
trades_all = q(f"SELECT * FROM trades WHERE {MW} ORDER BY exit_ts", MP)
realized_total = trades_all["net_pnl"].sum() if not trades_all.empty else 0.0

TRADE_COLS = ["exit_ts", "mode", "strategy", "symbol", "side", "qty",
              "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl",
              "r_multiple", "exit_reason"]


def trade_view(df: pd.DataFrame, cols: list[str] = TRADE_COLS) -> pd.DataFrame:
    keep = [c for c in cols if c in df.columns and (multi or c != "mode")]
    return df[keep]


def open_positions() -> pd.DataFrame:
    return q(f"SELECT * FROM positions WHERE status='OPEN' AND {MW} "
             "ORDER BY entry_ts", MP)


def position_rows(pos: pd.DataFrame, marks: dict[str, float]) -> tuple[list, float]:
    """Detailed open-position rows (entry, current price, current P&L, margin,
    risk, distances) + total unrealized."""
    unrealized, rows = 0.0, []
    for _, p in pos.iterrows():
        ltp = marks.get(p["symbol"], p["entry_price"])
        sign = 1 if p["side"] == "LONG" else -1
        upnl = (ltp - p["entry_price"]) * p["qty"] * sign
        unrealized += upnl
        risk = abs(p["entry_price"] - p["stop_price"]) * p["qty"]
        tgt = p["target_price"]
        rows.append({
            "Mode": p["mode"], "Strategy": p["strategy"], "Symbol": p["symbol"],
            "Side": p["side"], "Qty": int(p["qty"]), "Entry": p["entry_price"],
            "Current": round(ltp, 2), "Current P&L ₹": round(upnl),
            "Margin ₹": round(p["margin_used"]),
            "Stop": p["stop_price"], "Target": tgt, "Risk ₹": round(risk),
            "To stop %": round((ltp - p["stop_price"]) / ltp * 100 * sign, 2)
                         if ltp else None,
            "To target %": round((tgt - ltp) / ltp * 100 * sign, 2)
                           if tgt and ltp else None,
            "Since": p["entry_ts"][11:16],
        })
    if not multi:
        for r in rows:
            r.pop("Mode")
    return rows, unrealized


# ===========================================================================
# 📝 PAPER — Summary / Open Book / Closed Book / Fleet / History / Backtest
# ===========================================================================

def render_engine_status() -> None:
    hb = heartbeat()
    if hb is None:
        st.info("No engine heartbeat yet — the status panel activates once a "
                "session runs (run_live.py).")
        return
    stale = hb.get("wall_ts", "")[:16]
    running = f"**{hb.get('mode')}** · phase **{hb.get('phase')}** · feed `{hb.get('feed')}`"
    if hb.get("halted"):
        running += f" · ⛔ HALTED: {hb.get('halt_reason')}"
    st.markdown(f"⚙️ {running} — last beat {stale}")
    feed = str(hb.get("feed", "")).lower()
    if "degraded" in feed or (feed and not feed.startswith(("fyers-ws", "replay"))):
        st.warning("🧊 Feed is not the real Fyers feed — under the Fyers-only "
                   "policy the paper book is FROZEN (no open/close/mark) on a "
                   "fallback/degraded feed. Scanning & logging continue.")
    cols = st.columns(4)
    cols[0].metric("Engine equity", inr(hb.get("equity", 0)))
    cols[1].metric("Day P&L", inr(hb.get("day_pnl", 0)))
    cols[2].metric("Entry budget used",
                   f"{hb.get('entries_today', 0)}/{hb.get('entries_budget', 0)}")
    cols[3].metric("Trades today", hb.get("trades_today", 0))
    strategies = hb.get("strategies", [])
    benched = set(hb.get("benched", []))
    chips = "  ".join(
        f"🔴 ~~{s}~~ (benched)" if s in benched else f"🟢 {s}" for s in strategies)
    st.markdown(f"**Running strategies:** {chips or '—'}")


def _fyers_connection() -> tuple[bool, str]:
    """Read-only Fyers status for the Summary banner. Green when today's login
    exists (token saved_at == today, mirroring fyers_auth.ensure_access_token)
    AND — if a session is actually live (fresh heartbeat) — its feed is the real
    fyers-ws/replay feed. Red when a running session is on a degraded/fallback
    feed (book FROZEN) or when there is no fresh token. A STALE heartbeat (dead
    session) is ignored so an old yfinance beat doesn't read as a live freeze.
    Pure display; reads the token file + heartbeat only, changes no logic."""
    from datetime import datetime

    try:
        tok = json.loads(config.FYERS_TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:
        tok = {}
    today = date.today().isoformat()
    logged_in = bool(tok.get("access_token")) and str(tok.get("saved_at", ""))[:10] == today

    feed = ""
    hb = heartbeat()
    if hb:
        try:
            beat = datetime.fromisoformat(str(hb.get("wall_ts", "")))
            if (datetime.now(beat.tzinfo) - beat).total_seconds() < 180:
                feed = str(hb.get("feed", "")).lower()   # only trust a FRESH beat
        except Exception:
            feed = ""
    live_real = feed.startswith(("fyers-ws", "replay"))
    live_degraded = bool(feed) and not live_real

    if live_degraded:
        return False, f"live feed is {hb.get('feed')} (fallback) — book FROZEN"
    if logged_in:
        if live_real:
            return True, f"live feed {hb.get('feed')}"
        return True, f"today's token present (saved {str(tok.get('saved_at', ''))[:16]})"
    return False, "no fresh token today"


def _fyers_banner() -> None:
    ok, detail = _fyers_connection()
    if ok:
        st.success(f"🟢 **Connected to Fyers** — {detail}.")
    else:
        st.error(f"🔴 **Not connected to Fyers** — {detail}. Run the daily Fyers "
                 "login (before the market session) so the engine trades on the "
                 "real feed; the paper book is frozen otherwise.")


def render_summary() -> None:
    today = date.today().isoformat()
    _fyers_banner()
    render_engine_status()
    st.divider()

    pos = open_positions()
    marks = latest_marks(pos["symbol"].tolist() if not pos.empty else [])
    rows, unrealized = position_rows(pos, marks)

    trades_today = trades_all[trades_all["exit_ts"].str[:10] == today] \
        if not trades_all.empty else pd.DataFrame()
    realized_today = trades_today["net_pnl"].sum() if not trades_today.empty else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity (marked)", inr(start_cash + realized_total + unrealized),
              f"{(realized_total + unrealized) / start_cash * 100:+.2f}%"
              if start_cash else None)
    c2.metric("Unrealized (open)", inr(unrealized))
    c3.metric("Realized today", inr(realized_today))
    c4.metric("Open positions", len(rows))
    c5.metric("Realized all-time", inr(realized_total))
    st.caption(f"Starting capital {inr(start_cash)} · equity = starting + closed "
               "ledger + open unrealized. The closed-trade ledger is the source "
               "of truth.")

    st.subheader("Open positions at a glance")
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption("Full margin / distances are in the **Open Book** tab.")
    else:
        st.caption("Flat — no open positions.")

    st.subheader(f"Today's closed trades ({today})")
    if trades_today.empty:
        st.caption("No trades closed today.")
    else:
        st.dataframe(trade_view(trades_today), width="stretch", hide_index=True)

    eq = q(f"SELECT ts, SUM(equity) AS equity FROM equity_log WHERE {MW} "
           "AND substr(ts,1,10)=? GROUP BY ts ORDER BY ts", MP + (today,))
    if not eq.empty:
        st.subheader("Session equity")
        eq["ts"] = pd.to_datetime(eq["ts"])
        st.line_chart(eq.set_index("ts")["equity"], color=EQUITY_BLUE)


def render_open_book() -> None:
    st.subheader("Open Book — live positions")
    st.caption("Every open paper position with margin blocked, entry, current "
               "price and current unrealized P&L (marked on the latest cached tick).")
    pos = open_positions()
    if pos.empty:
        st.info("Flat — no open paper positions.")
        return
    marks = latest_marks(pos["symbol"].tolist())
    rows, unrealized = position_rows(pos, marks)
    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)

    total_margin = float(pos["margin_used"].sum())
    m1, m2, m3 = st.columns(3)
    m1.metric("Total margin blocked", inr(total_margin))
    m2.metric("Total current P&L", inr(unrealized))
    m3.metric("Open positions", len(df))
    st.caption("Distances are signed in the trade's favor; risk uses the planned stop.")


def render_closed_book() -> None:
    st.subheader("Closed Book — realized trades + attribution")
    if trades_all.empty:
        st.info("No closed trades recorded yet in this view.")
        return
    t = trades_all.copy()
    n = len(t)
    wins = int((t["net_pnl"] > 0).sum())
    total = float(t["net_pnl"].sum())
    avg_win = t.loc[t["net_pnl"] > 0, "net_pnl"].mean()
    avg_loss = t.loc[t["net_pnl"] <= 0, "net_pnl"].mean()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed trades", f"{n}", f"{wins / n * 100:.0f}% win" if n else None)
    c2.metric("Total realized P&L", inr(total))
    c3.metric("Avg win", inr(avg_win) if wins else "-")
    c4.metric("Avg loss", inr(avg_loss) if n - wins else "-")

    st.dataframe(
        trade_view(t.sort_values("exit_ts", ascending=False),
                   ["exit_ts", "mode", "strategy", "symbol", "side", "qty",
                    "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl",
                    "r_multiple", "exit_reason"]),
        width="stretch", hide_index=True, height=340)

    st.subheader("Attribution — per strategy")
    keys = ["mode", "strategy"] if multi else ["strategy"]
    g = (t.assign(win=(t["net_pnl"] > 0).astype(int)).groupby(keys)
         .agg(Trades=("net_pnl", "count"), Wins=("win", "sum"),
              Gross=("gross_pnl", "sum"), Costs=("costs", "sum"),
              Net=("net_pnl", "sum")).reset_index())
    g["Win %"] = (g["Wins"] / g["Trades"] * 100).round(0)
    g = g.sort_values("Net", ascending=False)
    st.dataframe(g, width="stretch", hide_index=True)
    st.bar_chart(g.set_index("strategy")["Net"])

    st.subheader("Attribution — per symbol")
    agg = (t.assign(win=(t["net_pnl"] > 0).astype(int)).groupby("symbol")
           .agg(Trades=("net_pnl", "count"), Wins=("win", "sum"),
                Net=("net_pnl", "sum")).reset_index())
    agg["Win %"] = (agg["Wins"] / agg["Trades"] * 100).round(1)
    agg = agg.sort_values("Net", ascending=False)
    st.dataframe(agg[["symbol", "Trades", "Win %", "Net"]],
                 width="stretch", hide_index=True)

    st.subheader("Exit-reason breakdown")
    reasons = (t["exit_reason"].str.split(":").str[0].value_counts()
               .rename_axis("reason").reset_index(name="n"))
    if not reasons.empty:
        st.bar_chart(reasons.set_index("reason")["n"])

    st.subheader("Trade drill-down")
    ids = t.sort_values("exit_ts", ascending=False)["id"].tolist()
    lbl = {r["id"]: (f"#{r['id']} · {r['exit_ts'][:16]} · {r['strategy']} · "
                     f"{r['symbol']} {r['side']} · net {inr(r['net_pnl'])}")
           for _, r in t.iterrows()}
    sel_id = st.selectbox("Trade", ids, format_func=lambda i: lbl[i], key="cb_drill")
    trade_drilldown(t[t["id"] == sel_id].iloc[0])


def trade_drilldown(tr: pd.Series) -> None:
    entry_t = pd.to_datetime(tr["entry_ts"])
    exit_t = pd.to_datetime(tr["exit_ts"])
    hold_min = max(0, int((exit_t - entry_t).total_seconds() // 60))

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Net P&L", inr(tr["net_pnl"]))
    c2.metric("Gross", inr(tr["gross_pnl"]))
    c3.metric("Costs", inr(tr["costs"]))
    c4.metric("R multiple", f"{tr['r_multiple']:.2f}" if pd.notna(tr["r_multiple"]) else "—")
    c5.metric("Held", f"{hold_min} min")
    c6.metric("Exit reason", tr["exit_reason"])

    day = tr["entry_ts"][:10]
    bars = q("SELECT ts, close FROM bars_1m WHERE symbol=? AND substr(ts,1,10)=? "
             "ORDER BY ts", (tr["symbol"], day))
    if bars.empty:
        st.caption("No 1-minute bars cached for this symbol/day — no chart.")
        return
    bars["ts"] = pd.to_datetime(bars["ts"])
    pnl_color = GOOD if tr["net_pnl"] >= 0 else BAD
    price = (alt.Chart(bars).mark_line(strokeWidth=2, color=EQUITY_BLUE)
             .encode(x=alt.X("ts:T", title=None),
                     y=alt.Y("close:Q", title="Price ₹", scale=alt.Scale(zero=False))))
    marker_df = pd.DataFrame([
        {"ts": entry_t, "price": tr["entry_price"], "label": "Entry", "color": EQUITY_BLUE},
        {"ts": exit_t, "price": tr["exit_price"], "label": "Exit", "color": pnl_color},
    ])
    points = (alt.Chart(marker_df)
              .mark_point(size=140, filled=True, stroke="white", strokeWidth=2)
              .encode(x="ts:T", y="price:Q", color=alt.Color("color:N", scale=None),
                      tooltip=[alt.Tooltip("label:N", title="Event"),
                               alt.Tooltip("price:Q", title="Price", format=",.2f")]))
    labels = (alt.Chart(marker_df).mark_text(dy=-14, fontWeight="bold")
              .encode(x="ts:T", y="price:Q", text="label:N"))
    layers = [price, points, labels]
    levels = []
    if pd.notna(tr.get("planned_stop")):
        levels.append({"price": tr["planned_stop"], "label": "Stop", "color": BAD})
    if pd.notna(tr.get("planned_target")):
        levels.append({"price": tr["planned_target"], "label": "Target", "color": GOOD})
    if levels:
        lv = pd.DataFrame(levels)
        layers.append(alt.Chart(lv).mark_rule(strokeDash=[5, 4], strokeWidth=1.5)
                      .encode(y="price:Q", color=alt.Color("color:N", scale=None)))
    st.altair_chart(alt.layer(*layers).properties(height=360).interactive(bind_y=False),
                    width="stretch")


def render_history() -> None:
    st.caption("Everything over time — equity curve, daily P&L, and the run log.")
    if not trades_all.empty:
        t = trades_all.copy()
        t["day"] = t["exit_ts"].str[:10]
        daily = t.groupby("day")["net_pnl"].sum().reset_index()
        daily["equity"] = start_cash + daily["net_pnl"].cumsum()

        c1, c2, c3, c4 = st.columns(4)
        wins = (t["net_pnl"] > 0).sum()
        gross_win = t.loc[t["net_pnl"] > 0, "net_pnl"].sum()
        gross_loss = -t.loc[t["net_pnl"] <= 0, "net_pnl"].sum()
        c1.metric("Net P&L (all time)", inr(realized_total))
        c2.metric("Trades / Win rate", f"{len(t)} / {wins / len(t) * 100:.0f}%")
        c3.metric("Profit factor", f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "∞")
        c4.metric("Total costs paid", inr(t["costs"].sum()))

        st.subheader("Equity curve (closed-trade ledger)")
        st.line_chart(daily.set_index("day")["equity"], color=EQUITY_BLUE)

        st.subheader("Daily net P&L")
        dd = daily.set_index("day")[["net_pnl"]]
        dd["profit"] = dd["net_pnl"].clip(lower=0)
        dd["loss"] = dd["net_pnl"].clip(upper=0)
        st.bar_chart(dd[["profit", "loss"]], color=[GOOD, BAD])

        st.subheader("Cumulative net by strategy")
        pivot = (t.pivot_table(index="day", columns="strategy", values="net_pnl",
                               aggfunc="sum").fillna(0).cumsum())
        colors = [STRATEGY_COLORS.get(c, "#52514e") for c in pivot.columns]
        st.line_chart(pivot, color=colors)
    else:
        st.caption("No closed trades yet — the equity curve appears as trades close.")

    st.subheader("Run log")
    runs = q(f"SELECT id, mode, session_date, feed_source, started_at, finished_at, "
             f"bars_processed, signals, trades FROM runs WHERE {MW} "
             "ORDER BY id DESC LIMIT 200", MP)
    if runs.empty:
        st.caption("No runs logged yet.")
    else:
        st.dataframe(runs, width="stretch", hide_index=True, height=280)


def render_backtest() -> None:
    from bot.backtest import PERIODS

    st.caption("Replays cached 1m bars through the real engine (bot/backtest.py). "
               "Backfills come from Fyers /history (the authorized source; a fetch "
               "fails loud if Fyers is unavailable rather than silently using "
               "yfinance). Read-only — results are held in this session.")
    with st.form("bt_form"):
        col = st.columns(4)
        period = col[0].selectbox("Period", list(PERIODS), index=1)
        max_instr = col[1].number_input("Max instruments", 1, 60, 15)
        capital = col[2].number_input("Capital ₹", 100_000, 50_000_000,
                                      int(config.PAPER_STARTING_CASH), step=100_000)
        seeds_only = col[3].checkbox("Seeds only", value=False,
                                     help="backtest only the SEED_GENES library (equity)")
        submitted = st.form_submit_button("▶️ Run backtest")

    if submitted:
        from bot.backtest import run_and_save
        with st.spinner(f"Replaying {period}…"):
            try:
                _, summary = run_and_save(period=period, max_instruments=int(max_instr),
                                          starting_cash=float(capital), seeds_only=seeds_only)
                st.session_state["bt_last"] = {"period": period, "summary": summary,
                                               "seeds_only": seeds_only}
            except Exception as exc:  # noqa: BLE001
                st.error(f"Backtest failed: {exc}")

    last = st.session_state.get("bt_last")
    if last:
        s = last["summary"]
        if "error" in s:
            st.warning(s["error"])
        else:
            st.caption(f"Last run · {last['period']}"
                       + (" · seeds only" if last["seeds_only"] else ""))
            m = st.columns(5)
            m[0].metric("Sessions", s["sessions"])
            m[1].metric("Trades", f"{s['trades']:,}")
            m[2].metric("Net P&L", inr(s["total_net"]))
            m[3].metric("Max DD", f"{s['max_dd_pct']:.1f}%")
            pf = s["profit_factor"]
            m[4].metric("Profit factor",
                        "∞" if pf == float("inf") else (f"{pf:.2f}" if pf else "—"))

    st.divider()
    st.subheader("Persisted backtest / replay runs")
    bt_runs = q("SELECT r.id, r.mode, r.session_date, r.feed_source, r.started_at, "
                "r.bars_processed, r.trades, COALESCE(t.net, 0) AS net_pnl "
                "FROM runs r LEFT JOIN (SELECT run_id, SUM(net_pnl) AS net FROM trades "
                "GROUP BY run_id) t ON t.run_id = r.id "
                "WHERE r.mode IN ('BACKTEST','REPLAY') ORDER BY r.id DESC LIMIT 200")
    if bt_runs.empty:
        st.caption("No persisted BACKTEST/REPLAY runs — run "
                   "`run_backtest.py --persist` to record one.")
    else:
        st.dataframe(bt_runs, width="stretch", hide_index=True, height=240)


# ------------------------------------------------------------ Fleet analysis

def _variant_ledger() -> pd.DataFrame:
    led = q(
        f"SELECT variant_key, strategy, COUNT(*) AS trades, SUM(net_pnl) AS net, "
        f"SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        f"SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END) AS gains, "
        f"-SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END) AS losses "
        f"FROM trades WHERE {MW} GROUP BY variant_key, strategy", MP)
    specs = q("SELECT name AS variant_key, channel, entry_expr, source, status, "
              "created_at FROM discovered_specs")
    if led.empty:
        led = pd.DataFrame(columns=["variant_key", "strategy", "trades", "net",
                                    "wins", "gains", "losses"])
    merged = led.merge(specs, on="variant_key", how="outer")
    merged["trades"] = merged["trades"].fillna(0).astype(int)
    for c in ("net", "wins", "gains", "losses"):
        merged[c] = merged[c].fillna(0.0)
    merged["channel"] = merged["channel"].fillna(merged["strategy"]).fillna("—")
    merged["entry_expr"] = merged["entry_expr"].fillna("—")
    merged["source"] = merged["source"].fillna("classic")
    merged["status"] = merged["status"].fillna("ACTIVE")
    merged["win%"] = (merged["wins"] / merged["trades"].replace(0, pd.NA) * 100).fillna(0)
    merged["PF"] = (merged["gains"] / merged["losses"].replace(0, pd.NA))
    return merged


def render_fleet() -> None:
    st.caption("Every strategy variant — classic + discovered + bred — with its "
               "own track record. Discovered specs are DATA (a boolean entry_expr) "
               "run through a whitelist-only interpreter; all trading is paper.")
    v = _variant_ledger()
    active_specs = q("SELECT channel, status, source FROM discovered_specs")
    n_active_disc = int((active_specs["status"] == "ACTIVE").sum()) if not active_specs.empty else 0
    traded = v[v["trades"] > 0]
    total_trades = int(traded["trades"].sum())
    fleet_wr = float(traded["wins"].sum()) / total_trades * 100 if total_trades else 0.0
    realized = float(traded["net"].sum())
    disc = traded[traded["source"].isin(["discovered", "mixer"])]
    graduates = int(((disc["trades"] >= 15) & (disc["net"] > 0)).sum()) if not disc.empty else 0

    c = st.columns(6)
    c[0].metric("Active variants", int((v["status"] == "ACTIVE").sum()))
    c[1].metric("Discovered (active)", n_active_disc)
    c[2].metric("Closed trades", f"{total_trades:,}")
    c[3].metric("Fleet win-rate", f"{fleet_wr:.0f}%")
    c[4].metric("Realized P&L", inr(realized))
    c[5].metric("Graduates", graduates,
                help="discovered/bred specs net-positive over ≥15 forward trades")

    st.subheader("Per-channel breakdown")
    if not traded.empty:
        chan = traded.groupby("channel").agg(
            variants=("variant_key", "nunique"), trades=("trades", "sum"),
            net=("net", "sum"), wins=("wins", "sum")).reset_index()
        chan["win%"] = (chan["wins"] / chan["trades"] * 100).round(0)
        chan["net"] = chan["net"].round(0)
        st.dataframe(chan[["channel", "variants", "trades", "win%", "net"]],
                     width="stretch", hide_index=True)
        st.bar_chart(chan.set_index("channel")["net"])
    else:
        st.caption("No closed trades yet — variants build their ledgers as they trade.")

    st.subheader("All variants (classic + discovered + bred)")
    show = v.copy()
    show["net"] = pd.to_numeric(show["net"], errors="coerce").round(0)
    show["win%"] = pd.to_numeric(show["win%"], errors="coerce").round(0)
    # PF is pd.NA for variants with no losing trades — coerce so .round() is safe.
    show["PF"] = pd.to_numeric(show["PF"], errors="coerce").round(2)
    show = show.sort_values(["channel", "net"], ascending=[True, False])
    st.dataframe(
        show[["variant_key", "channel", "source", "status", "trades", "win%",
              "net", "PF", "entry_expr"]],
        width="stretch", hide_index=True, height=420,
        column_config={"entry_expr": st.column_config.TextColumn("entry_expr", width="large")})


# --------------------------------------------------------- Feed & Status tab

def render_feed_status() -> None:
    render_engine_status()
    st.divider()

    st.subheader("Cached bar provenance")
    st.caption("Where the 1-minute bars in the cache came from. Live ticks and "
               "Fyers /history backfills are tagged `fyers`; dev backfills `yf`/`dhan`.")
    prov = q("SELECT source, COUNT(*) AS bars, MIN(ts) AS first, MAX(ts) AS last "
             "FROM bars_1m GROUP BY source ORDER BY bars DESC")
    if prov.empty:
        st.caption("No cached bars yet.")
    else:
        st.dataframe(prov, width="stretch", hide_index=True)

    st.subheader("Recent skips — why signals did NOT become trades")
    skips = q(f"SELECT ts, mode, strategy, symbol, reason FROM skips WHERE {MW} "
              "ORDER BY id DESC LIMIT 500", MP)
    if skips.empty:
        st.caption("None recorded.")
        return
    if not multi:
        skips = skips.drop(columns=["mode"])
    reason_counts = (skips["reason"].str.split(":").str[0]
                     .value_counts().reset_index())
    reason_counts.columns = ["reason", "count"]
    c1, c2 = st.columns([1, 2])
    c1.dataframe(reason_counts, width="stretch", hide_index=True)
    c2.dataframe(skips, width="stretch", hide_index=True, height=360)


# ===========================================================================
# 🟢 LIVE — real-money readiness & broker gate (inactive until a graduate)
# ===========================================================================

def live_readiness() -> None:
    st.caption("Real orders route through the broker only for strategies that "
               "**graduate** off the paper book. This is the path from paper → real.")
    from bot import reports
    from rich.console import Console
    results = reports.promotion_readiness(console=Console(quiet=True), mode="PAPER")
    ready = [r for r in results if r["verdict"] == "READY"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Real orders", "OFF" if not config.LIVE_TRADING_ENABLED else "ON ⚠️")
    c2.metric("Allowlisted strategies", len(config.LIVE_STRATEGY_ALLOWLIST))
    c3.metric("Promotion-ready", len(ready),
              help=f"meet PROMOTION_CRITERIA over the trailing "
                   f"{config.PROMOTION_CRITERIA['window_sessions']} sessions "
                   "(fallback-feed trades excluded)")

    if not config.LIVE_TRADING_ENABLED:
        st.info("Real trading is **disabled** (`LIVE_TRADING_ENABLED=False`). When "
                "enabled, only allowlisted strategies may route live orders; "
                "everything else keeps paper-trading in the same session.")
    else:
        st.warning("⚠️ Real order placement is ENABLED for allowlisted strategies.")

    st.subheader("Promotion readiness (paper track record)")
    st.caption("Judged on the production **Fyers** feed only — trades booked on a "
               "yfinance fallback/degrade are excluded.")
    if not results:
        st.caption("No paper trades recorded yet.")
        return
    rows = []
    for r in results:
        rows.append({
            "Strategy": r["strategy"], "Trades": r["trades"],
            "Win %": round(r["win_rate"], 0),
            "PF": round(r["profit_factor"], 2) if r["profit_factor"] else None,
            "Expectancy ₹": round(r["expectancy"], 0),
            "Net ₹": round(r["net"], 0),
            "Verdict": "✅ READY" if r["verdict"] == "READY" else "⏳ NOT READY",
            "Failing": ", ".join(r["failing"]) or "-",
        })
    df = pd.DataFrame(rows).sort_values(["Verdict", "Net ₹"], ascending=[True, False])
    st.dataframe(df, width="stretch", hide_index=True)


def live_broker_gate() -> None:
    st.caption("Broker wiring and the hard gates that guard real order placement.")
    dhan = config.dhan_settings()
    fyers = config.fyers_settings()
    confirm_ok = config.live_confirm() == config.LIVE_CONFIRM_STRING

    c1, c2, c3 = st.columns(3)
    c1.metric("Order gate", "CLOSED (safe)" if not config.LIVE_TRADING_ENABLED else "OPEN ⚠️")
    c2.metric("Dhan creds",
              "configured" if dhan["client_id"] and dhan["access_token"] else "missing")
    c3.metric("Fyers creds",
              "configured" if fyers["app_id"] and fyers["secret_id"] else "missing")

    st.subheader("Live gate checklist")
    st.caption("ALL four must pass before a single real order can route "
               "(run_live.py --live).")
    checks = [
        ("config.LIVE_TRADING_ENABLED", config.LIVE_TRADING_ENABLED),
        ("strategy in LIVE_STRATEGY_ALLOWLIST",
         len(config.LIVE_STRATEGY_ALLOWLIST) > 0),
        (".env live-confirm == LIVE_CONFIRM_STRING", confirm_ok),
        ("launched with --live flag", "runtime — set per session"),
    ]
    st.dataframe(pd.DataFrame(
        [{"Gate": g, "Status": ("✅" if v is True else "❌" if v is False else f"ℹ️ {v}")}
         for g, v in checks]),
        width="stretch", hide_index=True)

    st.markdown(
        f"- Allowlist: `{sorted(config.LIVE_STRATEGY_ALLOWLIST) or 'empty'}` "
        "— only these may route live.\n"
        "- Fyers provides free data (+ optional order routing); Dhan is the "
        "alternate order broker.\n"
        f"- Live capital cap: {inr(config.LIVE_CAPITAL)} · "
        f"max concurrent: {config.LIVE_MAX_CONCURRENT_POSITIONS}.")

    live_trades = q("SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl),0) AS net "
                    "FROM trades WHERE mode='LIVE'")
    n_live = int(live_trades.iloc[0]["n"]) if not live_trades.empty else 0
    if n_live:
        st.subheader("Live trades")
        st.metric("Live trades booked", n_live,
                  inr(float(live_trades.iloc[0]["net"])))
    else:
        st.caption("No live trades booked yet.")


# ---------------------------------------------------------------- dispatch
if view == "🟢 Live":
    st.title("🟢 Live — real trading")
    tab_ready, tab_broker = st.tabs(["Readiness", "Broker & Gate"])
    with tab_ready:
        live_readiness()
    with tab_broker:
        live_broker_gate()
else:
    st.title("📝 Paper")
    (tab_sum, tab_open, tab_closed, tab_fleet, tab_hist, tab_bt, tab_feed) = st.tabs(
        ["📊 Summary", "📖 Open Book", "📕 Closed Book", "🧬 Fleet",
         "📅 History", "🧪 Backtest", "📡 Feed & Status"])
    with tab_sum:
        render_summary()
    with tab_open:
        render_open_book()
    with tab_closed:
        render_closed_book()
    with tab_fleet:
        render_fleet()
    with tab_hist:
        render_history()
    with tab_bt:
        render_backtest()
    with tab_feed:
        render_feed_status()
