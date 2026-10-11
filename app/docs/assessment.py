"""Multipage PDF report for the AI-maturity assessment (ES/EN).

Built from a ``quiz.Result`` only, so it is deterministic and explainable:
  p.1  level, score, where you sit on L0-L3, strengths and priorities
  p.2  the nine domains with the answer the person picked
  p.3  next steps for the weakest domains, 90-day plan, how to continue
"""
from __future__ import annotations

import datetime as dt
import io
from typing import Optional

from reportlab.lib import colors
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

from ..flows import quiz
from .common import FRAME_W, MARGIN, PAGE_H, PAGE_W, fmt_date, numbered_canvas, today_mx
from .theme import (AMBER, AMBER_DARK, CARD, LEVEL_COLORS, LINE, MUTED, NAVY, PAPER, S, esc,
                    pdf_safe, style)

BAND_H = 124

TXT = {
    "es": {
        "kicker": "DIAGNÓSTICO · GOBERNANZA DE IA", "title": "Tu diagnóstico de madurez en IA",
        "for": "Preparado para", "date": "Fecha", "score": "Puntaje promedio", "of": "de 3",
        "scale": "Dónde estás en la escala", "meaning": "Qué significa",
        "gate": "Tu nivel quedó topado en L1: sin patrocinio directivo y sin ownership, los demás dominios "
                "no generan valor. Atender esos dos puntos es lo que más mueve el resultado.",
        "strengths": "Fortalezas", "priorities": "Prioridades", "none_strong": "Ningún dominio en el nivel máximo "
        "todavía: es normal, empieza por las prioridades.", "none_weak": "Sin prioridades urgentes: mantén el rumbo.",
        "detail": "Detalle por dominio", "domain": "Dominio", "level": "Nivel", "answer": "Tu respuesta",
        "inferred": "Calificado a partir de lo que contaste.",
        "legend": "Cada dominio se califica de 0 a 3 según la respuesta elegida. 3 es el nivel más avanzado.",
        "next": "Tus siguientes pasos", "why": "Por qué importa", "do": "Qué hacer",
        "all_good": "Mantén el rumbo: revisa el costo por unidad de resultado y comparte lo que funciona.",
        "d90": "Primeros 90 días", "d90_sub": "Una lista corta y concreta para arrancar, sin importar tu nivel.",
        "cta_t": "¿Quieres profundizar?",
        "cta": "Escribe <b>hablar con Alan</b> en este chat para conversar con una persona, o pregunta por "
               "cualquier dominio o por la plática. Más en alanestrada.space.",
        "disc": "Autodiagnóstico orientativo basado en 9 preguntas; no es una auditoría. Los niveles y dominios "
                "provienen de la plática «Gobernanza de IA» (DORA, NIST AI RMF, McKinsey, BCG, EU AI Act).",
        "lv_title": "Los cuatro niveles",
    },
    "en": {
        "kicker": "ASSESSMENT · AI GOVERNANCE", "title": "Your AI maturity report",
        "for": "Prepared for", "date": "Date", "score": "Average score", "of": "of 3",
        "scale": "Where you are on the scale", "meaning": "What it means",
        "gate": "Your level was capped at L1: without executive sponsorship and ownership, the other domains "
                "don't produce value. Fixing those two moves the result the most.",
        "strengths": "Strengths", "priorities": "Priorities", "none_strong": "No domain at the top level yet: "
        "that's normal, start with the priorities.", "none_weak": "No urgent priorities: keep going.",
        "detail": "Domain detail", "domain": "Domain", "level": "Level", "answer": "Your answer",
        "inferred": "Scored from what you told me.",
        "legend": "Each domain is scored 0 to 3 from the answer you picked. 3 is the most advanced.",
        "next": "Your next steps", "why": "Why it matters", "do": "What to do",
        "all_good": "Keep going: review the cost per unit of outcome and share what works.",
        "d90": "First 90 days", "d90_sub": "A short, concrete list to get started, whatever your level.",
        "cta_t": "Want to go deeper?",
        "cta": "Type <b>talk to Alan</b> in this chat to speak with a person, or ask about any domain or "
               "the talk. More at alanestrada.space.",
        "disc": "Orientative self-assessment based on 9 questions; not an audit. Levels and domains come from "
                "the talk “AI Governance” (DORA, NIST AI RMF, McKinsey, BCG, EU AI Act).",
        "lv_title": "The four levels",
    },
}

