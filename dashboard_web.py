"""Streamlit dashboard over data/bot.db — live book, history, full ledger.

Run:  streamlit run dashboard_web.py    (http://localhost:8501)
Read-only: it never writes to the database.
"""
from __future__ import annotations

import sqlite3
from datetime import date

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


@st.cache_resource
def get_conn():
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


def latest_marks(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    df = q(f"SELECT symbol, close FROM bars_1m b WHERE symbol IN ({ph}) "
           f"AND ts = (SELECT MAX(ts) FROM bars_1m WHERE symbol = b.symbol)",
           tuple(symbols))
    return dict(zip(df["symbol"], df["close"])) if not df.empty else {}


st.title("📈 Intraday Bot — Nifty 50 + Bank Nifty")
mode = st.sidebar.selectbox(
    "Mode", ["PAPER", "PAPER-OPT", "REPLAY", "BACKTEST", "LIVE"],
    help="PAPER = equity paper book · PAPER-OPT = index options paper book")
st.sidebar.caption("Paper equity = starting capital + closed-trade ledger. "
                   "The ledger is the source of truth.")

trades_all = q("SELECT * FROM trades WHERE mode=? ORDER BY exit_ts", (mode,))
realized_total = trades_all["net_pnl"].sum() if not trades_all.empty else 0.0

tab_live, tab_hist, tab_ledger, tab_runs, tab_skips = st.tabs(
    ["🔴 Live", "📅 History", "📒 Ledger", "🧪 Runs & Backtests", "🚫 Skips"])

# ----------------------------------------------------------------- Live tab

def render_engine_status():
    import json
    raw = q("SELECT value FROM kv WHERE key='engine_heartbeat'")
    if raw.empty:
        st.info("No engine heartbeat yet — the status panel activates once a "
                "session runs (run_live.py).")
        return
    try:
        hb = json.loads(raw.iloc[0, 0])
    except Exception:
        return
    stale = hb.get("wall_ts", "")[:16]
    running = f"**{hb.get('mode')}** · phase **{hb.get('phase')}** · feed `{hb.get('feed')}`"
    if hb.get("halted"):
        running += f" · ⛔ HALTED: {hb.get('halt_reason')}"
    st.markdown(f"⚙️ {running} — last beat {stale}")
    cols = st.columns(4)
    cols[0].metric("Engine equity", inr(hb.get("equity", 0)))
    cols[1].metric("Day P&L", inr(hb.get("day_pnl", 0)))
    cols[2].metric("Entry budget used",
                   f"{hb.get('entries_today', 0)}/{hb.get('entries_budget', 0)}")
    cols[3].metric("Trades today", hb.get("trades_today", 0))
    strategies = hb.get("strategies", [])
    benched = set(hb.get("benched", []))
    chips = "  ".join(
        f"🔴 ~~{s}~~ (benched)" if s in benched else f"🟢 {s}" for s in strategies
    )
    st.markdown(f"**Running strategies:** {chips or '—'}")


def render_live():
    today = date.today().isoformat()
    render_engine_status()
    st.divider()
    pos = q("SELECT * FROM positions WHERE status='OPEN' AND mode=? ORDER BY entry_ts",
            (mode,))
    marks = latest_marks(pos["symbol"].tolist() if not pos.empty else [])

    unrealized = 0.0
    rows = []
    for _, p in pos.iterrows():
        ltp = marks.get(p["symbol"], p["entry_price"])
        sign = 1 if p["side"] == "LONG" else -1
        upnl = (ltp - p["entry_price"]) * p["qty"] * sign
        unrealized += upnl
        rows.append({
            "Strategy": p["strategy"], "Symbol": p["symbol"], "Side": p["side"],
            "Qty": int(p["qty"]), "Entry": p["entry_price"], "Stop": p["stop_price"],
            "Target": p["target_price"], "LTP": ltp, "Unrealized ₹": round(upnl),
            "Since": p["entry_ts"][11:16],
        })

    trades_today = trades_all[trades_all["exit_ts"].str[:10] == today] \
        if not trades_all.empty else pd.DataFrame()
    realized_today = trades_today["net_pnl"].sum() if not trades_today.empty else 0.0
    equity = config.PAPER_STARTING_CASH + realized_total + unrealized

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity (marked)", inr(equity))
    c2.metric("Unrealized (open)", inr(unrealized))
    c3.metric("Realized today", inr(realized_today))
    c4.metric("Open positions", len(rows))
    c5.metric("Realized all-time", inr(realized_total))

    st.subheader("Open positions")
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("Flat — no open positions.")

    st.subheader(f"Today's closed trades ({today})")
    if trades_today.empty:
        st.caption("No trades closed today.")
    else:
        st.dataframe(
            trades_today[["strategy", "symbol", "side", "qty", "entry_price",
                          "exit_price", "gross_pnl", "costs", "net_pnl",
                          "r_multiple", "exit_reason"]],
            use_container_width=True, hide_index=True)

    eq = q("SELECT ts, equity FROM equity_log WHERE mode=? AND substr(ts,1,10)=? "
           "ORDER BY ts", (mode, today))
    if not eq.empty:
        st.subheader("Session equity")
        eq["ts"] = pd.to_datetime(eq["ts"])
        st.line_chart(eq.set_index("ts")["equity"], color=EQUITY_BLUE)

    if mode == "PAPER-OPT":
        st.subheader("Option chain — latest cached premiums")
        chain = q("SELECT symbol, MAX(ts) AS ts, close FROM bars_1m "
                  "WHERE symbol LIKE 'NSE:%' AND substr(ts,1,10)=? "
                  "GROUP BY symbol ORDER BY symbol", (today,))
        if chain.empty:
            st.caption("No option ticks cached yet today.")
        else:
            chain.columns = ["Contract", "Last tick", "Premium"]
            st.dataframe(chain, use_container_width=True, hide_index=True, height=320)


with tab_live:
    if hasattr(st, "fragment"):
        st.fragment(run_every="10s")(render_live)()
    else:
        render_live()

# -------------------------------------------------------------- History tab

with tab_hist:
    if trades_all.empty:
        st.caption("No closed trades recorded yet in this mode.")
    else:
        t = trades_all.copy()
        t["day"] = t["exit_ts"].str[:10]

        daily = t.groupby("day")["net_pnl"].sum().reset_index()
        daily["equity"] = config.PAPER_STARTING_CASH + daily["net_pnl"].cumsum()

        c1, c2, c3, c4 = st.columns(4)
        wins = (t["net_pnl"] > 0).sum()
        gross_win = t.loc[t["net_pnl"] > 0, "net_pnl"].sum()
        gross_loss = -t.loc[t["net_pnl"] <= 0, "net_pnl"].sum()
        c1.metric("Net P&L (all time)", inr(realized_total))
        c2.metric("Trades / Win rate", f"{len(t)} / {wins / len(t) * 100:.0f}%")
        c3.metric("Profit factor",
                  f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "∞")
        c4.metric("Total costs paid", inr(t["costs"].sum()))

        st.subheader("Equity curve (closed-trade ledger)")
        st.line_chart(daily.set_index("day")["equity"], color=EQUITY_BLUE)

        st.subheader("Daily net P&L")
        daily_disp = daily.set_index("day")[["net_pnl"]]
        daily_disp["profit"] = daily_disp["net_pnl"].clip(lower=0)
        daily_disp["loss"] = daily_disp["net_pnl"].clip(upper=0)
        st.bar_chart(daily_disp[["profit", "loss"]], color=[GOOD, BAD])

        st.subheader("Cumulative net by strategy")
        pivot = (t.pivot_table(index="day", columns="strategy", values="net_pnl",
                               aggfunc="sum").fillna(0).cumsum())
        colors = [STRATEGY_COLORS.get(c, "#52514e") for c in pivot.columns]
        st.line_chart(pivot, color=colors)

        st.subheader("Per-strategy summary")
        g = t.groupby("strategy").agg(
            trades=("id", "count"),
            win_rate=("net_pnl", lambda s: f"{(s > 0).mean() * 100:.0f}%"),
            gross=("gross_pnl", "sum"), costs=("costs", "sum"),
            net=("net_pnl", "sum"), avg_r=("r_multiple", "mean"),
        ).reset_index()
        st.dataframe(g, use_container_width=True, hide_index=True)

# --------------------------------------------------------------- Ledger tab

with tab_ledger:
    if trades_all.empty:
        st.caption("Ledger is empty in this mode.")
    else:
        t = trades_all.copy()
        t["day"] = t["exit_ts"].str[:10]
        f1, f2, f3 = st.columns(3)
        strategies = ["(all)"] + sorted(t["strategy"].unique())
        symbols = ["(all)"] + sorted(t["symbol"].unique())
        sel_strat = f1.selectbox("Strategy", strategies)
        sel_sym = f2.selectbox("Symbol", symbols)
        days = sorted(t["day"].unique())
        sel_range = f3.select_slider("Date range", options=days,
                                     value=(days[0], days[-1]))
        view = t[(t["day"] >= sel_range[0]) & (t["day"] <= sel_range[1])]
        if sel_strat != "(all)":
            view = view[view["strategy"] == sel_strat]
        if sel_sym != "(all)":
            view = view[view["symbol"] == sel_sym]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", len(view))
        c2.metric("Gross", inr(view["gross_pnl"].sum()))
        c3.metric("Costs", inr(view["costs"].sum()))
        c4.metric("Net", inr(view["net_pnl"].sum()))

        st.dataframe(
            view[["exit_ts", "strategy", "symbol", "side", "qty", "entry_price",
                  "exit_price", "gross_pnl", "costs", "net_pnl", "r_multiple",
                  "planned_stop", "planned_target", "exit_reason"]],
            use_container_width=True, hide_index=True, height=420)
        st.download_button(
            "⬇ Download CSV", view.to_csv(index=False).encode(),
            file_name=f"trades_{mode}_{sel_range[0]}_{sel_range[1]}.csv",
            mime="text/csv")

# ----------------------------------------------------------------- Runs tab

with tab_runs:
    st.caption("Every engine run — live sessions, replays, and backtests run "
               "with --persist. Select one to inspect its trades.")
    runs = q("SELECT r.id, r.mode, r.session_date, r.feed_source, r.started_at, "
             "r.finished_at, r.bars_processed, r.signals, r.trades, "
             "COALESCE(t.net, 0) AS net_pnl "
             "FROM runs r LEFT JOIN (SELECT run_id, SUM(net_pnl) AS net "
             "FROM trades GROUP BY run_id) t ON t.run_id = r.id "
             "ORDER BY r.id DESC LIMIT 200")
    if runs.empty:
        st.caption("No runs recorded yet.")
    else:
        st.dataframe(runs, use_container_width=True, hide_index=True, height=280)
        run_ids = runs["id"].tolist()
        sel_run = st.selectbox("Inspect run", run_ids,
                               format_func=lambda i: (
                                   f"#{i} — "
                                   f"{runs.loc[runs['id'] == i, 'mode'].iloc[0]} "
                                   f"{runs.loc[runs['id'] == i, 'session_date'].iloc[0]}"))
        rt = q("SELECT strategy, symbol, side, qty, entry_ts, entry_price, "
               "exit_ts, exit_price, gross_pnl, costs, net_pnl, r_multiple, "
               "exit_reason FROM trades WHERE run_id=? ORDER BY exit_ts", (sel_run,))
        if rt.empty:
            st.caption("This run closed no trades.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Trades", len(rt))
            c2.metric("Net", inr(rt["net_pnl"].sum()))
            c3.metric("Costs", inr(rt["costs"].sum()))
            st.dataframe(rt, use_container_width=True, hide_index=True, height=330)
            per_strat = rt.groupby("strategy")["net_pnl"].agg(["count", "sum"])
            per_strat.columns = ["trades", "net ₹"]
            st.dataframe(per_strat.reset_index(), use_container_width=True,
                         hide_index=True)

# ---------------------------------------------------------------- Skips tab

with tab_skips:
    skips = q("SELECT ts, strategy, symbol, reason FROM skips WHERE mode=? "
              "ORDER BY id DESC LIMIT 500", (mode,))
    st.caption("Why signals did NOT become trades — the risk engine's audit trail.")
    if skips.empty:
        st.caption("None recorded.")
    else:
        reason_counts = (skips["reason"].str.split(":").str[0]
                         .value_counts().reset_index())
        reason_counts.columns = ["reason", "count"]
        st.dataframe(reason_counts, use_container_width=True, hide_index=True)
        st.dataframe(skips, use_container_width=True, hide_index=True, height=380)
