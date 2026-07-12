"""Streamlit dashboard over data/bot.db — summary, today's picks, trades, ledger.

Run:  streamlit run dashboard_web.py    (http://localhost:8503)
Read-only: it never writes to the database.
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
    "LIVE": config.PAPER_STARTING_CASH,
    "REPLAY": config.PAPER_STARTING_CASH,
    "BACKTEST": config.PAPER_STARTING_CASH,
}


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


# ------------------------------------------------------- fixed sidebar nav
# Only two real trading accounts. Replay/backtest are testing output and live
# under the "Runs & Backtests" view, not here.

MODE_OPTIONS = {
    "📝 Paper": ["PAPER", "PAPER-OPT"],   # equity book + index-options book
    "🔴 Live": ["LIVE"],
}
PAGES = ["📊 Summary", "🎯 Today's Picks", "🧾 All Trades", "📒 Ledger",
         "📅 History", "🧪 Runs & Backtests", "🚫 Skips"]

head_l, head_r = st.columns([3, 2])
head_l.title("📈 Intraday Bot — Nifty 50 + Bank Nifty")
choice = head_r.radio(
    "Trading mode", list(MODE_OPTIONS), index=0, horizontal=True,
    help="Paper = simulated book · Live = real orders. "
         "Replays and backtests are inspected under the Runs & Backtests view.")
db_modes = MODE_OPTIONS[choice]           # underlying DB modes this covers
multi = len(db_modes) > 1                 # spans >1 book → show the mode column
start_cash = sum(START_CASH.get(m, 0.0) for m in db_modes)
head_r.caption("Paper equity = starting capital + closed-trade ledger. "
               "The ledger is the source of truth.")


def mode_where(col: str = "mode") -> tuple[str, tuple]:
    """WHERE fragment + params matching the selected trading mode's books."""
    ph = ",".join("?" * len(db_modes))
    return f"{col} IN ({ph})", tuple(db_modes)


MW, MP = mode_where()
trades_all = q(f"SELECT * FROM trades WHERE {MW} ORDER BY exit_ts", MP)
realized_total = trades_all["net_pnl"].sum() if not trades_all.empty else 0.0

TRADE_COLS = ["exit_ts", "mode", "strategy", "symbol", "side", "qty",
              "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl",
              "r_multiple", "exit_reason"]


def trade_view(df: pd.DataFrame, cols: list[str] = TRADE_COLS) -> pd.DataFrame:
    """Column subset for display; hides the mode column in single-book views."""
    keep = [c for c in cols if c in df.columns and (multi or c != "mode")]
    return df[keep]


def open_positions() -> pd.DataFrame:
    return q(f"SELECT * FROM positions WHERE status='OPEN' AND {MW} "
             "ORDER BY entry_ts", MP)


def heartbeat() -> dict | None:
    raw = q("SELECT value FROM kv WHERE key='engine_heartbeat'")
    if raw.empty:
        return None
    try:
        return json.loads(raw.iloc[0, 0])
    except Exception:
        return None


def position_rows(pos: pd.DataFrame, marks: dict[str, float]) -> tuple[list, float]:
    """Detailed open-position rows (risk, reward, distances) + total unrealized."""
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
            "LTP": ltp, "Unrealized ₹": round(upnl),
            "Stop": p["stop_price"], "Target": tgt,
            "Risk ₹": round(risk),
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


# -------------------------------------------------------------- Summary tab

def render_engine_status():
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


def render_summary():
    today = date.today().isoformat()
    render_engine_status()
    st.divider()
    pos = open_positions()
    marks = latest_marks(pos["symbol"].tolist() if not pos.empty else [])
    rows, unrealized = position_rows(pos, marks)

    trades_today = trades_all[trades_all["exit_ts"].str[:10] == today] \
        if not trades_all.empty else pd.DataFrame()
    realized_today = trades_today["net_pnl"].sum() if not trades_today.empty else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity (marked)", inr(start_cash + realized_total + unrealized))
    c2.metric("Unrealized (open)", inr(unrealized))
    c3.metric("Realized today", inr(realized_today))
    c4.metric("Open positions", len(rows))
    c5.metric("Realized all-time", inr(realized_total))

    st.subheader("Open positions")
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
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

    if "PAPER-OPT" in db_modes:
        chain = q("SELECT symbol, MAX(ts) AS ts, close FROM bars_1m "
                  "WHERE symbol LIKE 'NSE:%' AND substr(ts,1,10)=? "
                  "GROUP BY symbol ORDER BY symbol", (today,))
        st.subheader("Option chain — latest cached premiums")
        if chain.empty:
            st.caption("No option ticks cached yet today.")
        else:
            chain.columns = ["Contract", "Last tick", "Premium"]
            st.dataframe(chain, width="stretch", hide_index=True, height=320)


