"""Minimal Twilio REST helper (form-encoded, basic auth) — no SDK dependency."""
from __future__ import annotations

import json
from typing import Any

import httpx

from .config import get_settings

CONVERSATIONS_API = "https://conversations.twilio.com/v1"
FLEX_API = "https://flex-api.twilio.com/v1"


class TwilioAPIError(Exception):
    def __init__(self, status: int, code: int | None, message: str) -> None:
        super().__init__(f"Twilio API {status} (code {code}): {message[:400]}")
        self.status = status
        self.code = code
        self.message = message


async def twilio_post(url: str, data: dict[str, Any]) -> dict:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, data=data, auth=(s.twilio_account_sid or "", s.twilio_auth_token or ""))
    except httpx.HTTPError as exc:
        raise TwilioAPIError(0, None, repr(exc)) from exc
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = {}
        raise TwilioAPIError(resp.status_code, body.get("code"), body.get("message", resp.text))
    return resp.json() if resp.content else {}