# Why each domain matters: the talk's key messages and figures (platica.md).
WHY = {
    "adoption": {"es": "La IA se adopta por actividad; la responsabilidad se queda en el rol y en una persona con nombre.",
                 "en": "AI is adopted by activity; accountability stays with the role and a named person."},
    "strategy": {"es": "3 de cada 4 empresas no pueden probarle al CFO los resultados de su gasto en IA (Tokenomics Foundation, 2026).",
                 "en": "3 in 4 companies can't prove to the CFO the results of their AI spend (Tokenomics Foundation, 2026)."},
    "governance": {"es": "La gobernanza no frena ni solo habilita: es la visión estratégica que dirige el programa de IA. NIST AI RMF y EU AI Act coinciden en asignar la responsabilidad a personas.",
                   "en": "Governance neither brakes nor merely enables: it is the strategic vision that steers the AI program. NIST AI RMF and the EU AI Act agree: accountability is assigned to people."},
    "leadership": {"es": "La IA amplifica lo que ya existe: con buen liderazgo acelera resultados, sin él acelera el desorden.",
                   "en": "AI amplifies what already exists: with good leadership it speeds up results, without it it speeds up disorder."},
    "ownership": {"es": "La columna «accountable» nunca contiene un agente: cada actividad que pasa a IA necesita un humano con nombre y presupuesto.",
                  "en": "The 'accountable' column never contains an agent: every activity that moves to AI needs a named human with a budget."},
    "people": {"es": "74% de las empresas no obtiene valor tangible de la IA; los líderes invierten 70% en personas y procesos (BCG, 2024).",
               "en": "74% of companies get no tangible value from AI; leaders invest 70% in people and processes (BCG, 2024)."},
    "process": {"es": "Solo 6% son high performers y 73% de ellos rediseñó sus flujos de trabajo (McKinsey, The State of AI).",
                "en": "Only 6% are high performers, and 73% of them redesigned their workflows (McKinsey, The State of AI)."},
    "data": {"es": "La IA amplifica lo que ya existe (DORA 2025): si los datos están dispersos o no son confiables, la IA amplifica ese problema.",
             "en": "AI amplifies what already exists (DORA 2025): if data is scattered or unreliable, AI amplifies that problem."},
    "yield": {"es": "Medir uso no es medir valor. El yield es el costo total de IA por unidad de resultado; si nadie es dueño del gasto, nadie puede demostrar su valor.",
              "en": "Measuring usage is not measuring value. Yield is total AI cost per unit of outcome; if nobody owns the spend, nobody can prove its value."},
}

PLAN_90 = {
    "es": ["Nombrar un sponsor ejecutivo y publicar una postura clara sobre IA.",
           "Elegir 2 o 3 roles críticos y hacer su matriz responsabilidad–actividad.",
           "Correr el diagnóstico Organización/Equipo/Individual en esas áreas.",
           "Asignar un owner humano a cada actividad que pase a IA o agentes.",
           "Fijar 1 o 2 KPIs de negocio por caso de uso antes del piloto."],
    "en": ["Name an executive sponsor and publish a clear stance on AI.",
           "Pick 2-3 critical roles and build their responsibility-activity matrix.",
           "Run the Organization/Team/Individual assessment in those areas.",
           "Give every activity that moves to AI or agents a named human owner.",
           "Set 1-2 business KPIs per use case before the pilot."],
}


# --------------------------------------------------------------------------- #
# Flowables
# --------------------------------------------------------------------------- #
class Banner(Flowable):
    """Big level badge + name + score + description."""

    def __init__(self, r: quiz.Result, tx: dict, width: float):
        super().__init__()
        self.r, self.tx, self.width, self.height = r, tx, width, 118

    def draw(self):
        c, r = self.canv, self.r
        col = LEVEL_COLORS[r.level]
        name, desc = quiz.LEVELS[r.level][r.lang]
        c.setFillColor(col)
        c.roundRect(0, 0, self.width, self.height, 12, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 54)
        c.drawString(20, self.height / 2 - 18, f"L{r.level}")
        x = 108
        c.setFont("Helvetica-Bold", 17)
        c.drawString(x, self.height - 30, pdf_safe(name.split("·", 1)[-1].strip()))
        c.setFont("Helvetica", 10.5)
        c.drawString(x, self.height - 48, pdf_safe(f"{self.tx['score']}: {r.average:.1f} {self.tx['of']}"))
        p = Paragraph(esc(desc), style("bd", fontSize=10.5, leading=14, textColor=colors.white))
        _, h = p.wrap(self.width - x - 18, 100)
        p.drawOn(c, x, self.height - 58 - h - 4)

    def wrap(self, aw, ah):
        return self.width, self.height


