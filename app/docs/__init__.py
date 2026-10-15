"""PDF documents the bot can send over WhatsApp.

    make_deck(key)          the talk's slides: whole deck or one slide (Spanish)
    make_slide_image(key)   one slide as a PNG (inline preview in WhatsApp)
    make_topic(spec)        a one-pager on a talk topic, content written by the agent
    make_assessment(result) multipage report of a finished AI-maturity assessment
    make_dossier(entry)     an on-request entry (with live status) as a multipage PDF

Every function returns a ``Doc`` (bytes + filename + caption). Rendering is
pure Python (reportlab, built-in fonts): nothing to install on the server.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from ..flows import quiz
from . import assessment as _assessment
from . import dossier as _dossier
from . import slides as _slides
from . import topic as _topic
from .common import slugify


@dataclass
class Doc:
    data: bytes
    filename: str
    caption: str
    mime: str = "application/pdf"
    kind: str = ""        # slides | topic_pdf | report (metrics event on delivery)
    slide: str = ""       # slide id or "all" (kind=slides)

    @property
    def pages(self) -> int:
        """Page count of a PDF (1 for an image); a cheap scan, no rendering."""
        if not self.mime.endswith("pdf"):
            return 1
        return len(re.findall(rb"/Type\s*/Page(?![s\w])", self.data)) or 1


@lru_cache(maxsize=32)
def _deck_bytes(key: Optional[str]) -> bytes:
    return _slides.build_slides_pdf(key)  # static content: render once per process


def make_deck(slide: Optional[str] = None, lang: str = "es") -> Doc:
    """``slide``: None/'all' for the whole deck, else a slide id (see slides.resolve).

    Raises ValueError for an unknown slide.
    """
    key = _slides.resolve(slide)
    return Doc(_deck_bytes(key), _slides.filename(key), _slides.caption(key, lang),
               kind="slides", slide=key or "all")


@lru_cache(maxsize=32)
def _slide_png(key: str) -> bytes:
    return _slides.render_png(key)  # static content: render once per process


def make_slide_image(slide: str, lang: str = "es") -> Doc:
    """One slide as a PNG image. Raises ValueError for an unknown slide or 'all'."""
    key = _slides.resolve(slide)
    if key is None:
        raise ValueError(f"a single slide id is required for an image ({', '.join(_slides.BY_ID)})")
    return Doc(_slide_png(key), _slides.filename(key, "png"), _slides.image_caption(key, lang), "image/png",
               kind="slides", slide=key)


def make_topic(spec: dict) -> Doc:
    """Raises ValueError (with a message meant for the agent) when the spec is unusable."""
    clean = _topic.validate(spec)
    data = _topic.build_topic_pdf(clean)
    return Doc(data, f"{slugify(clean['title'])}.pdf", f"📄 {clean['title']}", kind="topic_pdf")


def make_assessment(result: quiz.Result, name: Optional[str] = None) -> Doc:
    lang = result.lang if result.lang in ("es", "en") else "es"
    level = quiz.LEVELS[result.level][lang][0]
    if lang == "es":
        caption = f"📊 Tu reporte del diagnóstico de madurez en IA: {level} ({result.average:.1f}/3)."
        filename = "Diagnostico-madurez-IA.pdf"
    else:
        caption = f"📊 Your AI maturity report: {level} ({result.average:.1f}/3)."
        filename = "AI-maturity-report.pdf"
    return Doc(_assessment.build_assessment_pdf(result, name), filename, caption, kind="report")


def make_dossier(entry, lang: str = "es") -> Doc:
    """``entry``: an on_request.Entry. Rendered now, so the live status is current."""
    from .. import on_request
    lang = "en" if lang == "en" else "es"
    data = _dossier.build_dossier_pdf(entry.title, on_request.response_for(entry, lang),
                                      on_request.detail_for(entry, lang), lang)
    caption = "📁 El expediente completo." if lang == "es" else "📁 The full dossier."
    return Doc(data, f"{slugify(entry.title)}.pdf", caption)


slide_index_text = _slides.index_text

__all__ = ["Doc", "make_deck", "make_slide_image", "make_topic", "make_assessment", "make_dossier",
           "slide_index_text"]
