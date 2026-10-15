"""Slide PDFs: the whole deck or one slide, 16:9.

The talk's deck is the presentation itself: the 12 pages of
``assets/gobernanza-quien-da-la-cara.pdf``, exported from the ``.pptx`` next to
it. To update it, edit the .pptx, export it again to PDF::

    soffice --headless --convert-to pdf --outdir app/docs/assets app/docs/assets/gobernanza-quien-da-la-cara.pptx

and, if the page titles change, ``_DECK_SHORT`` (the names shown in the index).
Annex pages (A, B, 90 days) are drawn here with reportlab, one page per slide,
with a hard overflow check: a slide whose content does not fit raises
``ValueError`` at build time (a test builds every slide, so it can't ship broken).

Spanish only: the talk is given in Spanish.
"""
from __future__ import annotations

import io
import re
from itertools import groupby
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from .theme import (AMBER, AMBER_DARK, BRAND, CARD, INK, LINE, MUTED, NAVY, PAPER, esc,
                    pdf_safe, style)

W, H = 960.0, 540.0
MX = 60.0
CW = W - 2 * MX
BOTTOM = 86.0   # content must end above this; sources + footer live below
GAP = 16.0

P = {
    "kicker": style("k", fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=AMBER_DARK),
    "title": style("t", fontName="Helvetica-Bold", fontSize=27, leading=32, textColor=NAVY),
    "subtitle": style("st", fontSize=15, leading=20, textColor=MUTED),
    "hero": style("hero", fontSize=30, leading=40, textColor=NAVY),
    "bullet": style("b", fontSize=16, leading=22),
    "row_l": style("rl", fontName="Helvetica-Bold", fontSize=15, leading=20, textColor=AMBER_DARK),
    "row_t": style("rt", fontSize=14, leading=19),
    "stat_l": style("sl", fontSize=13.5, leading=18),
    "stat_s": style("ss", fontSize=10, leading=13, textColor=MUTED),
    "quote": style("q", fontName="Helvetica-Oblique", fontSize=16, leading=22, textColor=NAVY),
    "quote_a": style("qa", fontSize=11, leading=14, textColor=MUTED),
    "note": style("n", fontName="Helvetica-Oblique", fontSize=13, leading=18, textColor=MUTED),
    "sources": style("src", fontSize=9, leading=11.5, textColor=MUTED),
    "th": style("th", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=colors.white),
    "td": style("td", fontSize=12.5, leading=16),
    "td_b": style("tdb", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=NAVY),
    "step_t": style("stt", fontName="Helvetica-Bold", fontSize=14.5, leading=18, textColor=NAVY),
    "step_x": style("stx", fontSize=13, leading=17),
    "chip": style("c", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=NAVY),
}

# The talk's deck: the 12 pages of the PDF rendered from the .pptx in assets/ (16:9).
# "page" is the 0-based index in that PDF; "id" is the slide number the bot shows.
DECK_FILE = Path(__file__).parent / "assets" / "gobernanza-quien-da-la-cara.pdf"
_DECK_SHORT = [
    "Portada: ¿quién da la cara?",
    "1962: la IBM calculó; Katherine Johnson verificó",
    "La máquina calculó; una persona dio la cara",
    "Un patrón de 230 años",
    "Rol = responsabilidades + actividades",
    "El problema: 74% sin valor",
    "Dónde está el valor: 10-20-70",
    "¿Quién da la cara en tu área?",
    "Ejemplo: finanzas",
    "Autoliderazgo: Dorothy Vaughan",
    "Tres preguntas para el lunes",
    "Cierre: los computers somos todos",
]
DECK: list[dict] = [
    {"id": str(n), "label": f"{n} / {len(_DECK_SHORT)}", "short": short, "page": n - 1}
    for n, short in enumerate(_DECK_SHORT, 1)
]

