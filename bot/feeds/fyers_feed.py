"""Real-time feed from the Fyers v3 data websocket (free with any Fyers account).

SymbolUpdate packets carry ltp + vol_traded_today (cumulative), which the
TickAggregator turns into 1m bars via volume deltas — same pattern as the Dhan
feed. On persistent errors the feed degrades permanently for the session to
the free yfinance poller and alerts; trading never stops.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime

import config
from bot import alerts, clock, db, fyers_auth
from bot.bars import Bar, TickAggregator
from bot.feeds import Feed
from bot.feeds.yf_feed import YfFeed

log = logging.getLogger(__name__)


def fyers_symbol(symbol: str) -> str:
    if symbol.startswith("NSE:"):
        return symbol                      # already a full Fyers symbol (options)
    return config.FYERS_INDEX_SYMBOLS.get(symbol, f"NSE:{symbol}-EQ")


class FyersFeed(Feed):
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._fy_to_symbol = {fyers_symbol(s): s
                              for s in symbols + list(config.INDEX_SYMBOLS)}
        self._aggs = {s: TickAggregator(s) for s in self._fy_to_symbol.values()}
        self._ticks: queue.Queue = queue.Queue(maxsize=100_000)
        self._stop = threading.Event()
        self._errors = 0
        self._fallback: YfFeed | None = None
        self._socket = None

    @property
    def source_name(self) -> str:
        return "yfinance (degraded from fyers)" if self._fallback else "fyers-ws"

    # ------------------------------------------------------------- websocket

    def start(self) -> None:
        token = fyers_auth.ws_token()
        if token is None:
            self._degrade("no valid access token — run the 30-second morning login: "
                          "python -m bot.fyers_auth")
            return
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as exc:
            self._degrade(f"fyers-apiv3 not installed: {exc}")
            return

        def on_message(message):
            if isinstance(message, dict):
                self._errors = 0
                try:
                    self._ticks.put_nowait(message)
                except queue.Full:
                    pass  # consumer stalled; drop rather than block the socket

        def on_error(message):
            self._errors += 1
            log.warning("fyers ws error #%d: %s", self._errors, message)
            if self._errors >= config.FYERS_FEED_MAX_ERRORS:
                self._degrade(f"websocket failed {self._errors}x: {message}")

        def on_open():
            self._socket.subscribe(symbols=list(self._fy_to_symbol),
                                   data_type="SymbolUpdate")
            self._socket.keep_running()

        self._socket = data_ws.FyersDataSocket(
            access_token=token, log_path="", litemode=False,
            write_to_file=False, reconnect=True,
            on_connect=on_open, on_close=lambda m: log.info("fyers ws closed: %s", m),
            on_error=on_error, on_message=on_message,
        )
        threading.Thread(target=self._socket.connect, daemon=True,
                         name="fyers-ws").start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._socket is not None:
                self._socket.close_connection()
        except Exception:  # noqa: BLE001
            pass

    def _degrade(self, reason: str) -> None:
        if self._fallback is None:
            log.error("FYERS FEED DEGRADED -> yfinance: %s", reason)
            alerts.send(f"⚠️ Fyers feed degraded to yfinance: {reason}")
            self._fallback = YfFeed(self.symbols)

    # ------------------------------------------------------------------ poll

    def poll(self) -> list[Bar]:
        if self._fallback is not None:
            return self._fallback.poll()

        out: list[Bar] = []
        cache_rows: list[tuple] = []
        while True:
            try:
                data = self._ticks.get_nowait()
            except queue.Empty:
                break
            bar = self._apply_tick(data)
            if bar is not None:
                out.append(bar)
                cache_rows.append((bar.symbol, bar.ts.isoformat(), bar.open,
                                   bar.high, bar.low, bar.close, bar.volume, "fyers"))
        if cache_rows:
            try:
                db.upsert_bars(cache_rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("bar cache write failed: %s", exc)
        out.sort(key=lambda b: (b.ts, b.symbol))
        return out

    def _apply_tick(self, data: dict) -> Bar | None:
        try:
            sym = self._fy_to_symbol.get(str(data.get("symbol", "")))
            if sym is None:
                return None
            ltp = float(data.get("ltp") or 0)
            if ltp <= 0:
                return None
            vol_raw = data.get("vol_traded_today")
            cum_vol = int(vol_raw) if vol_raw is not None else None
            epoch = data.get("last_traded_time") or data.get("exch_feed_time")
            ts = (datetime.fromtimestamp(int(epoch), tz=clock.IST)
                  if epoch else clock.now_ist())
            return self._aggs[sym].on_tick(ts, ltp, cum_vol)
        except Exception as exc:  # noqa: BLE001 — malformed packet: skip it
            log.debug("bad fyers packet %s: %s", data, exc)
            return None
