"""Twilio WhatsApp channel (Sandbox now; a production Twilio sender later).

Inbound:  Twilio POSTs application/x-www-form-urlencoded to /webhooks/twilio
Outbound: REST API  POST /2010-04-01/Accounts/{SID}/Messages.json
Docs: https://www.twilio.com/docs/whatsapp/api
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

import httpx
from fastapi import Request

from .. import format_whatsapp
from ..config import get_settings
from .base import InboundMessage, ParsedWebhook, normalize_user_id
from .errors import SendError

TWILIO_API = "https://api.twilio.com/2010-04-01"
WA_PREFIX = "whatsapp:"


def compute_twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted k+v))."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TwilioChannel:
    name = "twilio"
    text_limit = 1600  # Twilio's body cap for a WhatsApp message

    def __init__(self) -> None:
        s = get_settings()
        self._sid = s.twilio_account_sid or ""
        self._token = s.twilio_auth_token or ""
        self._from = s.twilio_whatsapp_from
        self._validate = s.twilio_validate_signature
        self._public_base_url = s.public_base_url.rstrip("/")

    def _public_url(self, request: Request) -> str:
        # Behind ngrok/Vercel the URL Twilio signed is the *public* one.
        if self._public_base_url:
            url = self._public_base_url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            return url
        return str(request.url)

    async def verify_request(self, request: Request, raw_body: bytes) -> bool:
        if not self._validate:
            return True
        signature = request.headers.get("X-Twilio-Signature", "")
        params = dict(parse_qsl(raw_body.decode(), keep_blank_values=True))
        expected = compute_twilio_signature(self._token, self._public_url(request), params)
        return hmac.compare_digest(signature, expected)

    async def parse_webhook(self, request: Request, raw_body: bytes) -> ParsedWebhook:
        form = dict(parse_qsl(raw_body.decode(), keep_blank_values=True))
        sender = form.get("From", "")
        if not sender.startswith(WA_PREFIX):
            return ParsedWebhook()  # not a WhatsApp message (e.g. status callback)

        num_media = int(form.get("NumMedia", "0") or 0)
        body = form.get("Body", "")
        # Quick-reply buttons arrive as ButtonText/ButtonPayload.
        if not body and form.get("ButtonText"):
            body = form["ButtonText"]
        msg_type = "text" if body else ("media" if num_media else "unknown")

        msg = InboundMessage(
            channel=self.name,
            user_id=normalize_user_id(sender),
            reply_to=sender,  # keep Twilio's exact address ("whatsapp:+521...")
            message_id=form.get("MessageSid", ""),
            text=body,
            msg_type=msg_type,
            profile_name=form.get("ProfileName"),
        )
        return ParsedWebhook(messages=[msg])

    async def send_text(self, to: str, body: str, *, sanitize: bool = True) -> None:
        """Bot text is sanitized on the way out; a human's reply passes ``sanitize=False``."""
        if not to.startswith(WA_PREFIX):
            to = f"{WA_PREFIX}+{to.lstrip('+')}"
        from_ = self._from if self._from.startswith(WA_PREFIX) else f"{WA_PREFIX}{self._from}"
        if sanitize:
            body = format_whatsapp.for_wire(body)
        data = {"From": from_, "To": to, "Body": body[: self.text_limit]}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{TWILIO_API}/Accounts/{self._sid}/Messages.json",
                    data=data,
                    auth=(self._sid, self._token),
                )
        except httpx.HTTPError as exc:
            raise SendError(self.name, 0, repr(exc)) from exc
        if resp.status_code >= 400:
            raise SendError(self.name, resp.status_code, resp.text)

    async def mark_as_read(self, msg: InboundMessage) -> None:
        return None  # Twilio doesn't expose read receipts for WhatsApp
