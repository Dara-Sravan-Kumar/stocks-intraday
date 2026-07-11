"""Real-time feed from the DhanHQ v2 market websocket.

A reader thread drains Quote packets into a queue; poll() aggregates ticks
into completed 1m bars (volume = cumulative-day-volume deltas). Requires the
Dhan Data API subscription; on persistent errors (expired 24h token, no
subscription, network) the feed degrades PERMANENTLY for the session to the
free yfinance poller and sends an alert — trading never stops.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime

import config
from bot import alerts, clock, db
from bot.bars import Bar, TickAggregator
from bot.feeds import Feed
from bot.feeds.yf_feed import YfFeed

log = logging.getLogger(__name__)


class DhanFeed(Feed):
    def __init__(self, symbols: list[str], instruments: dict):
        self.symbols = symbols
        self._id_to_symbol: dict[str, str] = {}
        for sym in symbols:
            inst = instruments.get(sym)
            if inst is not None and inst.dhan_security_id:
                self._id_to_symbol[str(inst.dhan_security_id)] = sym
        for name, sec_id in config.DHAN_INDEX_IDS.items():
            self._id_to_symbol[str(sec_id)] = name

        missing = [s for s in symbols if s not in self._id_to_symbol.values()]
        if missing:
            log.warning("dhan feed: no security id for %s", missing)
        if not self._id_to_symbol:
            raise RuntimeError("no Dhan security ids mapped — run scrip master fetch")

        self._aggs = {sym: TickAggregator(sym) for sym in self._id_to_symbol.values()}
        self._ticks: queue.Queue = queue.Queue(maxsize=100_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._errors = 0
        self._fallback: YfFeed | None = None

    @property
    def source_name(self) -> str:
        return "yfinance (degraded from dhan)" if self._fallback else "dhan-ws"

    # ------------------------------------------------------------- websocket

    def start(self) -> None:
        self._thread = threading.Thread(target=self._reader, daemon=True,
                                        name="dhan-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _reader(self) -> None:
        try:
            from dhanhq import DhanContext, MarketFeed
        except ImportError as exc:
            log.error("dhanhq SDK missing: %s", exc)
            self._degrade(f"dhanhq import failed: {exc}")
            return

        s = config.dhan_settings()
        ctx = DhanContext(s["client_id"], s["access_token"])
        # Quote packets carry LTP + cumulative day volume (needed for bar volume).
        idx_ids = set(config.DHAN_INDEX_IDS.values())
        instruments = []
        for sec_id in self._id_to_symbol:
            seg = MarketFeed.IDX if sec_id in idx_ids else MarketFeed.NSE
            instruments.append((seg, str(sec_id), MarketFeed.Quote))

        while not self._stop.is_set():
            try:
                feed = MarketFeed(ctx, instruments, version="v2")
                while not self._stop.is_set():
                    feed.run_forever()
                    data = feed.get_data()
                    if data:
                        self._errors = 0
                        try:
                            self._ticks.put_nowait(data)
                        except queue.Full:
                            pass  # consumer stalled; drop rather than block the socket
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                log.warning("dhan ws error #%d: %s", self._errors, exc)
                if self._errors >= config.DHAN_FEED_MAX_ERRORS:
                    self._degrade(f"websocket failed {self._errors}x: {exc}")
                    return
                self._stop.wait(min(2 ** self._errors, 30))  # backoff, then reconnect

    def _degrade(self, reason: str) -> None:
        if self._fallback is None:
            log.error("DHAN FEED DEGRADED -> yfinance: %s", reason)
            alerts.send(f"⚠️ Dhan feed degraded to yfinance: {reason}\n"
                        f"(check DHAN_ACCESS_TOKEN — 24h validity — and Data API subscription)")
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
                                   bar.high, bar.low, bar.close, bar.volume, "dhan"))
        if cache_rows:
            try:
                db.upsert_bars(cache_rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("bar cache write failed: %s", exc)
        out.sort(key=lambda b: (b.ts, b.symbol))
        return out

    def _apply_tick(self, data: dict) -> Bar | None:
        """Map one SDK packet to a tick; returns a completed 1m bar if any."""
        try:
            sec_id = str(data.get("security_id", ""))
            sym = self._id_to_symbol.get(sec_id)
            if sym is None:
                return None
            ltp = float(data.get("LTP") or data.get("ltp") or 0)
            if ltp <= 0:
                return None
            vol_raw = data.get("volume") or data.get("Volume")
            cum_vol = int(vol_raw) if vol_raw is not None else None
            ts = self._parse_ltt(data) or clock.now_ist()
            return self._aggs[sym].on_tick(ts, ltp, cum_vol)
        except Exception as exc:  # noqa: BLE001 — malformed packet: skip it
            log.debug("bad tick packet %s: %s", data, exc)
            return None

    @staticmethod
    def _parse_ltt(data: dict) -> datetime | None:
        ltt = data.get("LTT") or data.get("ltt")
        if ltt is None:
            return None
        try:
            if isinstance(ltt, (int, float)):
                return datetime.fromtimestamp(int(ltt), tz=clock.IST)
            # SDK sometimes formats as 'HH:MM:SS'
            now = clock.now_ist()
            h, m, s = (int(x) for x in str(ltt).split(":"))
            return now.replace(hour=h, minute=m, second=s, microsecond=0)
        except Exception:  # noqa: BLE001
            return None
