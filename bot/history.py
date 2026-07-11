"""Historical data: 1m candle backfill into bars_1m and prev-day reference
levels (PDH/PDL/close, volume profile for RVOL, avg range) computed from it.

Sources: yfinance (free, ≤7 days per 1m request, ~30 days lookback) or the
Dhan intraday API when a token + data subscription exist.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

import config
from bot import clock, db
from bot.indicators import PrevDayLevels

log = logging.getLogger(__name__)


def _yf_symbol(symbol: str) -> str:
    return config.INDEX_SYMBOLS.get(symbol, f"{symbol}{config.YF_SUFFIX}")


def fetch_1m_yfinance(symbols: list[str], start: date, end: date) -> int:
    """Download 1m bars for [start, end] inclusive in ≤7-day chunks. Returns rows written."""
    import yfinance as yf

    total = 0
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=6), end)
        tickers = [_yf_symbol(s) for s in symbols]
        try:
            df = yf.download(
                tickers=tickers, interval="1m",
                start=chunk_start.isoformat(),
                end=(chunk_end + timedelta(days=1)).isoformat(),
                group_by="ticker", auto_adjust=False, threads=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance 1m download failed %s..%s: %s",
                        chunk_start, chunk_end, exc)
            chunk_start = chunk_end + timedelta(days=1)
            continue
        if df is None or df.empty:
            chunk_start = chunk_end + timedelta(days=1)
            continue
        total += _store_yf_frame(df, symbols)
        chunk_start = chunk_end + timedelta(days=1)
    return total


def _store_yf_frame(df: pd.DataFrame, symbols: list[str]) -> int:
    rows: list[tuple] = []
    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in symbols:
        ysym = _yf_symbol(sym)
        try:
            sub = df[ysym] if multi else df
        except KeyError:
            continue
        sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
        if sub.empty:
            continue
        idx = sub.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        idx = idx.tz_convert(clock.IST)
        for ts, r in zip(idx, sub.itertuples(index=False)):
            rows.append((
                sym, ts.isoformat(),
                float(r.Open), float(r.High), float(r.Low), float(r.Close),
                int(r.Volume) if r.Volume == r.Volume else 0,  # NaN check
                "yf",
            ))
    if rows:
        db.upsert_bars(rows)
    return len(rows)


def fetch_1m_dhan(symbols_with_ids: dict[str, str], start: date, end: date) -> int:
    """Backfill via Dhan intraday API (requires token + data subscription)."""
    from dhanhq import DhanContext, dhanhq

    s = config.dhan_settings()
    if not (s["client_id"] and s["access_token"]):
        log.warning("dhan history: no credentials, skipping")
        return 0
    dhan = dhanhq(DhanContext(s["client_id"], s["access_token"]))
    total = 0
    for sym, sec_id in symbols_with_ids.items():
        if not sec_id:
            continue
        try:
            resp = dhan.intraday_minute_data(
                security_id=str(sec_id), exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=start.isoformat(), to_date=end.isoformat(),
            )
            data = resp.get("data") or {}
            opens = data.get("open") or []
            if not opens:
                continue
            rows = []
            for i in range(len(opens)):
                ts = datetime.fromtimestamp(int(data["timestamp"][i]), tz=clock.IST)
                rows.append((
                    sym, ts.isoformat(), float(data["open"][i]),
                    float(data["high"][i]), float(data["low"][i]),
                    float(data["close"][i]), int(data["volume"][i]), "dhan",
                ))
            db.upsert_bars(rows)
            total += len(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("dhan history failed for %s: %s", sym, exc)
    return total


def fetch_1m_fyers(symbols: list[str], start: date, end: date) -> int:
    """Backfill via Fyers history API (free; needs a valid access token).

    Minute data is served in windows, chunked to FYERS_HISTORY_CHUNK_DAYS.
    """
    from fyers_apiv3 import fyersModel

    from bot import fyers_auth
    from bot.feeds.fyers_feed import fyers_symbol

    token = fyers_auth.ensure_access_token()
    if token is None:
        log.warning("fyers history: no valid token, skipping")
        return 0
    fy = fyersModel.FyersModel(client_id=config.fyers_settings()["app_id"],
                               token=token, is_async=False, log_path="")
    total = 0
    for sym in symbols:
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=config.FYERS_HISTORY_CHUNK_DAYS - 1),
                            end)
            try:
                resp = fy.history({
                    "symbol": fyers_symbol(sym), "resolution": "1",
                    "date_format": "1",
                    "range_from": chunk_start.isoformat(),
                    "range_to": chunk_end.isoformat(),
                    "cont_flag": "1",
                })
                candles = (resp or {}).get("candles") or []
                rows = []
                for c in candles:  # [epoch, o, h, l, c, v]
                    ts = datetime.fromtimestamp(int(c[0]), tz=clock.IST)
                    rows.append((sym, ts.isoformat(), float(c[1]), float(c[2]),
                                 float(c[3]), float(c[4]), int(c[5]), "fyers"))
                if rows:
                    db.upsert_bars(rows)
                    total += len(rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("fyers history failed %s %s..%s: %s",
                            sym, chunk_start, chunk_end, exc)
            chunk_start = chunk_end + timedelta(days=1)
    return total


def build_prev_day_levels(symbols: list[str], session_date: date,
                          lookback_days: int = 10) -> dict[str, PrevDayLevels]:
    """Compute reference levels for `session_date` from cached bars_1m history."""
    start_ts = (session_date - timedelta(days=lookback_days * 2)).isoformat()
    end_ts = session_date.isoformat()  # strictly before the session
    out: dict[str, PrevDayLevels] = {}

    for sym in symbols:
        rows = db.load_bars([sym], start_ts, end_ts)
        if not rows:
            out[sym] = PrevDayLevels()
            continue
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["ts"][:10], []).append(r)
        days = sorted(by_day)[-lookback_days:]
        if not days:
            out[sym] = PrevDayLevels()
            continue

        last_day = by_day[days[-1]]
        highs = [r["high"] for r in last_day]
        lows = [r["low"] for r in last_day]
        prev_close = last_day[-1]["close"]

        ranges, turnovers = [], []
        cum_profiles: list[list[float]] = []
        for d in days:
            bars = by_day[d]
            dh, dl = max(r["high"] for r in bars), min(r["low"] for r in bars)
            dc = bars[-1]["close"]
            if dc > 0:
                ranges.append((dh - dl) / dc * 100.0)
            turnovers.extend(r["close"] * r["volume"] for r in bars)
            cum, profile = 0, []
            for r in bars:
                cum += r["volume"]
                profile.append(float(cum))
            cum_profiles.append(profile)

        max_len = max(len(p) for p in cum_profiles)
        avg_cum = []
        for i in range(max_len):
            vals = [p[i] if i < len(p) else p[-1] for p in cum_profiles]
            avg_cum.append(sum(vals) / len(vals))

        out[sym] = PrevDayLevels(
            high=max(highs), low=min(lows), close=prev_close,
            avg_daily_range_pct=(sum(ranges) / len(ranges)) if ranges else None,
            avg_cum_volume=avg_cum,
            avg_1m_turnover=(sum(turnovers) / len(turnovers)) if turnovers else None,
        )
    return out
