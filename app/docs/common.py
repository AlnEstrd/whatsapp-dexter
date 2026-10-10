"""Shared pieces for the portrait (Letter) documents: page footer + page x/y."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from .theme import BRAND, LINE, MUTED, NAVY, pdf_safe

PAGE_W, PAGE_H = LETTER
MARGIN = 54.0
FRAME_W = PAGE_W - 2 * MARGIN

MONTHS = {
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
           "octubre", "noviembre", "diciembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
}


def today_mx() -> dt.date:
    """Today in Mexico City (UTC-6 all year since 2022); the server clock is UTC."""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).date()


def fmt_date(d: dt.date, lang: str) -> str:
    if lang == "en":
        return f"{MONTHS['en'][d.month - 1]} {d.day}, {d.year}"
    return f"{d.day} de {MONTHS['es'][d.month - 1]} de {d.year}"


def slugify(text: str, max_len: int = 48) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return (s[:max_len].strip("-")) or "documento"


def numbered_canvas(footer_note: str = ""):
    """Canvas class that stamps 'page x / y' and the brand line on every page."""

    class _Numbered(canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total: int) -> None:
            self.setStrokeColor(LINE)
            self.setLineWidth(0.8)
            self.line(MARGIN, 40, PAGE_W - MARGIN, 40)
            self.setFont("Helvetica", 8)
            self.setFillColor(MUTED)
            self.drawString(MARGIN, 27, pdf_safe(BRAND))
            if footer_note:
                self.drawCentredString(PAGE_W / 2 + 40, 27, pdf_safe(footer_note))
            self.setFont("Helvetica-Bold", 8.5)
            self.setFillColor(NAVY)
            self.drawRightString(PAGE_W - MARGIN, 27, f"{self._pageNumber} / {total}")

    return _Numbered
