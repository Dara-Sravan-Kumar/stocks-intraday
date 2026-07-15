"""Fyers token management.

A one-time interactive login (auth-code flow) caches an access token that is
valid for the trading day only. Fyers has DISABLED programmatic refresh to
comply with SEBI intraday-2FA rules — the validate-refresh-token endpoint now
answers HTTP 400 / code -16 ("Refresh token API is currently disabled..."), so
there is NO auto-renew. A fresh interactive login is required every trading day.

CLI:
  python -m bot.fyers_auth          # interactive login (run once each morning)
  python -m bot.fyers_auth --check  # show token status (no auto-refresh)
"""
from __future__ import annotations

import json
import logging

import config
from bot import alerts, clock

log = logging.getLogger(__name__)

# Fyers permanently disabled programmatic token renewal to comply with SEBI
# intraday-2FA rules: POST validate-refresh-token now returns HTTP 400 / code
# -16. Renewal without a fresh interactive login is impossible.
REFRESH_DISABLED_CODE = -16
REFRESH_DISABLED_MESSAGE = (
    "Refresh token API is currently disabled to comply with SEBI regulations "
    "(code -16) — run a fresh login: python -m bot.fyers_auth"
)


def _load_tokens() -> dict:
    try:
        return json.loads(config.FYERS_TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_tokens(tokens: dict) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tokens["saved_at"] = clock.now_ist().isoformat(timespec="seconds")
    config.FYERS_TOKENS_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def login_interactive() -> bool:
    """Daily auth-code flow (SEBI requires fresh 2FA every trading day; Fyers
    has disabled the refresh-token API). Opens the browser, accepts either the
    bare auth_code or the whole redirect URL pasted back."""
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    from fyers_apiv3 import fyersModel

    s = config.fyers_settings()
    if not (s["app_id"] and s["secret_id"]):
        print("Set FYERS_APP_ID and FYERS_SECRET_ID in .env first "
              "(create an app at https://myapi.fyers.in).")
        return False
    session = fyersModel.SessionModel(
        client_id=s["app_id"], redirect_uri=s["redirect_uri"],
        response_type="code", state="stocks-intraday",
        secret_key=s["secret_id"], grant_type="authorization_code",
    )
    url = session.generate_authcode()
    print("\nOpening the Fyers login page (also printed below):\n")
    print(url)
    try:
        webbrowser.open(url, new=1)
    except Exception:  # noqa: BLE001
        pass
    print("\nAfter approving, copy EITHER the whole redirected URL from the "
          "address bar OR just the auth_code value.")
    raw = input("\nPaste here: ").strip()
    if "auth_code=" in raw:
        auth_code = parse_qs(urlparse(raw).query).get("auth_code", [""])[0] or raw
    else:
        auth_code = raw
    session.set_token(auth_code)
    resp = session.generate_token()
    if not resp or "access_token" not in resp:
        print(f"Token exchange failed: {resp}")
        return False
    _save_tokens({
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token", ""),
    })
    print(f"Tokens saved to {config.FYERS_TOKENS_FILE} — valid for today's session.")
    return True


def refresh() -> None:
    """Programmatic token renewal is impossible — always returns None.

    Fyers disabled the refresh-token endpoint (validate-refresh-token) to comply
    with SEBI intraday-2FA rules; it now answers HTTP 400 with code -16
    ("Refresh token API is currently disabled..."). This is a deliberate,
    network-free tombstone so callers fail fast and clearly instead of hammering
    a dead endpoint. The only way to renew is a fresh interactive login
    (``python -m bot.fyers_auth``).
    """
    log.warning("fyers token refresh unavailable (code %d): %s",
                REFRESH_DISABLED_CODE, REFRESH_DISABLED_MESSAGE)
    return None


def ensure_access_token() -> str | None:
    """Today's access token, or None when there is no fresh login.

    A token that is missing OR was stamped on an earlier day is useless: Fyers
    cannot refresh it programmatically (see refresh()), and handing a stale
    token to the websocket only yields a cryptic auth failure. So a not-today
    token is treated as MISSING — we return None and alert, prompting the
    morning interactive login.
    """
    tokens = _load_tokens()
    saved = tokens.get("saved_at", "")
    today = clock.now_ist().date().isoformat()
    if tokens.get("access_token") and saved[:10] == today:
        return tokens["access_token"]
    if tokens.get("access_token"):
        log.warning("fyers access token is stale (saved %s, today %s) — treating as missing",
                    saved[:10] or "never", today)
    alerts.send("No fresh Fyers login today — run `python -m bot.fyers_auth`")
    return None


def ws_token() -> str | None:
    """'APPID:access_token' form used by the websocket."""
    token = ensure_access_token()
    app_id = config.fyers_settings()["app_id"]
    return f"{app_id}:{token}" if (token and app_id) else None


def has_credentials() -> bool:
    s = config.fyers_settings()
    return bool(s["app_id"] and s["secret_id"])


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if "--check" in sys.argv:
        tok = ensure_access_token()
        print(f"access token: {'OK (valid today)' if tok else 'MISSING/EXPIRED - run: python -m bot.fyers_auth'}")
    else:
        login_interactive()
