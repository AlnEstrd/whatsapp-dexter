"""Topic one-pager: a short PDF on one subject of the talk.

The *content* comes from the agent (it writes the spec from the knowledge
base); the *layout* is deterministic. ``validate`` normalizes and clips the
spec so a chatty model can't produce a 6-page document, and every string goes
through ``esc`` so model output can't inject PDF markup.
"""
from __future__ import annotations

import datetime as dt
import io
from typing import Any

from reportlab.lib import colors
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, NextPageTemplate, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from .common import FRAME_W, MARGIN, PAGE_H, PAGE_W, fmt_date, numbered_canvas, today_mx
from .theme import AMBER, AMBER_DARK, CARD, LINE, NAVY, PAPER, S, esc, pdf_safe, style

LIMITS = {"title": 90, "summary": 520, "heading": 80, "paragraph": 700, "bullet": 300,
          "fig_value": 14, "fig_label": 110, "fig_source": 80, "cell": 90, "header": 40,
          "source": 170, "sections": 5, "paragraphs": 3, "bullets": 7, "figures": 4,
          "rows": 7, "cols": 4, "sources": 8}

MAX_WORDS = 520   # about two pages; longer content makes the agent shorten it and retry

TXT = {
    "es": {"kicker": "TEMA DE LA PLÁTICA · GOBERNANZA DE IA", "sources": "Fuentes",
           "note": "Resumen basado en la base de conocimiento de la plática; revisa las fuentes antes de citar.",
           "generated": "Generado el"},
    "en": {"kicker": "TALK TOPIC · AI GOVERNANCE", "sources": "Sources",
           "note": "Summary based on the talk's knowledge base; check the sources before citing.",
           "generated": "Generated on"},
}


