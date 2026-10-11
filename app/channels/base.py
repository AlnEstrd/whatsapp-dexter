"""Provider-agnostic messaging contract.

Everything above this layer (agent, escalation, store) only sees
InboundMessage and calls Channel.send_text(). Adding or swapping a provider
means writing one class that implements Channel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from fastapi import Request


def normalize_user_id(raw: str) -> str:
    """Stable, provider-independent user id: digits only, E.164 without '+'.

    Mexico gotcha: WhatsApp reports MX mobiles as 521XXXXXXXXXX on some
    providers and 52XXXXXXXXXX on others. Collapse to 52XXXXXXXXXX so the
    same person keeps the same conversation after a provider switch.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("521") and len(digits) == 13:
        digits = "52" + digits[3:]
    return digits


@dataclass
class InboundMessage:
    channel: str            # "twilio" | "meta"
    user_id: str            # normalized id (see normalize_user_id) — key for history
    reply_to: str           # provider-native address to send the reply to
    message_id: str         # provider message id (dedup / read receipts)
    text: str               # body ("" for unsupported types)
    msg_type: str = "text"
    profile_name: Optional[str] = None
    reply_id: Optional[str] = None  # id of the tapped interactive button / list row
    # Inbound file (image, document, audio...): {"id", "mime", "filename"}. ``text`` then holds the caption.
    media: Optional[dict] = None
    # True when ``text`` is the transcript of a voice note (app/voice.py), not typed.
    transcribed: bool = False


@dataclass
class ProviderEvent:
    """Non-message webhook events worth surfacing (e.g. Meta account review)."""
    channel: str
    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    events: list[ProviderEvent] = field(default_factory=list)


class Channel(Protocol):
    name: str

    async def verify_request(self, request: Request, raw_body: bytes) -> bool:
        """Authenticate the webhook call (provider signature)."""

    async def parse_webhook(self, request: Request, raw_body: bytes) -> ParsedWebhook:
        """Turn a raw webhook into normalized messages/events."""

    async def send_text(self, to: str, body: str) -> None:
        """Send a text message. Raise on failure (retries happen above)."""

    async def mark_as_read(self, msg: InboundMessage) -> None:
        """Best-effort read receipt; no-op where unsupported."""

    # Optional capabilities, looked up with getattr() by the caller:
    #   send_options(to, body, button, options)           interactive list (Meta)
    #   send_buttons(to, body, options)                   interactive reply buttons, max 3 (Meta)
    #   send_document(to, data, filename, caption="")     PDF as a document message (Meta)
    #   send_image(to, data, filename, caption="", mime)  image message (Meta)
    #   download_media(media_id, max_bytes) -> (bytes, mime)  fetch an inbound file (Meta)
    #   send_typing(msg)                                  "typing..." indicator, ~25 s (Meta)