# --------------------------------------------------------- Today's Picks tab

def render_picks():
    today = date.today().isoformat()
    st.caption("Everything the bot picked TODAY — open positions in detail, "
               "entries taken, and what it looked at but skipped.")

    hb = heartbeat()
    if hb:
        benched = set(hb.get("benched", []))
        chips = "  ".join(
            f"🔴 ~~{s}~~ (benched)" if s in benched else f"🟢 {s}"
            for s in hb.get("strategies", []))
        st.markdown(f"**Strategies hunting today:** {chips or '—'}")

    pos = open_positions()
    marks = latest_marks(pos["symbol"].tolist() if not pos.empty else [])
    rows, unrealized = position_rows(pos, marks)

    st.subheader("Active picks — open positions")
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption(f"Total unrealized: {inr(unrealized)} · risk figures use the "
                   "planned stop; distances are signed in the trade's favor.")
    else:
        st.caption("No active picks — the book is flat.")

    st.subheader("Entries taken today")
    entered = trades_all[trades_all["entry_ts"].str[:10] == today] \
        if not trades_all.empty else pd.DataFrame()
    n_open_today = len(pos[pos["entry_ts"].str[:10] == today]) if not pos.empty else 0
    if entered.empty and n_open_today == 0:
        st.caption("No entries yet today.")
    else:
        if n_open_today:
            st.caption(f"{n_open_today} of today's entries are still open "
                       "(listed above); closed ones below.")
        if not entered.empty:
            st.dataframe(
                trade_view(entered, ["entry_ts", "mode", "strategy", "symbol",
                                     "side", "qty", "entry_price", "exit_price",
                                     "net_pnl", "r_multiple", "exit_reason"]),
                width="stretch", hide_index=True)

    st.subheader("Considered but skipped today")
    skips = q(f"SELECT ts, mode, strategy, symbol, reason FROM skips "
              f"WHERE {MW} AND substr(ts,1,10)=? ORDER BY id DESC LIMIT 300",
              MP + (today,))
    if skips.empty:
        st.caption("No skips recorded today.")
    else:
        top = (skips["reason"].str.split(":").str[0]
               .value_counts().head(8).reset_index())
        top.columns = ["Skip reason", "Count"]
        c1, c2 = st.columns([1, 2])
        c1.dataframe(top, width="stretch", hide_index=True)
        c2.dataframe(skips if multi else skips.drop(columns=["mode"]),
                     width="stretch", hide_index=True, height=300)


# ------------------------------------------------------------ All Trades tab

def trade_drilldown(tr: pd.Series):
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
                     y=alt.Y("close:Q", title="Price ₹",
                             scale=alt.Scale(zero=False))))

    hover = alt.selection_point(fields=["ts"], nearest=True,
                                on="mouseover", empty=False)
    probe = (alt.Chart(bars).mark_point(opacity=0)
             .encode(x="ts:T",
                     tooltip=[alt.Tooltip("ts:T", title="Time", format="%H:%M"),
                              alt.Tooltip("close:Q", title="Price", format=",.2f")])
             .add_params(hover))
    crosshair = (alt.Chart(bars).mark_rule(color="#8a8a8a", strokeWidth=1)
                 .encode(x="ts:T").transform_filter(hover))

    marker_df = pd.DataFrame([
        {"ts": entry_t, "price": tr["entry_price"], "label": "Entry",
         "color": EQUITY_BLUE},
        {"ts": exit_t, "price": tr["exit_price"], "label": "Exit",
         "color": pnl_color},
    ])
    points = (alt.Chart(marker_df)
              .mark_point(size=140, filled=True, stroke="white", strokeWidth=2)
              .encode(x="ts:T", y="price:Q",
                      color=alt.Color("color:N", scale=None),
                      tooltip=[alt.Tooltip("label:N", title="Event"),
                               alt.Tooltip("price:Q", title="Price", format=",.2f")]))
    point_labels = (alt.Chart(marker_df)
                    .mark_text(dy=-14, fontWeight="bold")
                    .encode(x="ts:T", y="price:Q", text="label:N"))

    levels = []
    if pd.notna(tr.get("planned_stop")):
        levels.append({"price": tr["planned_stop"], "label": "Stop", "color": BAD})
    if pd.notna(tr.get("planned_target")):
        levels.append({"price": tr["planned_target"], "label": "Target",
                       "color": GOOD})
    layers = [price, probe, crosshair, points, point_labels]
    if levels:
        lv = pd.DataFrame(levels)
        layers.append(alt.Chart(lv)
                      .mark_rule(strokeDash=[5, 4], strokeWidth=1.5)
                      .encode(y="price:Q", color=alt.Color("color:N", scale=None)))
        layers.append(alt.Chart(lv)
                      .mark_text(align="left", dx=4, dy=-6)
                      .encode(y="price:Q", text="label:N",
                              x=alt.value(0)))

    st.altair_chart(alt.layer(*layers).properties(height=380)
                    .interactive(bind_y=False), width="stretch")