def _clip(text: Any, n: int) -> str:
    s = " ".join(pdf_safe(text).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def validate(spec: Any) -> dict:
    """Return a clean spec or raise ValueError with a message the model can act on."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    title = _clip(spec.get("title"), LIMITS["title"])
    if not title:
        raise ValueError("title is required")
    lang = "en" if spec.get("language") == "en" else "es"

    sections = []
    for sec in _list(spec.get("sections"))[: LIMITS["sections"]]:
        if not isinstance(sec, dict):
            continue
        heading = _clip(sec.get("heading"), LIMITS["heading"])
        paragraphs = [_clip(p, LIMITS["paragraph"]) for p in _list(sec.get("paragraphs"))[: LIMITS["paragraphs"]]]
        bullets = [_clip(b, LIMITS["bullet"]) for b in _list(sec.get("bullets"))[: LIMITS["bullets"]]]
        paragraphs, bullets = [p for p in paragraphs if p], [b for b in bullets if b]
        if heading and (paragraphs or bullets):
            sections.append({"heading": heading, "paragraphs": paragraphs, "bullets": bullets})
    if not sections:
        raise ValueError("at least one section with a heading and paragraphs or bullets is required")

    figures = []
    for fig in _list(spec.get("figures"))[: LIMITS["figures"]]:
        if isinstance(fig, dict) and fig.get("value") and fig.get("label"):
            figures.append({"value": _clip(fig["value"], LIMITS["fig_value"]),
                            "label": _clip(fig["label"], LIMITS["fig_label"]),
                            "source": _clip(fig.get("source", ""), LIMITS["fig_source"])})

    table = None
    t = spec.get("table")
    if isinstance(t, dict) and _list(t.get("headers")) and _list(t.get("rows")):
        headers = [_clip(h, LIMITS["header"]) for h in t["headers"][: LIMITS["cols"]]]
        rows = []
        for r in t["rows"][: LIMITS["rows"]]:
            if isinstance(r, list):
                cells = [_clip(v, LIMITS["cell"]) for v in r[: len(headers)]]
                rows.append(cells + [""] * (len(headers) - len(cells)))
        if rows:
            table = {"headers": headers, "rows": rows}

    sources = [_clip(x, LIMITS["source"]) for x in _list(spec.get("sources"))[: LIMITS["sources"]]]
    clean = {
        "language": lang, "title": title, "summary": _clip(spec.get("summary"), LIMITS["summary"]),
        "sections": sections, "figures": figures, "table": table,
        "sources": [x for x in sources if x],
    }
    words = _word_count(clean)
    if words > MAX_WORDS:
        raise ValueError(f"content is too long ({words} words, max {MAX_WORDS}): shorten it to about "
                         "350 words in total (fewer or shorter sections) and call the tool again")
    return clean


def _word_count(value: Any) -> int:
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, dict):
        return sum(_word_count(v) for k, v in value.items() if k != "language")
    if isinstance(value, list):
        return sum(_word_count(v) for v in value)
    return 0


def _figure_row(figures: list[dict]) -> Table:
    n = len(figures)
    gap = 8
    w = (FRAME_W - gap * (n - 1)) / n
    cells, widths = [], []
    for i, f in enumerate(figures):
        inner = [Paragraph(esc(f["value"]), S["fig_v"]), Paragraph(esc(f["label"]), S["fig_l"])]
        if f["source"]:
            inner.append(Paragraph(esc(f["source"]), S["fig_s"]))
        cells.append(inner)
        widths.append(w)
        if i < n - 1:
            cells.append("")
            widths.append(gap)
    t = Table([cells], colWidths=widths)
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i in range(0, len(cells), 2):
        cmds += [("BACKGROUND", (i, 0), (i, 0), CARD), ("BOX", (i, 0), (i, 0), 0.8, LINE),
                 ("LINEBEFORE", (i, 0), (i, 0), 3, AMBER),
                 ("LEFTPADDING", (i, 0), (i, 0), 10), ("RIGHTPADDING", (i, 0), (i, 0), 8),
                 ("TOPPADDING", (i, 0), (i, 0), 8), ("BOTTOMPADDING", (i, 0), (i, 0), 8)]
    t.setStyle(TableStyle(cmds))
    return t


def _table(tbl: dict) -> Table:
    n = len(tbl["headers"])
    data = [[Paragraph(esc(h), S["th"]) for h in tbl["headers"]]]
    for r in tbl["rows"]:
        data.append([Paragraph(esc(v), S["cell_b"] if j == 0 else S["cell"]) for j, v in enumerate(r)])
    t = Table(data, colWidths=[FRAME_W / n] * n, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [CARD, PAPER]), ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def build_topic_pdf(spec: dict, today: dt.date | None = None) -> bytes:
    sp = validate(spec)
    lang = sp["language"]
    tx = TXT[lang]
    today = today or today_mx()
    band_h = 128

    def first_page(c, doc):
        c.setFillColor(PAPER)
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)
        c.setFillColor(AMBER)
        c.rect(0, PAGE_H - band_h, PAGE_W, 5, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(AMBER)
        c.drawString(MARGIN, PAGE_H - 34, pdf_safe(tx["kicker"]))
        p = Paragraph(esc(sp["title"]), style("tt", fontName="Helvetica-Bold", fontSize=22, leading=26,
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
                          topMargin=MARGIN, bottomMargin=54, title=pdf_safe(sp["title"]),
                          author="Alan Estrada", subject="Gobernanza de IA")
    f1 = Frame(MARGIN, 54, FRAME_W, PAGE_H - band_h - 54 - 18, id="f1", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    f2 = Frame(MARGIN, 54, FRAME_W, PAGE_H - 54 - MARGIN, id="f2", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate("first", [f1], onPage=first_page),
                          PageTemplate("later", [f2], onPage=later_page)])

    story: list = [NextPageTemplate("later")]
    if sp["summary"]:
        story.append(Paragraph(esc(sp["summary"]), S["lead"]))
    if sp["figures"]:
        story += [_figure_row(sp["figures"]), Spacer(1, 6)]
    for sec in sp["sections"]:
        block: list = [Paragraph(esc(sec["heading"]), S["h2"])]
        block += [Paragraph(esc(p), S["body"]) for p in sec["paragraphs"]]
        block += [Paragraph(esc(b), S["bullet"], bulletText="•") for b in sec["bullets"]]
        story.append(KeepTogether(block[:3]))
        story += block[3:]
    if sp["table"]:
        story += [Spacer(1, 8), _table(sp["table"])]
    if sp["sources"]:
        src = [Paragraph(esc(tx["sources"]), S["h3"])]
        src += [Paragraph(esc(x), S["small"], bulletText="•") for x in sp["sources"]]
        story += [Spacer(1, 12), KeepTogether(src)]
    story += [Spacer(1, 10), Paragraph(esc(tx["note"]), S["small"])]

    doc.build(story, canvasmaker=numbered_canvas())
    return buf.getvalue()