# Annex pages are drawn here (blocks below). They are reference material after the talk.
ANNEXES: list[dict] = [
    {
        "id": "A", "label": "Anexo A", "short": "Anexo A: gestión del cambio (OCM)",
        "kicker": "ANEXO A · GESTIÓN DEL CAMBIO",
        "title": "Seis obstáculos del cambio organizacional (OCM)",
        "blocks": [
            ("chips", ["Visión", "Alineación del liderazgo", "Involucramiento", "KPIs", "Cultura", "Comunicación"]),
            ("bullets", ["Redefinir roles es el cambio que hay que gestionar: el miedo a perder el puesto se "
                         "responde mostrando qué responsabilidades permanecen.",
                         "Patrocinio ejecutivo: 2× más en los high performers (McKinsey)."]),
        ],
        "sources": "Referencias: Kotter (1995), Leading Change; Prosci ADKAR; McKinsey, The State of AI.",
    },
    {
        "id": "B", "label": "Anexo B", "short": "Anexo B: marcos de gobernanza",
        "kicker": "ANEXO B · MARCOS DE GOBERNANZA",
        "title": "Los marcos coinciden: la responsabilidad se asigna a personas",
        "blocks": [("rows", [
            ("NIST AI RMF 1.0", "Roles documentados (GOVERN 2.1); el liderazgo ejecutivo asume el riesgo (2.3); "
                                "roles para configuraciones humano-IA (3.2)."),
            ("ISO/IEC 42001:2023", "Cláusula 5: la alta dirección fija la política de IA y asigna roles, "
                                   "responsabilidades y autoridades."),
            ("EU AI Act, Art. 14", "Personas que entiendan el sistema, vigilen el sesgo de automatización y "
                                   "puedan anularlo o detenerlo."),
            ("EU AI Act, Art. 4", "Alfabetización en IA suficiente del personal (desde febrero de 2025)."),
        ], 190)],
    },
    {
        "id": "90", "label": "Plan · 90 días", "short": "Primeros 90 días",
        "kicker": "PLAN · PRIMEROS 90 DÍAS",
        "title": "Qué hacer en los primeros 90 días",
        "blocks": [("checks", [
            "Nombrar un sponsor ejecutivo y publicar una postura clara sobre IA.",
            "Elegir 2 o 3 roles críticos y hacer su matriz responsabilidad–actividad.",
            "Correr el diagnóstico Organización/Equipo/Individual en esas áreas.",
            "Asignar un owner humano a cada actividad que pase a IA o agentes.",
            "Fijar 1 o 2 KPIs de negocio por caso de uso antes del piloto.",
        ])],
    },
]
SLIDES: list[dict] = DECK + ANNEXES
BY_ID = {s["id"]: s for s in SLIDES}

_PREFIX = re.compile(r"^(l[aá]mina|lamina|slide|anexo|annex|plan)\s*", re.I)


def resolve(key: Optional[str]) -> Optional[str]:
    """Normalize a user/agent slide reference to a slide id; None = whole deck.

    Raises ValueError for an unknown slide.
    """
    k = (key or "").strip().lower()
    if k in ("", "all", "todas", "todo", "deck", "completa", "completo", "presentacion", "presentación"):
        return None
    k = _PREFIX.sub("", k).strip(" .:#")
    if k in ("90d", "90 dias", "90 días", "90"):
        return "90"
    k = k.lstrip("0") or "0"
    if k.upper() in BY_ID:
        return k.upper()
    raise ValueError(f"unknown slide '{key}'. Valid: all, " + ", ".join(BY_ID))


def index_text(lang: str = "es") -> str:
    head = ("Láminas disponibles. Pídeme una con su número o tema, p. ej. *lámina 8* (te la envío como imagen; "
            "con *lámina 8 pdf* te mando el PDF):"
            if lang == "es" else
            "Available slides. Ask for one by number or topic, e.g. *slide 8* (sent as an image; "
            "*slide 8 pdf* sends the PDF). Slides are in Spanish:")
    lines = [head] + [f"{s['id']}. {s['short']}" for s in SLIDES]
    return "\n".join(lines)


