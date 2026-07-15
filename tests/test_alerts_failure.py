"""Task 2 — the failure-alert builder, throttle, and routing."""
from __future__ import annotations

import config
from bot import alerts


def test_build_failure_alert_lists_each_category():
    msg = alerts.build_failure_alert([
        {"kind": "LIVE DATA FEED", "detail": "feed aborted"},
        {"kind": "LLM / DISCOVERY", "detail": "claude missing"},
    ])
    assert "health alert" in msg and "2 failures" in msg
    assert "LIVE DATA FEED" in msg and "feed aborted" in msg
    assert "LLM / DISCOVERY" in msg and "claude missing" in msg


def test_build_failure_alert_clips_long_detail():
    msg = alerts.build_failure_alert([{"kind": "X", "detail": "y" * 800}])
    assert msg.endswith("...") and len(msg) < 500


def test_send_failure_alert_no_op_on_empty():
    assert alerts.send_failure_alert([]) == "no failures"


def test_send_failure_alert_sends_then_throttles(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send", lambda msg, **kw: sent.append(msg) or True)

    failures = [{"kind": "LIVE DATA FEED / LOGIN", "detail": "frozen"}]
    assert alerts.send_failure_alert(failures, throttle_key="k",
                                     throttle_minutes=60) == "sent"
    assert len(sent) == 1
    # second call within the window is suppressed
    assert alerts.send_failure_alert(failures, throttle_key="k",
                                     throttle_minutes=60) == "throttled"
    assert len(sent) == 1


def test_failed_send_does_not_write_throttle(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(alerts, "send", lambda msg, **kw: False)
    failures = [{"kind": "X", "detail": "d"}]
    assert alerts.send_failure_alert(failures, throttle_key="k",
                                     throttle_minutes=60) == "failed"
    # no state file written -> not throttled next time
    assert not (tmp_path / ".alert_k").exists()


def test_login_reminder_sends_once_then_throttles(monkeypatch, tmp_path):
    """In-session, the login nudge fires once then is throttled for the hour —
    the fix for the 'same nudge 3x in a few minutes' spam (ensure_access_token
    is called from several subsystems per cycle)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from bot import clock
    monkeypatch.setattr(clock, "phase", lambda now: clock.OPEN)
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send", lambda msg, **kw: sent.append(msg) or True)

    assert alerts.send_login_reminder(throttle_minutes=60) == "sent"
    assert len(sent) == 1 and "login" in sent[0].lower()
    # every subsequent caller within the hour is suppressed
    assert alerts.send_login_reminder(throttle_minutes=60) == "throttled"
    assert alerts.send_login_reminder(throttle_minutes=60) == "throttled"
    assert len(sent) == 1


def test_login_reminder_silent_off_session(monkeypatch, tmp_path):
    """No nudge outside a trading-day session — never pings at night/weekends."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from bot import clock
    monkeypatch.setattr(clock, "phase", lambda now: clock.CLOSED)
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send", lambda msg, **kw: sent.append(msg) or True)

    assert alerts.send_login_reminder() == "off-session"
    assert sent == []


def test_send_prefers_alerts_channel(monkeypatch):
    posted: dict = {}

    class _Resp:
        status_code = 200
        text = ""

    def fake_post(url, **kw):
        posted["url"] = url
        return _Resp()

    monkeypatch.setattr(config, "discord_settings", lambda: {
        "webhook_url": "", "bot_token": "tok", "channel_id": "111",
        "alerts_channel": "999",
    })
    monkeypatch.setattr(alerts.requests, "post", fake_post)
    alerts.send("hi", prefer_alerts=True)
    assert "999" in posted["url"]        # routed to the alerts channel
    alerts.send("hi", prefer_alerts=False)
    assert "111" in posted["url"]        # default channel otherwise
