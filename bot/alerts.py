"""Discord alerts. Supports either a webhook URL or a bot token + channel id
(whichever is configured). Silent no-op when neither is set."""
from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger(__name__)


def send(message: str) -> bool:
    s = config.discord_settings()
    content = message[:1990]   # Discord caps messages at 2000 chars
    try:
        if s["webhook_url"]:
            resp = requests.post(s["webhook_url"], json={"content": content}, timeout=10)
            return resp.status_code in (200, 204)
        if s["bot_token"] and s["channel_id"]:
            resp = requests.post(
                f"https://discord.com/api/v10/channels/{s['channel_id']}/messages",
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