def caption(key: Optional[str], lang: str = "es") -> str:
    if key is None:
        return (f"📄 Láminas de la plática «Gobernanza de IA» (PDF, {len(SLIDES)} páginas)." if lang == "es"
                else f"📄 Slides of the talk “AI Governance” (PDF, {len(SLIDES)} pages, in Spanish).")
    s = BY_ID[key]
    head = (f"Lámina {s['id']}" if lang == "es" else f"Slide {s['id']}") if key.isdigit() else None
    text = f"{head} · {s['short']}" if head else s["short"]
    return f"📄 {text}" if lang == "es" else f"📄 {text} (in Spanish)"


def image_caption(key: str, lang: str = "es") -> str:
    hint = (f"PDF: escribe «lámina {key.lower()} pdf»" if lang == "es"
            else f"PDF: type “slide {key.lower()} pdf”")
    return f"{caption(key, lang)}\n{hint}"


def filename(key: Optional[str], ext: str = "pdf") -> str:
    if key is None:
        return f"Gobernanza-de-IA-laminas.{ext}"
    return f"Gobernanza-de-IA-lamina-{key.lower()}.{ext}"


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #
def _par(c, text: str, st, x: float, y: float, w: float) -> float:
    """Draw a paragraph with its top at y; return the y of its bottom."""
    p = Paragraph(text, st)
    _, h = p.wrap(w, 10_000)
    p.drawOn(c, x, y - h)
    return y - h


def _rrect(c, x, y, w, h, fill=None, stroke=None, r=10):
    c.saveState()
    if fill is not None:
        c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
        c.setLineWidth(1)
    c.roundRect(x, y, w, h, r, stroke=1 if stroke is not None else 0, fill=1 if fill is not None else 0)
    c.restoreState()


def _b_hero(c, y, text):
    top = y
    bottom = _par(c, text, P["hero"], MX + 22, y, CW - 22)
    c.setFillColor(AMBER)
    c.rect(MX, bottom, 6, top - bottom, stroke=0, fill=1)
    return bottom


def _b_bullets(c, y, items):
    for t in items:
        p = Paragraph(t, P["bullet"])
        _, h = p.wrap(CW - 24, 10_000)
        c.setFillColor(AMBER)
        c.rect(MX, y - 15, 7, 7, stroke=0, fill=1)
        p.drawOn(c, MX + 24, y - h)
        y -= h + 10
    return y + 10


def _b_rows(c, y, items, label_w=90):
    for label, text in items:
        pl = Paragraph(label, P["row_l"])
        pt = Paragraph(text, P["row_t"])
        _, hl = pl.wrap(label_w - 8, 10_000)
        _, ht = pt.wrap(CW - label_w, 10_000)
        h = max(hl, ht)
        pl.drawOn(c, MX, y - hl)
        pt.drawOn(c, MX + label_w, y - ht)
        y -= h + 8
        c.setStrokeColor(LINE)
        c.setLineWidth(0.8)
        c.line(MX, y + 3, MX + CW, y + 3)
        y -= 10
    return y + 10


def _b_stats(c, y, items):
    n = len(items)
    gap = 18
    w = (CW - gap * (n - 1)) / n
    heights = []
    for _, label, src in items:
        _, hl = Paragraph(label, P["stat_l"]).wrap(w - 32, 10_000)
        _, hs = Paragraph(src, P["stat_s"]).wrap(w - 32, 10_000)
        heights.append(18 + 50 + hl + 8 + hs + 16)
    h = max(heights)
    for i, (value, label, src) in enumerate(items):
        x = MX + i * (w + gap)
        _rrect(c, x, y - h, w, h, fill=CARD, stroke=LINE)
        c.setFillColor(AMBER)
        c.rect(x, y - h + 14, 4, h - 28, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 44)
        c.drawString(x + 18, y - 18 - 40, pdf_safe(value))
        yy = _par(c, label, P["stat_l"], x + 18, y - 18 - 54, w - 32)
        _par(c, src, P["stat_s"], x + 18, yy - 8, w - 32)
    return y - h


def _b_table(c, y, header, rows, fracs):
    data = [[Paragraph(esc(h), P["th"]) for h in header]]
    for r in rows:
        data.append([Paragraph(esc(v), P["td_b"] if j == 0 and len(header) > 3 else P["td"])
                     for j, v in enumerate(r)])
    t = Table(data, colWidths=[f * CW for f in fracs])
    st = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.6, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [CARD, PAPER]),
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
    ]
    t.setStyle(TableStyle(st))
    _, h = t.wrap(CW, 10_000)
    t.drawOn(c, MX, y - h)
    return y - h


