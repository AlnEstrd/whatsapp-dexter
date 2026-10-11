"""Turn model-flavoured Markdown into WhatsApp formatting.

WhatsApp uses *bold* (one asterisk), _italic_, ~strike~ and ``` for code. A model
that writes **bold** leaves visible asterisks, so every agent reply goes through
``to_whatsapp`` right before it is sent. The function is idempotent and leaves
already-correct WhatsApp text (and ``` code blocks) untouched.
"""
from __future__ import annotations

import re

_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BOLD_ITALIC = re.compile(r"\*{3}(?=\S)([^\n]+?)(?<=\S)\*{3}")
_BOLD = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_BOLD_UNDER = re.compile(r"__(?=\S)([^\n]+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_STRAY = re.compile(r"\*{2,}")
_BLANKS = re.compile(r"\n{3,}")


def _heading(m: re.Match) -> str:
    title = m.group(1).strip("* ").strip()
    return f"*{title}*" if title else ""


def _convert(text: str) -> str:
    text = _HR.sub("", text)
    text = _HEADING.sub(_heading, text)
    text = _BOLD_ITALIC.sub(r"*\1*", text)
    text = _BOLD.sub(r"*\1*", text)
    text = _BOLD_UNDER.sub(r"*\1*", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _BULLET.sub("• ", text)
    text = _LINK.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)}: {m.group(2)}", text)
    return _STRAY.sub("", text)


def to_whatsapp(text: str) -> str:
    if not text:
        return text or ""
    parts = text.split("```")
    # even indexes are outside code fences; odd ones are code and stay as written
    parts = [_convert(p) if i % 2 == 0 else p for i, p in enumerate(parts)]
    return _BLANKS.sub("\n\n", "```".join(parts)).strip()


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
