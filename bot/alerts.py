"""Discord alerts. Supports either a webhook URL or a bot token + channel id
(whichever is configured). Silent no-op when neither is set.

Two kinds of message:
  * send()                — ordinary notes (EOD summary, freeze reminder).
  * send_failure_alert()  — a redacted, categorized health alert for pipeline
    failures (live-feed / login-auth / LLM), routed to the dedicated alerts
    channel when configured, with an optional per-key state-file throttle so a
    recurring failure doesn't spam (mirrors the engine's hourly freeze nudge).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

import config

log = logging.getLogger(__name__)


def send(message: str, *, prefer_alerts: bool = False) -> bool:
    s = config.discord_settings()
    content = message[:1990]   # Discord caps messages at 2000 chars
    try:
        if s["webhook_url"]:
            resp = requests.post(s["webhook_url"], json={"content": content}, timeout=10)
            return resp.status_code in (200, 204)
        if s["bot_token"] and s["channel_id"]:
            # Health alerts prefer a dedicated channel when one is set.
            channel = (s.get("alerts_channel") or s["channel_id"]
                       if prefer_alerts else s["channel_id"])
            resp = requests.post(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                headers={"Authorization": f"Bot {s['bot_token']}"},
                json={"content": content}, timeout=10,
            )
            if resp.status_code != 200:
                log.warning("discord bot post failed: %s %s",
                            resp.status_code, resp.text[:200])
            return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("discord alert failed: %s", exc)
    return False


# --------------------------------------------------------------- failure alerts

def _clip(text: str, limit: int = 400) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_failure_alert(failures: list[dict]) -> str:
    """Render a compact, redacted health alert from a list of
    {"kind", "detail"} dicts. Pure — no I/O — so it's trivially testable."""
    n = len(failures)
    lines = [f"\U0001f6a8 Intraday bot health alert — {n} failure"
             f"{'s' if n != 1 else ''} this run"]
    for f in failures[:15]:
        lines.append(f"❌ {f.get('kind', 'FAILURE')}: {_clip(f.get('detail', ''))}")
    return "\n".join(lines)


def _throttle_path(key: str):
    return config.DATA_DIR / f".alert_{key}"


def _throttle_active(key: str, minutes: int) -> bool:
    """True when a same-key alert was sent within the last `minutes` — read from
    a tiny ISO-timestamp state file. All IO errors are swallowed (treated as 'no
    prior send'), so a throttle problem can never suppress a real alert forever."""
    if minutes <= 0:
        return False
    path = _throttle_path(key)
    try:
        if not path.exists():
            return False
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return datetime.now() - last < timedelta(minutes=minutes)


def _throttle_touch(key: str) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _throttle_path(key).write_text(
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


_LOGIN_REMINDER_KEY = "login_reminder"


def send_login_reminder(throttle_minutes: int = 60) -> str:
    """Market-hours-gated, hourly nudge to run a fresh Fyers login.

    ensure_access_token() calls this from several subsystems every cycle (feed,
    broker, history, options); an unthrottled raw send() therefore fired one
    Discord ping PER caller for a single stale token — the "3 in a few minutes"
    spam. This gate fixes it: the nudge only posts on a trading-day session
    (PREOPEN/OPEN/SQUAREOFF — never nights, weekends, or holidays) and at most
    once per `throttle_minutes` via a state file in this bot's own data dir.

    DELIBERATELY NOT SHARED: stockbot + mcxbot share one Fyers token and
    coordinate a single throttle at <FYERS_TOKEN_PATH dir>/.fyers_login_reminder_sent.
    intraday has its OWN token (data/cache/fyers_tokens.json) and its OWN login
    (`python -m bot.fyers_auth`), so its nudge MUST stay separate — merging it
    into their shared file would let their reminder suppress intraday's distinct
    "run bot.fyers_auth" nudge and you'd never learn intraday needs its own login.
    Returns a short status ("off-session" | "throttled" | "sent" | "failed")."""
    from bot import clock
    if clock.phase(clock.now_ist()) == clock.CLOSED:
        return "off-session"
    if _throttle_active(_LOGIN_REMINDER_KEY, throttle_minutes):
        return "throttled"
    ok = send("No fresh Fyers login today — run `python -m bot.fyers_auth`")
    if ok:
        _throttle_touch(_LOGIN_REMINDER_KEY)
    return "sent" if ok else "failed"


def send_failure_alert(failures: list[dict], *, throttle_key: str | None = None,
                       throttle_minutes: int = 0) -> str:
    """Post a redacted, categorized failure alert to the alerts channel.

    `failures` is a list of {"kind", "detail"}. No-op (returns "no failures") on
    an empty list. With `throttle_key`/`throttle_minutes` set, a same-key alert
    inside the window is suppressed ("throttled") — mirroring the hourly
    login-reminder cadence during market hours. Never raises; returns a short
    status ("no failures" | "throttled" | "sent" | "failed")."""
    if not failures:
        return "no failures"
    if throttle_key and _throttle_active(throttle_key, throttle_minutes):
        return "throttled"
    ok = send(build_failure_alert(failures), prefer_alerts=True)
    if ok and throttle_key:
        _throttle_touch(throttle_key)
    return "sent" if ok else "failed"