def _b_note(c, y, text):
    return _par(c, text, P["note"], MX, y, CW)


def _b_quote(c, y, text, attribution):
    top = y
    yy = _par(c, text, P["quote"], MX + 22, y, CW - 22)
    yy = _par(c, attribution, P["quote_a"], MX + 22, yy - 6, CW - 22)
    c.setFillColor(AMBER)
    c.rect(MX, yy, 5, top - yy, stroke=0, fill=1)
    return yy


def _b_formula(c, y, text, caption_text):
    h = 64
    _rrect(c, MX, y - h, CW, h, fill=NAVY, r=12)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 25)
    c.drawCentredString(MX + CW / 2, y - h / 2 - 9, pdf_safe(text))
    return _par(c, caption_text, P["note"], MX, y - h - 10, CW)


def _b_stack(c, y, parts):
    h = 78
    x = MX
    total = sum(p for p, _ in parts)
    shades = [colors.HexColor("#94A3B8"), colors.HexColor("#475569"), NAVY]
    for i, (pct, label) in enumerate(parts):
        w = CW * pct / total
        c.setFillColor(AMBER if i == len(parts) - 1 else shades[i % 3])
        c.rect(x, y - h, w - 3, h, stroke=0, fill=1)
        c.setFillColor(NAVY if i == len(parts) - 1 else colors.white)
        c.setFont("Helvetica-Bold", 24)
        c.drawString(x + 10, y - 34, f"{pct}%")
        c.setFont("Helvetica-Bold", 9.5 if pct < 15 else 12)
        c.drawString(x + 10, y - 56, pdf_safe(label))
        x += w
    return y - h