class Scale(Flowable):
    """L0-L3 track with the current level highlighted."""

    def __init__(self, r: quiz.Result, width: float):
        super().__init__()
        self.r, self.width, self.height = r, width, 46

    def draw(self):
        c = self.canv
        n, gap = 4, 6
        w = (self.width - gap * (n - 1)) / n
        for lv in range(n):
            x = lv * (w + gap)
            on = lv == self.r.level
            c.setFillColor(LEVEL_COLORS[lv] if on else colors.HexColor("#E8E3D6"))
            c.roundRect(x, 12, w, 26, 6, stroke=0, fill=1)
            c.setFillColor(colors.white if on else MUTED)
            c.setFont("Helvetica-Bold", 10)
            short = quiz.LEVELS[lv][self.r.lang][0].split("·", 1)[-1].strip().split(" (")[0]
            c.drawCentredString(x + w / 2, 21, pdf_safe(f"L{lv} · {short}"))

    def wrap(self, aw, ah):
        return self.width, self.height


class Dots(Flowable):
    """Three dots, filled up to ``value`` (0-3)."""

    def __init__(self, value: int, color):
        super().__init__()
        self.value, self.color, self.width, self.height = value, color, 54, 14

    def draw(self):
        c = self.canv
        for i in range(3):
            cx = 7 + i * 17
            if i < self.value:
                c.setFillColor(self.color)
                c.circle(cx, 7, 5.5, stroke=0, fill=1)
            else:
                c.setStrokeColor(colors.HexColor("#B9B2A0"))
                c.setLineWidth(1.2)
                c.circle(cx, 7, 5.5, stroke=1, fill=0)

    def wrap(self, aw, ah):
        return self.width, self.height


def _domain_name(key: str, lang: str) -> str:
    return next(q["domain"][lang] for q in quiz.QUESTIONS if q["key"] == key)


def _answer(key: str, value: int, lang: str) -> tuple[str, str]:
    q = next(q for q in quiz.QUESTIONS if q["key"] == key)
    return q["opts"][lang][value]


