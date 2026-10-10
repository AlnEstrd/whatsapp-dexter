"""Shared look for every generated PDF: palette, text styles, text sanitizing.

Fonts are the PDF built-ins (Helvetica), so nothing needs to be installed on
the server. They cover Spanish accents, ñ, ¿ ¡, curly quotes and dashes
(WinAnsi). Anything outside that set (arrows, ≥, emoji...) is mapped to a plain
equivalent or dropped by ``pdf_safe`` instead of rendering as a black box.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle

NAVY = colors.HexColor("#0B1F3A")
AMBER = colors.HexColor("#F2A541")
AMBER_DARK = colors.HexColor("#B7710F")
PAPER = colors.HexColor("#FBF8F2")
CARD = colors.HexColor("#FFFFFF")
INK = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
LINE = colors.HexColor("#E3DDCF")
TEAL = colors.HexColor("#0D9488")
GREEN = colors.HexColor("#15803D")
ORANGE = colors.HexColor("#D97706")
SLATE = colors.HexColor("#64748B")

# Level colors for the maturity assessment (L0..L3).
LEVEL_COLORS = {0: SLATE, 1: ORANGE, 2: TEAL, 3: GREEN}

BRAND = "Gobernanza de IA · Alan Estrada · alanestrada.space"

_REPLACE = {
    "→": "->", "←": "<-", "↔": "<->", "≥": ">=", "≤": "<=", "≈": "~", "≠": "!=",
    "✓": "-", "✔": "-", "✗": "x", "●": "•", "○": "o", "▪": "•", "▶": ">", "★": "*",
    " ": " ", "​": "", "‑": "-", "−": "-",
}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def pdf_safe(text: object) -> str:
    """Make arbitrary text printable with the built-in fonts (no markup escaping)."""
    s = "" if text is None else str(text)
    for k, v in _REPLACE.items():
        s = s.replace(k, v)
    s = _CTRL.sub("", s)
    return s.encode("cp1252", errors="ignore").decode("cp1252")


def esc(text: object) -> str:
    """Sanitize + escape for use inside a reportlab Paragraph (untrusted text)."""
    return escape(pdf_safe(text))


def style(name: str, **kw) -> ParagraphStyle:
    base = dict(fontName="Helvetica", fontSize=10.5, leading=15, textColor=INK, alignment=TA_LEFT)
    base.update(kw)
    return ParagraphStyle(name, **base)


# --- Portrait documents (topic one-pager, assessment) ------------------------
S = {
    "h1": style("h1", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=NAVY, spaceAfter=6),
    "h2": style("h2", fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=NAVY,
                spaceBefore=12, spaceAfter=5),
    "h3": style("h3", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=NAVY, spaceAfter=2),
    "body": style("body", spaceAfter=5),
    "lead": style("lead", fontSize=12, leading=17, textColor=INK, spaceAfter=8),
    "small": style("small", fontSize=8.5, leading=11.5, textColor=MUTED),
    "bullet": style("bullet", leftIndent=14, bulletIndent=2, spaceAfter=3),
    "cell": style("cell", fontSize=9.5, leading=12.5),
    "cell_b": style("cell_b", fontName="Helvetica-Bold", fontSize=9.5, leading=12.5, textColor=NAVY),
    "th": style("th", fontName="Helvetica-Bold", fontSize=9.5, leading=12.5, textColor=colors.white),
    "fig_v": style("fig_v", fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=NAVY),
    "fig_l": style("fig_l", fontSize=9.5, leading=12.5),
    "fig_s": style("fig_s", fontSize=8, leading=10.5, textColor=MUTED),
}
