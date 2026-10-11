"""Channel-neutral outgoing message: text plus optional choices.

Channels that support WhatsApp interactive lists (Meta) render ``options``
as a list message; others send ``text`` alone (it already numbers the
options, so users can reply 1-4).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Option:
    id: str           # <= 200 chars, comes back as the reply id
    title: str        # <= 24 chars
    description: str = ""  # <= 72 chars


@dataclass
class Outgoing:
    text: str
    options: Optional[list[Option]] = None
    button: str = "Opciones"  # list-message button label, <= 20 chars
    # Up to 3 short options (titles <= 20 chars) as WhatsApp reply buttons instead of a list.
    buttons: bool = False
    # Body for the interactive message when ``text`` (the plain-text fallback) also numbers the options.
    interactive_text: Optional[str] = None
