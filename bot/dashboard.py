"""Rich terminal dashboard for a running session. Reads engine state; a
daemon thread refreshes it every few seconds."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from bot import clock


class Dashboard:
    def __init__(self, engine):
        self.engine = engine
        self.events: deque[tuple[str, str, str]] = deque(maxlen=14)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # engine hook
    def on_event(self, kind: str, msg: str) -> None:
        stamp = (self.engine.now or clock.now_ist()).strftime("%H:%M")
        self.events.appendleft((stamp, kind, msg))

    def _render(self):
        eng = self.engine
        equity = eng.broker.equity(eng.marks)
        day_pnl = equity - (eng.day.start_equity if eng.day else equity)
        now = eng.now or clock.now_ist()
        phase = clock.phase(now)

        header = Text()
        header.append(f" {now.strftime('%Y-%m-%d %H:%M IST')} ", style="bold")
        header.append(f"[{phase}] ", style="cyan")
        header.append(f"feed: {eng.feed.source_name}  mode: {eng.mode}  ")
        pnl_style = "green" if day_pnl >= 0 else "red"
        header.append(f"equity ₹{equity:,.0f} ", style="bold")
        header.append(f"({day_pnl:+,.0f})", style=pnl_style)
        if eng.day and eng.day.halted:
            header.append(f"  ⛔ HALTED: {eng.day.halt_reason}", style="bold red")

        pos_table = Table(title="Open positions", expand=True)
        for col in ("Strategy", "Symbol", "Side", "Qty", "Entry", "Stop", "Target",
                    "LTP", "uP&L"):
            pos_table.add_column(col, justify="right")
        for p in eng.broker.positions:
            ltp = eng.marks.get(p.symbol, p.entry_price)
            upnl = p.unrealized(ltp)
            pos_table.add_row(
                p.strategy, p.symbol, p.side, str(p.qty),
                f"{p.entry_price:.2f}", f"{p.stop_price:.2f}",
                f"{p.target_price:.2f}" if p.target_price else "-",
                f"{ltp:.2f}",
                Text(f"{upnl:+,.0f}", style="green" if upnl >= 0 else "red"),
            )
        if not eng.broker.positions:
            pos_table.add_row(*["-"] * 9)

        strat_table = Table(title="Per-strategy (closed today)", expand=True)
        for col in ("Strategy", "Trades", "Net ₹"):
            strat_table.add_column(col, justify="right")
        agg: dict[str, tuple[int, float]] = {}
        for t in eng.closed_trades:
            n, net = agg.get(t.position.strategy, (0, 0.0))
            agg[t.position.strategy] = (n + 1, net + t.net_pnl)
        for name in sorted(agg):
            n, net = agg[name]
            strat_table.add_row(name, str(n),
                                Text(f"{net:+,.0f}",
                                     style="green" if net >= 0 else "red"))
        if not agg:
            strat_table.add_row("-", "-", "-")

        ev_table = Table(title="Recent events", expand=True, show_header=False)
        ev_table.add_column("t", width=6)
        ev_table.add_column("kind", width=10)
        ev_table.add_column("msg")
        styles = {"entry": "green", "exit": "cyan", "skip": "dim",
                  "halt": "bold red", "signal": "yellow", "squareoff": "magenta"}
        for stamp, kind, msg in self.events:
            ev_table.add_row(stamp, kind, Text(msg, style=styles.get(kind, "")))

        return Group(Panel(header), pos_table, strat_table, ev_table)

    def run_in_background(self) -> None:
        def loop():
            with Live(self._render(), refresh_per_second=1, screen=False) as live:
                while not self._stop.is_set():
                    self._stop.wait(config.DASHBOARD_REFRESH_SECONDS)
                    try:
                        live.update(self._render())
                    except Exception:  # noqa: BLE001 — never kill trading over UI
                        pass
        self._thread = threading.Thread(target=loop, daemon=True, name="dashboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
