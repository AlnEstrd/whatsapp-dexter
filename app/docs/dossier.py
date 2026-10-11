"""Dossier: an on-request entry (Respuesta + Detalle, live status included) as a multi-page PDF.

Same look as the topic one-pager (navy band, amber rule, page x/y footer) but no
length limit. The entry is light Markdown written by Alan: ``###`` headings,
``-`` bullets (two-space indent for a sub-level), ``1.`` numbered items,
``*bold*`` / ``**bold**``, ``_italic_`` and ``code``. Everything is escaped first,
so nothing in the file can inject PDF markup.
"""
from __future__ import annotations

import datetime as dt
import io
import re

from reportlab.lib import colors
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, NextPageTemplate, PageTemplate,
                                Paragraph, Spacer)

from .common import FRAME_W, MARGIN, PAGE_H, PAGE_W, fmt_date, numbered_canvas, today_mx
from .theme import AMBER, NAVY, PAPER, S, esc, pdf_safe, style

TXT = {
    "es": {"kicker": "ESTRADA AI LAB · EXPEDIENTE", "generated": "Generado el",
           "note": "Información compartida a petición. El estado se generó al momento de crear este PDF."},
    "en": {"kicker": "ESTRADA AI LAB · DOSSIER", "generated": "Generated on",
           "note": "Information shared on request. The status was generated when this PDF was created."},
}

SUB_BULLET = style("sub_bullet", leftIndent=28, bulletIndent=16, spaceAfter=2, fontSize=10, leading=14)


def inline(text: str) -> str:
    """Escape, then turn the light Markdown into reportlab tags."""
    s = esc(text.strip())
    s = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w/])_(?!\s)(.+?)(?<!\s)_(?![\w/])", r"<i>\1</i>", s)
    return s


def blocks(markdown: str) -> list:
    """Flowables for a section of light Markdown."""
    out: list = []
    para: list[str] = []

    def flush():
        if para:
            out.append(Paragraph(inline(" ".join(para)), S["body"]))
            para.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if (m := re.match(r"^#{2,4}\s+(.*)$", line)):
            flush()
            out += [CondPageBreak(90), Paragraph(inline(m.group(1)), S["h2"])]
        elif (m := re.match(r"^(\s*)[-*•]\s+(.*)$", line)):
            flush()
            sub = len(m.group(1).replace("\t", "  ")) >= 2
            out.append(Paragraph(inline(m.group(2)), SUB_BULLET if sub else S["bullet"],
                                 bulletText="–" if sub else "•"))
        elif (m := re.match(r"^(\s*)(\d{1,2})[.)]\s+(.*)$", line)):
            flush()
            sub = len(m.group(1)) >= 2
            out.append(Paragraph(inline(m.group(3)), SUB_BULLET if sub else S["bullet"],
                                 bulletText=f"{m.group(2)}."))
        else:
            para.append(line.strip())
    flush()
    return out


def build_dossier_pdf(title: str, intro: str, detail: str, lang: str = "es",
                      today: dt.date | None = None) -> bytes:
    lang = "en" if lang == "en" else "es"
    tx = TXT[lang]
    today = today or today_mx()
    band_h = 118

    def first_page(c, doc):
        c.setFillColor(PAPER)
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)
        c.setFillColor(AMBER)
        c.rect(0, PAGE_H - band_h, PAGE_W, 5, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(MARGIN, PAGE_H - 34, pdf_safe(tx["kicker"]))
        p = Paragraph(esc(title), style("dt", fontName="Helvetica-Bold", fontSize=22, leading=26,
                                         textColor=colors.white))
        _, h = p.wrap(FRAME_W, 200)
        p.drawOn(c, MARGIN, PAGE_H - 44 - h)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#C9D3E3"))
        c.drawString(MARGIN, PAGE_H - band_h + 14, pdf_safe(f"{tx['generated']} {fmt_date(today, lang)}"))

    def later_page(c, doc):
        c.setFillColor(PAPER)
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=MARGIN, bottomMargin=54, title=pdf_safe(title),
                          author="Alan Estrada", subject="Estrada AI Lab")
    f1 = Frame(MARGIN, 54, FRAME_W, PAGE_H - band_h - 54 - 18, id="f1", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    f2 = Frame(MARGIN, 54, FRAME_W, PAGE_H - 54 - MARGIN, id="f2", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate("first", [f1], onPage=first_page),
                          PageTemplate("later", [f2], onPage=later_page)])

    story: list = [NextPageTemplate("later")]
    story += blocks(intro)
    story += [Spacer(1, 6)] + blocks(detail)
    story += [Spacer(1, 12), Paragraph(esc(tx["note"]), S["small"])]
    doc.build(story, canvasmaker=numbered_canvas())
    return buf.getvalue()
