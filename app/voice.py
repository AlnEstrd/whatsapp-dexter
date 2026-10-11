"""Voice notes from attendees: download, validate, transcribe; the text then goes through the
normal flow (menu, quiz, agent) as if the person had typed it.

Claude doesn't take audio, so a speech-to-text service turns the note into text first. Any
OpenAI-compatible ``/audio/transcriptions`` endpoint works (OpenAI, Groq, a self-hosted Whisper):
set TRANSCRIBE_API_KEY (and TRANSCRIBE_BASE_URL / TRANSCRIBE_MODEL for a non-OpenAI service).
Without a key, voice notes get the "photos and PDFs only" reply, as before.

Same policy as ``attachments``: size cap enforced on the declared size and while streaming,
the real type sniffed from the bytes, nothing written to disk. The audio is sent to the
transcription service and dropped; only the transcript is kept, in the chat history.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .attachments import Rejected
from .channels.base import InboundMessage
from .channels.errors import MediaError
from .config import get_settings

logger = logging.getLogger("voice")

MAX_AUDIO_BYTES = 4 * 1024 * 1024  # a WhatsApp voice note (Opus) is ~1 MB per 5 minutes
MAX_TRANSCRIPT_CHARS = 6000
# Domain words the recognizer tends to miss; a hint, not a constraint.
VOCAB_HINT = ("Dexter, Estrada Lab, Alan Estrada, IA, gobernanza de IA, ownership, sponsor, champions, "
              "yield, KPIs, ERP, CRM, ChatGPT, Copilot, Claude, agentes.")


class TranscriptionError(Exception):
    pass


def enabled() -> bool:
    s = get_settings()
    return s.voice_enabled and bool(s.transcribe_api_key)


def is_voice(msg: InboundMessage) -> bool:
    """A voice note or audio file (Meta sends both as type "audio")."""
    return msg.media is not None and msg.msg_type == "audio"


def sniff(data: bytes) -> Optional[tuple[str, str]]:
    """(mime, extension) of a format the transcription API accepts, from the bytes."""
    if data.startswith(b"OggS"):
        return "audio/ogg", "ogg"           # WhatsApp voice notes (Opus)
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg", "mp3"
    if data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm", "webm"
    return None                             # AMR, AAC (ADTS) and others: not accepted


async def transcribe(data: bytes, mime: str, ext: str) -> str:
    s = get_settings()
    url = s.transcribe_base_url.rstrip("/") + "/audio/transcriptions"
    form = {"model": s.transcribe_model, "response_format": "json", "prompt": VOCAB_HINT}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {s.transcribe_api_key}"},
                                     data=form, files={"file": (f"voice.{ext}", data, mime)})
    except httpx.HTTPError as exc:
        raise TranscriptionError(repr(exc)) from exc
    if resp.status_code >= 400:
        raise TranscriptionError(f"{resp.status_code}: {resp.text[:300]}")
    try:
        text = str((resp.json() or {}).get("text") or "")
    except ValueError as exc:
        raise TranscriptionError(f"bad response: {resp.text[:300]}") from exc
    return " ".join(text.split())[:MAX_TRANSCRIPT_CHARS]


async def load_transcript(channel, media: dict) -> str:
    """Download one voice note and return its transcript ("" = nothing intelligible).
    Raises ``Rejected`` (too_large | unsupported), ``MediaError`` or ``TranscriptionError``."""
    try:
        data, _declared = await channel.download_media(media["id"], MAX_AUDIO_BYTES)
    except MediaError as exc:
        if exc.reason == "too_large":
            raise Rejected("too_large") from exc
        raise
    kind = sniff(data)
    if kind is None:
        raise Rejected("unsupported")
    return await transcribe(data, *kind)
