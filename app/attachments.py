"""Files attendees send (photos, screenshots, PDFs): download, validate, hand to Claude.

Policy, on purpose conservative:
- Only images (JPEG, PNG, WebP, GIF) and PDFs; no voice notes, video or stickers.
- Size caps (image 4 MB, PDF 8 MB, PDF 20 pages) are enforced on the declared size
  *and* while streaming, so a lying header can't make us buffer a huge file.
- The real type is sniffed from the bytes; the type the sender declared is not trusted.
- Nothing is written to disk. The file lives in memory for the one turn it is shown to
  Claude (see ``Agent.handle_message``) and is then dropped from the history.
- Whatever is inside a file is data, never instructions (the system prompt says so).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Optional

from .channels.errors import MediaError

logger = logging.getLogger("attachments")

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 20
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class Rejected(Exception):
    """The file can't be used. ``reason``: unsupported | too_large | too_many_pages | unreadable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Attachment:
    kind: str          # "image" | "pdf"
    media_type: str    # sniffed from the bytes
    data: bytes
    filename: str = ""


def classify(mime: str) -> Optional[str]:
    m = (mime or "").split(";")[0].strip().lower()
    if m in IMAGE_TYPES:
        return "image"
    if m == "application/pdf":
        return "pdf"
    return None


def sniff(data: bytes) -> Optional[str]:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pdf_pages(data: bytes) -> Optional[int]:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:  # noqa: BLE001  (corrupt, encrypted, not a PDF)
        return None


async def load(channel, media: dict) -> Attachment:
    """Download and validate one inbound file. Raises ``Rejected`` (user-facing reason)
    or ``MediaError`` (download failed)."""
    kind = classify(media.get("mime", ""))
    if kind is None:
        raise Rejected("unsupported")
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_IMAGE_BYTES
    try:
        data, _declared = await channel.download_media(media["id"], limit)
    except MediaError as exc:
        if exc.reason == "too_large":
            raise Rejected("too_large") from exc
        raise
    real = sniff(data)
    if real is None or classify(real) != kind:
        raise Rejected("unreadable")
    if kind == "pdf":
        pages = await asyncio.to_thread(_pdf_pages, data)
        if pages is None or pages < 1:
            raise Rejected("unreadable")
        if pages > MAX_PDF_PAGES:
            raise Rejected("too_many_pages")
    return Attachment(kind=kind, media_type=real, data=data, filename=(media.get("filename") or "")[:120])


def content_blocks(att: Attachment) -> list[dict]:
    """Anthropic Messages API content block for this file."""
    block = "image" if att.kind == "image" else "document"
    return [{"type": block, "source": {"type": "base64", "media_type": att.media_type,
                                        "data": base64.standard_b64encode(att.data).decode()}}]