def build_assessment_pdf(r: quiz.Result, name: Optional[str] = None, today: Optional[dt.date] = None) -> bytes:
    lang = r.lang if r.lang in ("es", "en") else "es"
    tx = TXT[lang]
    today = today or today_mx()
    col = LEVEL_COLORS[r.level]
    lvl_name, lvl_desc = quiz.LEVELS[r.level][lang]

    def first_page(c, doc):
        c.setFillColor(PAPER)
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - BAND_H, PAGE_W, BAND_H, stroke=0, fill=1)
        c.setFillColor(AMBER)
        c.rect(0, PAGE_H - BAND_H, PAGE_W, 5, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(MARGIN, PAGE_H - 34, pdf_safe(tx["kicker"]))
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 25)
        c.drawString(MARGIN, PAGE_H - 70, pdf_safe(tx["title"]))
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor("#C9D3E3"))
        who = f"{tx['for']} {pdf_safe(name)}  ·  " if name else ""
        c.drawString(MARGIN, PAGE_H - BAND_H + 16, f"{who}{tx['date']}: {fmt_date(today, lang)}")

    def later_page(c, doc):
        c.setFillColor(PAPER)
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=MARGIN, bottomMargin=54, title=pdf_safe(tx["title"]),
                          author="Alan Estrada", subject="Gobernanza de IA")
    f1 = Frame(MARGIN, 54, FRAME_W, PAGE_H - BAND_H - 54 - 20, id="f1", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    f2 = Frame(MARGIN, 54, FRAME_W, PAGE_H - 54 - MARGIN, id="f2", leftPadding=0, rightPadding=0,
               topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate("first", [f1], onPage=first_page),
                          PageTemplate("later", [f2], onPage=later_page)])

    story: list = [NextPageTemplate("later")]

    # ---- Page 1: result ----------------------------------------------------
    story += [Banner(r, tx, FRAME_W), Spacer(1, 14),
              Paragraph(esc(tx["scale"]), S["h3"]), Scale(r, FRAME_W), Spacer(1, 6),
              Paragraph(esc(tx["meaning"]), S["h2"]), Paragraph(esc(lvl_desc), S["lead"])]
    if r.capped:
        gate = Table([[Paragraph(esc(tx["gate"]), style("g", fontSize=10.5, leading=14.5, textColor=NAVY))]],
                     colWidths=[FRAME_W])
        gate.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FDF1DC")),
                                  ("LINEBEFORE", (0, 0), (0, -1), 4, AMBER),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                                  ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
        story += [gate, Spacer(1, 8)]

    strong = [k for k in (q["key"] for q in quiz.QUESTIONS) if r.answers[k] == 3][:5]
    strengths = ([Paragraph(esc(_domain_name(k, lang)), S["bullet"], bulletText="•") for k in strong]
                 or [Paragraph(esc(tx["none_strong"]), S["body"])])
    priorities = ([Paragraph(f"{esc(_domain_name(k, lang))} <font color='#6B7280'>({r.answers[k]}/3)</font>",
                             S["bullet"], bulletText="•") for k in r.weakest]
                  or [Paragraph(esc(tx["none_weak"]), S["body"])])
    two = Table([[Paragraph(esc(tx["strengths"]), S["h3"]), Paragraph(esc(tx["priorities"]), S["h3"])],
                 [strengths, priorities]], colWidths=[FRAME_W / 2 - 6, FRAME_W / 2 - 6], hAlign="LEFT")
    two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), CARD),
                             ("BOX", (0, 0), (-1, -1), 0.8, LINE), ("LINEAFTER", (0, 0), (0, -1), 0.8, LINE),
                             ("LEFTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 8),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [Spacer(1, 6), two, Spacer(1, 14), Paragraph(esc(tx["lv_title"]), S["h3"])]
    for lv in range(4):
        n, d = quiz.LEVELS[lv][lang]
        story.append(Paragraph(f"<font color='{LEVEL_COLORS[lv].hexval().replace('0x', '#')}'><b>{esc(n)}</b></font>"
                               f" — {esc(d)}", style("lv", fontSize=9.5, leading=13, spaceAfter=3)))

    # ---- Page 2: detail ----------------------------------------------------
    story += [PageBreak(), Paragraph(esc(tx["detail"]), S["h1"]), Paragraph(esc(tx["legend"]), S["small"]),
              Spacer(1, 8)]
    rows = [[Paragraph(esc(tx["domain"]), S["th"]), Paragraph(esc(tx["level"]), S["th"]),
             Paragraph(esc(tx["answer"]), S["th"])]]
    for q in quiz.QUESTIONS:
        v = r.answers[q["key"]]
        title, desc = _answer(q["key"], v, lang)
        rows.append([Paragraph(esc(q["domain"][lang]), S["cell_b"]), Dots(v, LEVEL_COLORS[min(3, v + 0)] if v else MUTED),
                     [Paragraph(esc(title), S["cell_b"]), Paragraph(esc(desc), S["cell"]),
                      *([Paragraph(f"<i>{esc(tx['inferred'])}</i>", S["cell"])] if q["key"] in r.inferred else [])]])
    t = Table(rows, colWidths=[FRAME_W * 0.27, FRAME_W * 0.13, FRAME_W * 0.60], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [CARD, PAPER]), ("BOX", (0, 0), (-1, -1), 0.8, LINE),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story.append(t)

    # ---- Page 3: next steps -------------------------------------------------
    story += [PageBreak(), Paragraph(esc(tx["next"]), S["h1"])]
    if r.weakest:
        for i, k in enumerate(r.weakest, start=1):
            body = [Paragraph(esc(_domain_name(k, lang)), S["h3"]),
                    Paragraph(f"<b>{esc(tx['why'])}.</b> {esc(WHY[k][lang])}", S["cell"]), Spacer(1, 3),
                    Paragraph(f"<b>{esc(tx['do'])}.</b> {esc(quiz.TIPS[k][lang])}", S["cell"])]
            card = Table([[Paragraph(str(i), style("num", fontName="Helvetica-Bold", fontSize=20, leading=24,
                                                   textColor=AMBER_DARK)), body]],
                         colWidths=[34, FRAME_W - 34])
            card.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), CARD),
                                      ("BOX", (0, 0), (-1, -1), 0.8, LINE), ("LINEBEFORE", (0, 0), (0, -1), 4, AMBER),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 9),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
            story += [KeepTogether([card]), Spacer(1, 7)]
    else:
        story.append(Paragraph(esc(tx["all_good"]), S["lead"]))

    plan = [Paragraph(esc(tx["d90"]), S["h2"]), Paragraph(esc(tx["d90_sub"]), S["small"]), Spacer(1, 4)]
    plan += [Paragraph(esc(x), S["bullet"], bulletText="[ ]") for x in PLAN_90[lang]]
    story += [KeepTogether(plan), Spacer(1, 10)]
    cta = Table([[[Paragraph(esc(tx["cta_t"]), style("ct", fontName="Helvetica-Bold", fontSize=12,
                                                    leading=16, textColor=colors.white)),
                   Paragraph(tx["cta"].replace("<b>", "<b><font color='#F2A541'>").replace("</b>", "</font></b>"),
                             style("cb", fontSize=10, leading=14, textColor=colors.white))]]],
                colWidths=[FRAME_W])
    cta.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 14),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 14), ("TOPPADDING", (0, 0), (-1, -1), 11),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
    story += [KeepTogether([cta]), Spacer(1, 10), Paragraph(esc(tx["disc"]), S["small"])]

    doc.build(story, canvasmaker=numbered_canvas())
    return buf.getvalue()
