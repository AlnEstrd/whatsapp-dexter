"""Reply shaping for the text the bot sends: the greeting strip and the length backstop.

WhatsApp formatting (Markdown -> WhatsApp, tables, the 4,096-character limit) lives in
``app/format_whatsapp.py``; ``to_whatsapp`` here is kept for callers that want the text only.
"""
from __future__ import annotations

import logging
import re

from . import format_whatsapp

logger = logging.getLogger("formatting")

# "¡Hola!", "Hola, Ana.", "Hi there!" ... at the very start of a reply. The greeting needs
# trailing punctuation so a sentence that merely starts with the word is left alone.
_GREETING = re.compile(
    r"^\s*[¡!]?\s*(?:hola|hi|hello|hey|buenas(?:\s+tardes|\s+noches)?|buen(?:o|a)s?\s+d[ií]as?)"
    r"(?:[\s,]+[^\s,.!?¡]{1,20})?\s*[,.!]+[\s👋]*",
    re.I,
)


def strip_greeting(text: str) -> str:
    """Drop a leading greeting from an agent reply. The bot already greets the user (the
    welcome menu or the new-chat intro), so the agent's own "¡Hola!" would be a second one."""
    if not text:
        return text or ""
    rest = _GREETING.sub("", text, count=1).lstrip()
    if not rest or rest == text.lstrip():
        return text
    return rest[0].upper() + rest[1:]


def to_whatsapp(text: str) -> str:
    """The WhatsApp text only (see format_whatsapp.sanitize for the counters)."""
    return format_whatsapp.sanitize(text)[0]


# Where a reply may be cut: after a blank line (a paragraph), or after a sentence or a line.
_PARAGRAPH_END = re.compile(r"(?<=\S)(?=\s*\n\s*\n)")
_SENTENCE = re.compile(r"(?<=[.!?…])(?=\s|$)|(?<=\S)(?=\n)")


def _last_fit(text: str, cap: int, offsets: list[int]) -> int:
    """The last cut offset whose text before it has at most ``cap`` words (0 if none)."""
    cut = 0
    for off in offsets:
        if len(text[:off].split()) > cap:
            break
        cut = off
    return cut


def _block_cuts(text: str) -> tuple[list[int], list[int]]:
    """Where a reply may be cut, block-aware: after any block (never after a header, which stays with
    its paragraph), and inside a prose block after a sentence (never inside a list or code block)."""
    blocks = format_whatsapp.blocks(text)
    # The end of the text is not a cut point (nothing is cut): only the ends of blocks that are followed by another.
    paragraphs = sorted(b.end for b in blocks[:-1] if b.kind != "header")
    sentences: list[int] = []
    for b in blocks:
        if b.kind != "prose":
            continue
        first_nl = text.find("\n", b.start, b.end)
        header_first = first_nl != -1 and format_whatsapp.is_header(text[b.start:first_nl])
        for m in _SENTENCE.finditer(text, b.start, b.end):
            if header_first and m.start() <= first_nl:
                continue  # the header line stays with the paragraph under it
            sentences.append(m.start())
    return paragraphs, sorted(sentences)


def _cut_at(text: str, cap: int) -> int:
    """The cut for cap_words: block-aware with the sanitizer on (a list or code block is never cut inside);
    the paragraph and sentence cut from before otherwise, or when the block-aware cut fails."""
    if format_whatsapp.enabled():
        try:
            paragraphs, sentences = _block_cuts(text)
            return _last_fit(text, cap, paragraphs) or _last_fit(text, cap, sentences)
        except Exception:  # noqa: BLE001  (a formatting bug must never block a reply)
            logger.exception("block-aware trim failed; cutting by paragraph and sentence")
    return _last_fit(text, cap, [m.start() for m in _PARAGRAPH_END.finditer(text)]) or \
        _last_fit(text, cap, [m.start() for m in _SENTENCE.finditer(text)])


def cap_words(text: str, cap: int, offer: str, truncated: bool = False) -> tuple[str, bool]:
    """Keep a reply within ``cap`` words: cut at the last complete paragraph under the cap (or
    the last sentence, if the first paragraph is already too long), then add ``offer``, one
    question offering to go deeper. Returns (text, trimmed).

    Only the final text is capped; a tool's input (a PDF's content) never is. ``truncated`` means
    the model hit max_tokens, so its last sentence is incomplete and is dropped with the rest.
    Text under the cap is returned as written, and nothing is ever cut mid-sentence. With the
    sanitizer on, a list or code block is never cut inside (its paragraph is the smallest unit)."""
    text = (text or "").strip()
    if not truncated and len(text.split()) <= cap:
        return text, False
    cut = _cut_at(text, cap)
    if not cut:  # not even one whole block or sentence fits
        return ("", True) if truncated else (text, False)
    return f"{text[:cut].rstrip()}\n\n{offer}", True
