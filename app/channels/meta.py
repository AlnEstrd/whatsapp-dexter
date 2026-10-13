"""Meta WhatsApp Cloud API channel (test number today, production after approval).

Inbound:  Meta POSTs JSON to /webhooks/meta (legacy alias: /webhook)
Outbound: Graph API  POST /{phone_number_id}/messages
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional

import httpx
from fastapi import Request

from .. import format_whatsapp
from ..config import get_settings
from ..format_whatsapp import fit
from .base import InboundMessage, ParsedWebhook, ProviderEvent, normalize_user_id
from .errors import MediaError, SendError

MEDIA_TYPES = {"image", "document", "audio", "video", "sticker"}

# Webhook fields that tell you about your account / verification / number status.
# Subscribe to these in App Dashboard > WhatsApp > Configuration > Webhook fields.
ACCOUNT_FIELDS = {
    "account_review_update",
    "account_update",
    "business_capability_update",
    "phone_number_name_update",
    "phone_number_quality_update",
    "account_alerts",
}


class MetaChannel:
    name = "meta"
    text_limit = format_whatsapp.META_LIMIT  # a text message body (the splitter keeps each part under it)

    def __init__(self) -> None:
        s = get_settings()
        self._token = s.whatsapp_access_token or ""
        self._app_secret = s.meta_app_secret
        self._api_root = f"https://graph.facebook.com/{s.whatsapp_api_version}"
        self._base_url = f"{self._api_root}/{s.whatsapp_phone_number_id}"

    async def verify_request(self, request: Request, raw_body: bytes) -> bool:
        if not self._app_secret:
            return True  # signature check off until META_APP_SECRET is set
        header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            self._app_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(header, expected)

    async def parse_webhook(self, request: Request, raw_body: bytes) -> ParsedWebhook:
        payload = json.loads(raw_body or b"{}")
        return parse_meta_payload(payload)

    async def send_text(self, to: str, body: str, *, sanitize: bool = True) -> None:
        """A text message. Bot text is sanitized (app/format_whatsapp.py) on the way out; a human's
        reply (Slack, /admin/reply) passes ``sanitize=False`` and goes out exactly as typed."""
        to = normalize_user_id(to)  # Cloud API wants 52XXXXXXXXXX, not 521...
        if sanitize:
            body = format_whatsapp.for_wire(body)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[: self.text_limit]},
        }
        return _wamid(await self._post("/messages", payload))

    async def send_options(self, to: str, body: str, button: str, options: list) -> None:
        """Interactive list message (up to 10 rows). ``options``: flows.types.Option. Every string
        is fitted to its Meta limit at a word boundary (an over-long one makes Meta reject the message)."""
        to = normalize_user_id(to)
        rows = [{"id": o.id[:200], "title": fit(o.title, format_whatsapp.LIST_ROW_TITLE),
                 "description": fit(o.description, format_whatsapp.LIST_ROW_DESCRIPTION)}
                for o in options[: format_whatsapp.LIST_MAX_ROWS]]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": fit(format_whatsapp.for_wire(body), format_whatsapp.INTERACTIVE_BODY)},
                "action": {"button": fit(button, format_whatsapp.LIST_BUTTON),
                           "sections": [{"title": "Opciones", "rows": rows}]},
            },
        }
        return _wamid(await self._post("/messages", payload))

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> Optional[str]:
        """Text with up to 3 reply buttons, as ONE message. ``buttons``: [(id, title <= 20)]."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_user_id(to),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": fit(format_whatsapp.for_wire(body), format_whatsapp.INTERACTIVE_BODY)},
                "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256],
                                                                   "title": fit(t, format_whatsapp.REPLY_BUTTON)}}
                                       for i, t in buttons[: format_whatsapp.REPLY_MAX_BUTTONS]]},
            },
        }
        return _wamid(await self._post("/messages", payload))

    async def send_document(self, to: str, data: bytes, filename: str, caption: str = "") -> None:
        """Upload ``data`` as WhatsApp media, then send it as a document message.

        Sent by media id (not by public link) so generated files, such as a
        person's assessment report, never need a public URL. Subject to the
        same 24h customer-service window as text.
        """
        media_id = await self._upload_media(data, filename, "application/pdf")
        document: dict = {"id": media_id, "filename": filename}
        if caption:
            document["caption"] = caption[:1024]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_user_id(to),
            "type": "document",
            "document": document,
        }
        return _wamid(await self._post("/messages", payload))

    async def send_image(self, to: str, data: bytes, filename: str, caption: str = "",
                         mime: str = "image/png") -> None:
        """Upload an image and send it (shows as an inline preview). JPEG/PNG up to 5 MB."""
        media_id = await self._upload_media(data, filename, mime)
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = caption[:1024]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_user_id(to),
            "type": "image",
            "image": image,
        }
        return _wamid(await self._post("/messages", payload))

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        """Fetch an inbound file: ask Graph for a short-lived URL, then stream it.

        The size cap is checked against the declared size and again while streaming.
        Raises MediaError("too_large" | "fetch_failed").
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                info_resp = await client.get(f"{self._api_root}/{media_id}", headers=headers)
                if info_resp.status_code >= 400:
                    raise MediaError("fetch_failed", f"{info_resp.status_code}: {info_resp.text}")
                info = info_resp.json() or {}
                url, mime = str(info.get("url", "")), str(info.get("mime_type", ""))
                if not url.startswith("https://"):
                    raise MediaError("fetch_failed", "media url missing or not https")
                try:
                    if int(info.get("file_size") or 0) > max_bytes:
                        raise MediaError("too_large", f"declared {info.get('file_size')} bytes")
                except (TypeError, ValueError):
                    pass
                chunks: list[bytes] = []
                total = 0
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 300:
                        raise MediaError("fetch_failed", f"download status {resp.status_code}")
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaError("too_large", f"over {max_bytes} bytes while streaming")
                        chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise MediaError("fetch_failed", repr(exc)) from exc
        return b"".join(chunks), mime

    async def _upload_media(self, data: bytes, filename: str, mime: str) -> str:
        headers = {"Authorization": f"Bearer {self._token}"}  # httpx sets the multipart boundary
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self._base_url}/media",
                    headers=headers,
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (filename, data, mime)},
                )
        except httpx.HTTPError as exc:
            raise SendError(self.name, 0, repr(exc)) from exc
        if resp.status_code >= 400:
            raise SendError(self.name, resp.status_code, resp.text)
        media_id = (resp.json() or {}).get("id")
        if not media_id:
            raise SendError(self.name, 502, f"media upload returned no id: {resp.text}")
        return str(media_id)

    async def mark_as_read(self, msg: InboundMessage) -> None:
        await self._post(
            "/messages",
            {"messaging_product": "whatsapp", "status": "read", "message_id": msg.message_id},
        )

    async def send_typing(self, msg: InboundMessage) -> None:
        """Show "escribiendo..." to the user (also marks ``msg`` as read).

        WhatsApp hides it after ~25 s or as soon as we send a message, so the
        caller re-sends it while a long agent turn is still running.
        """
        await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": msg.message_id,
                "typing_indicator": {"type": "text"},
            },
        )

    async def _post(self, path: str, payload: dict) -> Optional[dict]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(f"{self._base_url}{path}", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SendError(self.name, 0, repr(exc)) from exc
        if resp.status_code >= 400:
            raise SendError(self.name, resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError:
            return None


def _wamid(resp: Optional[dict]) -> Optional[str]:
    """Outbound message id ("wamid....") from a /messages response, for status callbacks."""
    try:
        return str(resp["messages"][0]["id"]) if resp else None
    except (KeyError, IndexError, TypeError):
        return None


def parse_meta_payload(payload: dict) -> ParsedWebhook:
    out = ParsedWebhook()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field", "messages")
            value = change.get("value", {}) or {}

            if field in ACCOUNT_FIELDS:
                out.events.append(ProviderEvent(channel="meta", kind=field, data=value))
                continue

            for status in value.get("statuses", []):
                out.statuses.append(status)
                if status.get("status") == "failed":
                    out.events.append(
                        ProviderEvent(channel="meta", kind="message_failed", data=status)
                    )

            contacts = {
                c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                wa_id = msg.get("from", "")
                msg_type = msg.get("type", "unknown")
                text = ""
                reply_id = None
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    inter = msg.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title", "")
                    reply_id = reply.get("id")
                elif msg_type == "button":
                    text = msg.get("button", {}).get("text", "")

                media = None
                if msg_type in MEDIA_TYPES:
                    body = msg.get(msg_type, {}) or {}
                    text = body.get("caption", "") or ""
                    media = {"id": body.get("id", ""), "mime": body.get("mime_type", ""),
                             "filename": body.get("filename")}

                out.messages.append(
                    InboundMessage(
                        channel="meta",
                        user_id=normalize_user_id(wa_id),
                        reply_to=wa_id,
                        message_id=msg.get("id", ""),
                        text=text,
                        msg_type=msg_type,
                        profile_name=contacts.get(wa_id),
                        reply_id=reply_id,
                        media=media,
                    )
                )
    return out
