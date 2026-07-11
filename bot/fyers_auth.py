"""Fyers token management.

One-time interactive login (auth-code flow) caches an access token (valid ~1
day) plus a refresh token (valid ~15 days, rotates on use). Each morning the
bot renews the access token automatically via the refresh token + PIN — no
browser needed until the refresh token itself expires.

CLI:
  python -m bot.fyers_auth          # interactive login (run once / when refresh expires)
  python -m bot.fyers_auth --check  # show token status / attempt refresh
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

import requests

import config
from bot import clock

log = logging.getLogger(__name__)

REFRESH_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"


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


def refresh() -> str | None:
    """Renew the access token via the refresh token + PIN. Returns new token or None."""
    s = config.fyers_settings()
    tokens = _load_tokens()
    if not (tokens.get("refresh_token") and s["pin"] and s["app_id"] and s["secret_id"]):
        return None
    app_id_hash = hashlib.sha256(
        f"{s['app_id']}:{s['secret_id']}".encode()
    ).hexdigest()
    try:
        resp = requests.post(REFRESH_URL, json={
            "grant_type": "refresh_token",
            "appIdHash": app_id_hash,
            "refresh_token": tokens["refresh_token"],
            "pin": s["pin"],
        }, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("fyers token refresh failed: %s", exc)
        return None
    if data.get("s") != "ok" or "access_token" not in data:
        log.warning("fyers token refresh rejected: %s", data)
        return None
    tokens["access_token"] = data["access_token"]
    # refresh tokens rotate; keep the new one when provided
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    _save_tokens(tokens)
    log.info("fyers access token renewed")
    return tokens["access_token"]


def ensure_access_token() -> str | None:
    """Valid-today access token, renewing via refresh token if stale."""
    tokens = _load_tokens()
    saved = tokens.get("saved_at", "")
    today = clock.now_ist().date().isoformat()
    if tokens.get("access_token") and saved[:10] == today:
        return tokens["access_token"]
    return refresh() or (tokens.get("access_token") or None)


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