def _b_pyramid(c, y, labels):
    """labels: top -> bottom. Narrow at the top, wide at the bottom."""
    n = len(labels)
    row_h, gap = 40, 6
    for i, label in enumerate(labels):
        frac = 0.42 + 0.58 * i / (n - 1)
        w = CW * frac
        x = MX + (CW - w) / 2
        top = i == 0
        c.setFillColor(AMBER if top else colors.HexColor(["#E3DDCF", "#CFC7B4", "#B8AF98", "#9C927A"][min(i - 1, 3)]))
        c.roundRect(x, y - row_h, w, row_h, 8, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(MX + CW / 2, y - row_h / 2 - 5, pdf_safe(f"{n - i} · {label}"))
        y -= row_h + gap
    return y + gap


def _b_steps(c, y, items):
    for num, title, text in items:
        c.setFillColor(NAVY)
        c.circle(MX + 15, y - 17, 15, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(MX + 15, y - 22, num)
        pt = Paragraph(f"{esc(title)}", P["step_t"])
        px = Paragraph(text, P["step_x"])
        _, ht = pt.wrap(150, 10_000)
        _, hx = px.wrap(CW - 200, 10_000)
        pt.drawOn(c, MX + 44, y - ht - 2)
        px.drawOn(c, MX + 200, y - hx - 2)
        y -= max(ht, hx, 34) + 9
    return y + 6


def _b_closing(c, y, text):
    y -= 6
    h = 46
    _rrect(c, MX, y - h, CW, h, fill=AMBER, r=10)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 21)
    c.drawCentredString(MX + CW / 2, y - h / 2 - 7, pdf_safe(text))
    return y - h


def _b_chips(c, y, labels):
    cols, gap = 3, 16
    w = (CW - gap * (cols - 1)) / cols
    h = 62
    for i, label in enumerate(labels):
        r, col = divmod(i, cols)
        x = MX + col * (w + gap)
        top = y - r * (h + gap)
        _rrect(c, x, top - h, w, h, fill=CARD, stroke=LINE)
        c.setFillColor(AMBER)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(x + 14, top - h / 2 - 8, str(i + 1))
        _par(c, esc(label), P["chip"], x + 46, top - h / 2 + 10, w - 56)
    rows = (len(labels) + cols - 1) // cols
    return y - rows * (h + gap) + gap


def _b_checks(c, y, items):
    for t in items:
        p = Paragraph(t, P["bullet"])
        _, h = p.wrap(CW - 40, 10_000)
        c.setStrokeColor(NAVY)
        c.setLineWidth(1.6)
        c.rect(MX, y - 17, 15, 15, stroke=1, fill=0)
        p.drawOn(c, MX + 34, y - h)
        y -= max(h, 20) + 14
    return y + 14


_BLOCKS = {
    "hero": _b_hero, "bullets": _b_bullets, "rows": _b_rows, "stats": _b_stats, "table": _b_table,
    "note": _b_note, "quote": _b_quote, "formula": _b_formula, "stack": _b_stack, "pyramid": _b_pyramid,
    "steps": _b_steps, "closing": _b_closing, "chips": _b_chips, "checks": _b_checks,
}


def _blocks_height(s: dict, y: float) -> float:
    scratch = canvas.Canvas(io.BytesIO(), pagesize=(W, H))
    top = y
    for kind, *args in s["blocks"]:
        y = _BLOCKS[kind](scratch, y, *args) - GAP
    return top - (y + GAP)


def _draw_slide(c: canvas.Canvas, s: dict) -> None:
    # Background + chrome
    c.setFillColor(PAPER)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(AMBER)
    c.rect(0, 0, 12, H, stroke=0, fill=1)

    y = H - 42
    y = _par(c, esc(s["kicker"]), P["kicker"], MX, y, CW) - 6
    y = _par(c, esc(s["title"]), P["title"], MX, y, CW)
    if s.get("subtitle"):
        y = _par(c, esc(s["subtitle"]), P["subtitle"], MX, y - 6, CW)
    y -= 22

    # Measure the blocks on a scratch canvas, then centre the group vertically
    # (biased upward) in the free area so short slides don't look top-heavy.
    avail = y - BOTTOM
    used = _blocks_height(s, y)
    if used > avail:
        raise ValueError(f"slide {s['id']} overflows the page by {used - avail:.0f}pt")
    y -= max(0.0, avail - used) * 0.4
    for kind, *args in s["blocks"]:
        y = _BLOCKS[kind](c, y, *args) - GAP

    # Footer: sources, brand, label
    if s.get("sources"):
        _par(c, esc(s["sources"]), P["sources"], MX, 74, CW)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(MX, 38, W - MX, 38)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 9)
    c.drawString(MX, 24, pdf_safe(BRAND))
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(NAVY)
    c.drawRightString(W - MX, 24, pdf_safe(s["label"]))


def render_png(key: str, width_px: int = 1280) -> bytes:
    """One slide as a PNG (16:9). Inline preview in WhatsApp, easy to save or forward."""
    import pypdfium2 as pdfium

    page = pdfium.PdfDocument(build_slides_pdf(key))[0]
    bitmap = page.render(scale=width_px / page.get_width())
    out = io.BytesIO()
    bitmap.to_pil().convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def _drawn_page(s: dict) -> bytes:
    """One annex page as a one-page PDF (reportlab)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    _draw_slide(c, s)
    c.showPage()
    c.save()
    return buf.getvalue()


def build_slides_pdf(key: Optional[str] = None) -> bytes:
    """PDF of the whole deck (key=None) or of one slide (id from ``resolve``).

    Deck pages are copied from the talk's PDF, annex pages are drawn, and the
    result keeps the slide order.
    """
    import pypdfium2 as pdfium

    picked = SLIDES if key is None else [BY_ID[key]]
    out = pdfium.PdfDocument.new()
    with pdfium.PdfDocument(str(DECK_FILE)) as deck:
        for is_deck, group in groupby(picked, key=lambda s: "page" in s):
            group = list(group)
            if is_deck:  # one import per run: page by page would repeat shared fonts (4 MB instead of 0.4)
                out.import_pages(deck, [s["page"] for s in group])
            else:
                for s in group:
                    with pdfium.PdfDocument(_drawn_page(s)) as page:
                        out.import_pages(page)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()