def render_trades():
    if trades_all.empty:
        st.caption("No closed trades recorded yet in this mode.")
        return
    t = trades_all.copy()
    t["day"] = t["exit_ts"].str[:10]

    f1, f2, f3, f4 = st.columns(4)
    sel_strat = f1.selectbox("Strategy", ["(all)"] + sorted(t["strategy"].unique()),
                             key="tr_strat")
    sel_sym = f2.selectbox("Symbol", ["(all)"] + sorted(t["symbol"].unique()),
                           key="tr_sym")
    sel_out = f3.selectbox("Outcome", ["(all)", "Winners", "Losers"], key="tr_out")
    days = sorted(t["day"].unique())
    sel_range = f4.select_slider("Date range", options=days,
                                 value=(days[0], days[-1]), key="tr_days")

    view = t[(t["day"] >= sel_range[0]) & (t["day"] <= sel_range[1])]
    if sel_strat != "(all)":
        view = view[view["strategy"] == sel_strat]
    if sel_sym != "(all)":
        view = view[view["symbol"] == sel_sym]
    if sel_out == "Winners":
        view = view[view["net_pnl"] > 0]
    elif sel_out == "Losers":
        view = view[view["net_pnl"] <= 0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades", len(view))
    c2.metric("Gross", inr(view["gross_pnl"].sum()))
    c3.metric("Costs", inr(view["costs"].sum()))
    c4.metric("Net", inr(view["net_pnl"].sum()))

    st.dataframe(trade_view(view.sort_values("exit_ts", ascending=False),
                            ["exit_ts", "mode", "strategy", "symbol", "side",
                             "qty", "entry_ts", "entry_price", "exit_price",
                             "gross_pnl", "costs", "net_pnl", "r_multiple",
                             "planned_stop", "planned_target", "exit_reason"]),
                 width="stretch", hide_index=True, height=360)

    st.subheader("Trade drill-down")
    if view.empty:
        st.caption("No trades match the filters.")
        return
    ids = view.sort_values("exit_ts", ascending=False)["id"].tolist()
    lbl = {r["id"]: (f"#{r['id']} · {r['exit_ts'][:16]} · {r['strategy']} · "
                     f"{r['symbol']} {r['side']} · net {inr(r['net_pnl'])}")
           for _, r in view.iterrows()}
    sel_id = st.selectbox("Trade", ids, format_func=lambda i: lbl[i],
                          key="tr_drill")
    trade_drilldown(view[view["id"] == sel_id].iloc[0])


# --------------------------------------------------------------- Ledger tab

def render_ledger():
    if trades_all.empty:
        st.caption("Ledger is empty in this mode.")
        return
    t = trades_all.sort_values("exit_ts").copy()
    t["day"] = t["exit_ts"].str[:10]
    # Running balance over the FULL ledger (before any filters).
    t["balance"] = start_cash + t["net_pnl"].cumsum()

    f1, f2, f3 = st.columns(3)
    sel_strat = f1.selectbox("Strategy", ["(all)"] + sorted(t["strategy"].unique()),
                             key="lg_strat")
    sel_sym = f2.selectbox("Symbol", ["(all)"] + sorted(t["symbol"].unique()),
                           key="lg_sym")
    days = sorted(t["day"].unique())
    sel_range = f3.select_slider("Date range", options=days,
                                 value=(days[0], days[-1]), key="lg_days")
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

    disp = trade_view(view, ["exit_ts", "mode", "strategy", "symbol", "side",
                             "qty", "entry_price", "exit_price", "gross_pnl",
                             "costs", "net_pnl", "r_multiple", "planned_stop",
                             "planned_target", "exit_reason", "balance"])
    disp = disp.rename(columns={"balance": "Balance ₹"})
    st.dataframe(disp, width="stretch", hide_index=True, height=420)
    st.caption("Balance ₹ is the running book balance after each trade, "
               "computed over the whole ledger (filters don't change it).")

    st.subheader("Daily ledger")
    daily = (view.groupby("day")
             .agg(trades=("id", "count"), gross=("gross_pnl", "sum"),
                  costs=("costs", "sum"), net=("net_pnl", "sum"))
             .reset_index().sort_values("day", ascending=False))
    st.dataframe(daily, width="stretch", hide_index=True, height=240)

    label = choice.split(" ", 1)[-1]
    st.download_button(
        "⬇ Download CSV", view.to_csv(index=False).encode(),
        file_name=f"trades_{label}_{sel_range[0]}_{sel_range[1]}.csv",
        mime="text/csv")


# -------------------------------------------------------------- History tab

def render_history():
    if trades_all.empty:
        st.caption("No closed trades recorded yet in this mode.")
        return
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
    keys = ["mode", "strategy"] if multi else ["strategy"]
    g = t.groupby(keys).agg(
        trades=("id", "count"),
        win_rate=("net_pnl", lambda s: f"{(s > 0).mean() * 100:.0f}%"),
        gross=("gross_pnl", "sum"), costs=("costs", "sum"),
        net=("net_pnl", "sum"), avg_r=("r_multiple", "mean"),
    ).reset_index()
    st.dataframe(g, width="stretch", hide_index=True)


# ------------------------------------------------------ Runs & Backtests tab
# Independent of the Paper/Live toggle — this is the home for every run type,
# including the replay and backtest sessions.

def render_runs():
    st.caption("Every engine run — paper, live, replay, and backtest sessions. "
               "This is where replays and backtests are inspected. A replay "
               "re-runs the engine bar-by-bar over cached history; a backtest "
               "sweeps a date range at once.")
    all_runs = q("SELECT r.id, r.mode, r.session_date, r.feed_source, "
                 "r.started_at, r.finished_at, r.bars_processed, r.signals, "
                 "r.trades, COALESCE(t.net, 0) AS net_pnl "
                 "FROM runs r LEFT JOIN (SELECT run_id, SUM(net_pnl) AS net "
                 "FROM trades GROUP BY run_id) t ON t.run_id = r.id "
                 "ORDER BY r.id DESC LIMIT 500")
    if all_runs.empty:
        st.caption("No runs recorded yet.")
        return

    run_types = ["(all)"] + sorted(all_runs["mode"].unique())
    sel_type = st.selectbox("Run type", run_types)
    runs = all_runs if sel_type == "(all)" else all_runs[all_runs["mode"] == sel_type]

    st.dataframe(runs, width="stretch", hide_index=True, height=280)
    run_ids = runs["id"].tolist()
    if not run_ids:
        return
    sel_run = st.selectbox(
        "Inspect run", run_ids,
        format_func=lambda i: (
            f"#{i} — {runs.loc[runs['id'] == i, 'mode'].iloc[0]} "
            f"{runs.loc[runs['id'] == i, 'session_date'].iloc[0]}"))
    rt = q("SELECT strategy, symbol, side, qty, entry_ts, entry_price, "
           "exit_ts, exit_price, gross_pnl, costs, net_pnl, r_multiple, "
           "exit_reason FROM trades WHERE run_id=? ORDER BY exit_ts", (sel_run,))
    if rt.empty:
        st.caption("This run closed no trades.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Trades", len(rt))
    c2.metric("Net", inr(rt["net_pnl"].sum()))
    c3.metric("Costs", inr(rt["costs"].sum()))
    st.dataframe(rt, width="stretch", hide_index=True, height=330)
    per_strat = rt.groupby("strategy")["net_pnl"].agg(["count", "sum"])
    per_strat.columns = ["trades", "net ₹"]
    st.dataframe(per_strat.reset_index(), width="stretch", hide_index=True)


# ---------------------------------------------------------------- Skips tab

def render_skips():
    skips = q(f"SELECT ts, mode, strategy, symbol, reason FROM skips WHERE {MW} "
              "ORDER BY id DESC LIMIT 500", MP)
    st.caption("Why signals did NOT become trades — the risk engine's audit trail.")
    if skips.empty:
        st.caption("None recorded.")
        return
    if not multi:
        skips = skips.drop(columns=["mode"])
    reason_counts = (skips["reason"].str.split(":").str[0]
                     .value_counts().reset_index())
    reason_counts.columns = ["reason", "count"]
    st.dataframe(reason_counts, width="stretch", hide_index=True)
    st.dataframe(skips, width="stretch", hide_index=True, height=380)


# ------------------------------------------------------------ page dispatch

def _auto(fn, secs):
    """Render fn, auto-refreshing every `secs` if fragments are available."""
    if hasattr(st, "fragment"):
        st.fragment(run_every=secs)(fn)()
    else:
        fn()


tab_summary, tab_picks, tab_trades, tab_ledger, tab_hist, tab_runs, tab_skips = \
    st.tabs(PAGES)

with tab_summary:
    _auto(render_summary, "10s")
with tab_picks:
    _auto(render_picks, "30s")
with tab_trades:
    render_trades()
with tab_ledger:
    render_ledger()
with tab_hist:
    render_history()
with tab_runs:
    render_runs()
with tab_skips:
    render_skips()
