"""AI-maturity rapid assessment for small and mid-size companies (ES/EN).

Deterministic on purpose: questions, scoring and recommendations are fixed
data, so every attendee gets the same, explainable result and no LLM call is
needed. Built on the talk's seven organizational domains plus adoption and
measurement (yield).

Scale (average of 9 answers scored 0-3):
    L0 Exploración / Exploring        < 1.0
    L1 Ad hoc (en riesgo / at risk)   1.0 - 1.9
    L2 Sistemática / Systematic       2.0 - 2.7
    L3 Frontera / Frontier            >= 2.8
Gate from the talk: without sponsorship (leadership) and ownership the other
domains don't produce value -> if either scores <= 1 the level is capped at L1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types import Option, Outgoing

# Each option: (title <= 24 chars, description <= 72 chars), scored 0..3 in order.
QUESTIONS: list[dict] = [
    {
        "key": "adoption",
        "domain": {"es": "Adopción", "en": "Adoption"},
        "q": {"es": "¿Qué tanto usa IA tu equipo en el trabajo diario?",
              "en": "How much does your team use AI in daily work?"},
        "opts": {
            "es": [("Casi nada", "Pocas personas, de vez en cuando y por su cuenta"),
                   ("Algunos, sin reglas", "Varios la usan seguido, con herramientas personales"),
                   ("La mayoría, a diario", "Más de la mitad, a diario, con herramientas aprobadas"),
                   ("Parte de los procesos", "IA o agentes ejecutan pasos completos de procesos clave")],
            "en": [("Barely", "A few people, now and then, on their own"),
                   ("Some, no rules", "Several use it often, with personal tools"),
                   ("Most, daily", "Over half use it daily, with approved tools"),
                   ("Built into processes", "AI or agents run full steps of key processes")],
        },
    },
    {
        "key": "strategy",
        "domain": {"es": "Estrategia y valor", "en": "Strategy & value"},
        "q": {"es": "¿Para qué usan la IA en tu empresa?",
              "en": "What is your company using AI for?"},
        "opts": {
            "es": [("Sin objetivo claro", "Pruebas sueltas, por curiosidad"),
                   ("Casos aislados", "Algunos casos con beneficio esperado, sin medirlo"),
                   ("Casos con KPIs", "Casos priorizados con 1 o 2 KPIs de negocio cada uno"),
                   ("Parte de la estrategia", "Portafolio de casos y presupuesto en el plan de negocio")],
            "en": [("No clear goal", "Loose experiments, out of curiosity"),
                   ("Isolated use cases", "Some use cases with expected benefit, not measured"),
                   ("Use cases with KPIs", "Prioritized use cases with 1-2 business KPIs each"),
                   ("Part of the strategy", "Use-case portfolio and budget in the business plan")],
        },
    },
    {
        "key": "governance",
        "domain": {"es": "Gobernanza y riesgo", "en": "Governance & risk"},
        "q": {"es": "¿Tienen reglas para usar IA?",
              "en": "Do you have rules for using AI?"},
        "opts": {
            "es": [("Ninguna", "Cada quien decide qué herramienta y qué datos usar"),
                   ("Informales", "Recomendaciones verbales o un correo"),
                   ("Política escrita", "Herramientas aprobadas, datos prohibidos y revisión humana"),
                   ("Gobernanza activa", "Política, apetito de riesgo y revisión que guía el programa")],
            "en": [("None", "Everyone picks their own tools and what data to share"),
                   ("Informal", "Verbal guidance or an email"),
                   ("Written policy", "Approved tools, forbidden data and human review"),
                   ("Active governance", "Policy, risk appetite and reviews that steer the program")],
        },
    },
    {
        "key": "leadership",
        "domain": {"es": "Liderazgo y patrocinio", "en": "Leadership & sponsorship"},
        "q": {"es": "¿Quién impulsa la IA en la empresa?",
              "en": "Who drives AI in the company?"},
        "opts": {
            "es": [("Nadie en particular", "Surge sola, sin dueño"),
                   ("Entusiastas o TI", "Personas sueltas o el área de sistemas por su cuenta"),
                   ("Un sponsor directivo", "Un directivo es dueño del programa de IA"),
                   ("Dirección alineada", "La dirección comunica una postura clara y da seguimiento")],
            "en": [("Nobody in particular", "It happens on its own, no owner"),
                   ("Enthusiasts or IT", "Individuals or the IT team on their own"),
                   ("An executive sponsor", "One executive owns the AI program"),
                   ("Aligned leadership", "Leadership shares a clear stance and follows up")],
        },
    },
    {
        "key": "ownership",
        "domain": {"es": "Roles y ownership", "en": "Roles & ownership"},
        "q": {"es": "Cuando una actividad pasa a la IA, ¿quién responde por el resultado?",
              "en": "When an activity moves to AI, who answers for the result?"},
        "opts": {
            "es": [("No está claro", "Si algo sale mal, nadie sabe quién responde"),
                   ("Quien la usa", "Informalmente, la persona que usó la herramienta"),
                   ("Responsable con nombre", "Cada actividad con IA tiene un responsable humano"),
                   ("Roles rediseñados", "Responsable, presupuesto y autoridad para detener")],
            "en": [("Unclear", "If something goes wrong, nobody knows who answers"),
                   ("Whoever uses it", "Informally, the person who used the tool"),
                   ("A named owner", "Each AI activity has a named human owner"),
                   ("Roles redesigned", "Owner, budget and authority to stop it")],
        },
    },
    {
        "key": "people",
        "domain": {"es": "Personas y cultura", "en": "People & culture"},
        "q": {"es": "¿Cómo preparan a la gente para trabajar con IA?",
              "en": "How do you prepare people to work with AI?"},
        "opts": {
            "es": [("No se prepara", "Sin capacitación; hay miedo o desinterés"),
                   ("Autodidactas", "Cada quien aprende por su cuenta"),
                   ("Capacitación y champions", "Capacitación básica y algunos champions de IA"),
                   ("Programa continuo", "Alfabetización para todos, champions por área y liderazgo")],
            "en": [("Not at all", "No training; fear or lack of interest"),
                   ("Self-taught", "Everyone learns on their own"),
                   ("Training & champions", "Basic training and a few AI champions"),
                   ("Ongoing program", "AI literacy for all, champions per area, leadership")],
        },
    },
    {
        "key": "process",
        "domain": {"es": "Procesos y flujos", "en": "Processes & workflows"},
        "q": {"es": "¿Cómo integran la IA en sus procesos?",
              "en": "How is AI part of your processes?"},
        "opts": {
            "es": [("No está integrada", "La IA no toca los procesos de la empresa"),
                   ("Encima de lo existente", "Se copia y pega a un chat; el proceso no cambia"),
                   ("Algunos rediseñados", "Algunos procesos rediseñados de punta a punta"),
                   ("Rediseño sistemático", "Procesos clave con IA o agentes y supervisión humana")],
            "en": [("Not integrated", "AI doesn't touch company processes"),
                   ("Bolted on", "Copy-paste into a chat; the process is unchanged"),
                   ("Some redesigned", "A few processes redesigned end to end"),
                   ("Systematic redesign", "Key processes run with AI or agents and human oversight")],
        },
    },
    {
        "key": "data",
        "domain": {"es": "Datos y plataforma", "en": "Data & platform"},
        "q": {"es": "¿Cómo están sus datos para usarlos con IA?",
              "en": "How ready is your data to use with AI?"},
        "opts": {
            "es": [("Dispersos", "En Excel, correos y la cabeza de las personas"),
                   ("Sistemas, poca confianza", "ERP o CRM, pero datos incompletos o poco confiables"),
                   ("Confiables en lo clave", "Datos confiables y accesibles donde más importan"),
                   ("Plataforma común", "Datos gobernados y conectados a las herramientas de IA")],
            "en": [("Scattered", "In spreadsheets, emails and people's heads"),
                   ("Systems, low trust", "ERP or CRM, but data is incomplete or unreliable"),
                   ("Reliable where it counts", "Reliable, accessible data in key areas"),
                   ("Common platform", "Governed data connected to the AI tools")],
        },
    },
    {
        "key": "yield",
        "domain": {"es": "Medición y costo", "en": "Measurement & cost"},
        "q": {"es": "¿Cómo miden el valor de la IA?",
              "en": "How do you measure the value of AI?"},
        "opts": {
            "es": [("No se mide", "No sabemos cuánto gastamos ni qué obtenemos"),
                   ("Medimos uso", "Licencias, usuarios activos o número de consultas"),
                   ("Medimos productividad", "Tiempos o productividad en los casos de uso"),
                   ("Resultado y costo", "Resultado de negocio y costo de IA por unidad, con dueño")],
            "en": [("Not measured", "We don't know what we spend or what we get"),
                   ("We measure usage", "Licenses, active users or number of prompts"),
                   ("We measure productivity", "Cycle time or productivity in the use cases"),
                   ("Outcome and cost", "Business outcome and AI cost per unit, with an owner")],
        },
    },
]

LEVELS = {
    0: {"es": ("L0 · Exploración", "La IA aún no forma parte de la forma de trabajar."),
        "en": ("L0 · Exploring", "AI is not yet part of how the company works.")},
    1: {"es": ("L1 · Ad hoc (en riesgo)", "Hay uso de IA, pero inconsistente y sin dueño: los resultados varían y el valor no se puede demostrar."),
        "en": ("L1 · Ad hoc (at risk)", "AI is used, but inconsistently and without owners: results vary and value can't be proven.")},
    2: {"es": ("L2 · Sistemática", "La IA se usa de forma sistemática y escalable, con reglas, responsables y métricas."),
        "en": ("L2 · Systematic", "AI is used systematically and at scale, with rules, owners and metrics.")},
    3: {"es": ("L3 · Frontera", "La IA y los agentes están integrados a los procesos clave, con gobernanza que dirige el programa y valor medido."),
        "en": ("L3 · Frontier", "AI and agents are built into key processes, governance steers the program and value is measured.")},
}

# Next step for a weak dimension (from the talk's 5 steps and first 90 days).
TIPS = {
    "adoption": {"es": "Elige 2 o 3 roles críticos y define qué actividades pueden pasar a IA con herramientas aprobadas.",
                 "en": "Pick 2-3 critical roles and define which activities can move to AI with approved tools."},
    "strategy": {"es": "Fija 1 o 2 KPIs de negocio por caso de uso antes de lanzar el piloto.",
                 "en": "Set 1-2 business KPIs per use case before you launch the pilot."},
    "governance": {"es": "Publica una política corta: herramientas aprobadas, datos que no se comparten y cuándo revisa un humano.",
                   "en": "Publish a short policy: approved tools, data that must not be shared, and when a human reviews."},
    "leadership": {"es": "Nombra un sponsor ejecutivo y publica una postura clara sobre IA. Sin patrocinio, lo demás no rinde.",
                   "en": "Name an executive sponsor and publish a clear stance on AI. Without sponsorship, nothing else pays off."},
    "ownership": {"es": "Haz la matriz responsabilidad–actividad de un rol y asigna un responsable humano (y su presupuesto) a cada actividad con IA.",
                  "en": "Build the responsibility-activity matrix for one role and give each AI activity a named human owner (and budget)."},
    "people": {"es": "Arranca alfabetización en IA para todos y nombra champions por área; trabaja liderazgo y autoliderazgo.",
               "en": "Start AI literacy for everyone and name champions per area; build leadership and self-leadership."},
    "process": {"es": "Rediseña un flujo de punta a punta en lugar de agregar IA encima del proceso actual.",
                "en": "Redesign one workflow end to end instead of bolting AI onto the current process."},
    "data": {"es": "Identifica los datos que necesita tu primer caso de uso y asigna un dueño que garantice su calidad.",
             "en": "Identify the data your first use case needs and give it an owner accountable for quality."},
    "yield": {"es": "Mide resultado de negocio y costo total de IA por unidad de resultado, no solo uso; asigna un dueño del gasto.",
              "en": "Measure business outcome and total AI cost per unit of outcome, not just usage; give the spend an owner."},
}

T = {
    "intro": {"es": "📊 *Diagnóstico rápido de madurez en IA*\n9 preguntas, ~3 minutos. Responde con el número (1–4) o elige en la lista. Escribe *salir* para terminar.",
              "en": "📊 *AI maturity rapid assessment*\n9 questions, ~3 minutes. Reply with the number (1-4) or pick from the list. Type *exit* to stop."},
    "invalid": {"es": "Responde con un número del 1 al 4 (o escribe *salir*).",
                "en": "Please reply with a number from 1 to 4 (or type *exit*)."},
    "button": {"es": "Elegir respuesta", "en": "Choose answer"},
    "cancelled": {"es": "Listo, detuve el diagnóstico. Escribe *menú* para ver las opciones.",
                  "en": "OK, I stopped the assessment. Type *menu* to see the options."},
    "result_title": {"es": "📊 *Tu resultado*", "en": "📊 *Your result*"},
    "score": {"es": "Puntaje", "en": "Score"},
    "gate": {"es": "⚠️ Tope en L1: sin patrocinio directivo y sin ownership, los demás dominios no generan valor.",
             "en": "⚠️ Capped at L1: without executive sponsorship and ownership, the other domains don't produce value."},
    "by_domain": {"es": "*Por dominio*", "en": "*By domain*"},
    "next": {"es": "*Tus siguientes pasos*", "en": "*Your next steps*"},
    "all_good": {"es": "Mantén el rumbo: revisa costo por unidad de resultado y comparte lo que funciona.",
                 "en": "Keep going: review cost per unit of outcome and share what works."},
    "outro": {"es": "¿Quieres profundizar? Pregúntame sobre cualquier dominio o la plática, o escribe *hablar con Alan*.",
              "en": "Want to go deeper? Ask me about any domain or the talk, or type *talk to Alan*."},
}

EXIT_WORDS = {"salir", "exit", "cancelar", "cancel", "stop", "terminar", "menu", "menú"}
LETTERS = {"a": 1, "b": 2, "c": 3, "d": 4}


@dataclass
class QuizState:
    lang: str = "es"
    index: int = 0
    answers: list[int] = field(default_factory=list)


@dataclass
class Result:
    lang: str
    answers: dict[str, int]
    average: float
    level: int
    capped: bool
    weakest: list[str]


_states: dict[str, QuizState] = {}
_results: dict[str, Result] = {}


def active(user_id: str) -> bool:
    return user_id in _states


def last_result(user_id: str) -> Optional[Result]:
    return _results.get(user_id)


def is_exit(text: str) -> bool:
    """Typed words that stop the quiz (and only the quiz)."""
    return (text or "").strip().lower().strip("!.¡¿?") in EXIT_WORDS


def reset() -> None:  # tests
    _states.clear()
    _results.clear()


def _question(st: QuizState) -> Outgoing:
    q = QUESTIONS[st.index]
    lang = st.lang
    lines = [f"*{st.index + 1}/{len(QUESTIONS)} · {q['domain'][lang]}*", q["q"][lang], ""]
    options = []
    for i, (title, desc) in enumerate(q["opts"][lang], start=1):
        lines.append(f"{i}. *{title}* — {desc}")
        options.append(Option(id=f"quiz:{st.index}:{i}", title=title[:24], description=desc[:72]))
    return Outgoing(text="\n".join(lines), options=options, button=T["button"][lang])


def start(user_id: str, lang: str = "es") -> list[Outgoing]:
    st = QuizState(lang=lang if lang in ("es", "en") else "es")
    _states[user_id] = st
    return [Outgoing(text=T["intro"][st.lang]), _question(st)]


def _parse(st: QuizState, text: str, reply_id: Optional[str]) -> Optional[int]:
    if reply_id and reply_id.startswith("quiz:"):
        try:
            _, qi, choice = reply_id.split(":")
            if int(qi) == st.index:
                return int(choice)
        except ValueError:
            return None
        return None
    t = (text or "").strip().lower().rstrip(".)")
    if t[:1].isdigit() and t[:1] in "1234" and (len(t) == 1 or not t[1].isdigit()):
        return int(t[0])
    if t in LETTERS:
        return LETTERS[t]
    # Typed the option title?
    for i, (title, _) in enumerate(QUESTIONS[st.index]["opts"][st.lang], start=1):
        if t and t == title.lower():
            return i
    return None


def answer(user_id: str, text: str, reply_id: Optional[str] = None) -> list[Outgoing]:
    """Feed one user reply. Returns the next messages to send."""
    st = _states.get(user_id)
    if st is None:
        return []
    if is_exit(text) and not reply_id:
        _states.pop(user_id, None)
        return [Outgoing(text=T["cancelled"][st.lang])]
    choice = _parse(st, text, reply_id)
    if choice is None:
        return [Outgoing(text=T["invalid"][st.lang]), _question(st)]
    st.answers.append(choice - 1)
    st.index += 1
    if st.index < len(QUESTIONS):
        return [_question(st)]
    _states.pop(user_id, None)
    result = score(st.answers, st.lang)
    _results[user_id] = result
    return [Outgoing(text=render(result)), Outgoing(text=T["outro"][st.lang])]


def score(answers: list[int], lang: str = "es") -> Result:
    by_key = {q["key"]: a for q, a in zip(QUESTIONS, answers)}
    avg = round(sum(answers) / len(answers), 2)
    level = 0 if avg < 1.0 else 1 if avg < 2.0 else 2 if avg < 2.8 else 3
    capped = False
    if level > 1 and (by_key["leadership"] <= 1 or by_key["ownership"] <= 1):
        level, capped = 1, True
    # Weakest three (ties keep question order, which puts strategy/governance first).
    ranked = sorted(by_key, key=lambda k: by_key[k])
    weakest = [k for k in ranked if by_key[k] < 3][:3]
    return Result(lang=lang, answers=by_key, average=avg, level=level, capped=capped, weakest=weakest)


def _bar(v: int) -> str:
    return "●" * v + "○" * (3 - v)


def render(r: Result) -> str:
    lang = r.lang
    name, desc = LEVELS[r.level][lang]
    lines = [T["result_title"][lang], f"*{name}* — {T['score'][lang]} {r.average:.1f} / 3", desc]
    if r.capped:
        lines.append(T["gate"][lang])
    lines += ["", T["by_domain"][lang]]
    for q in QUESTIONS:
        lines.append(f"{_bar(r.answers[q['key']])} {q['domain'][lang]}")
    lines += ["", T["next"][lang]]
    if r.weakest:
        lines += [f"{i}. {TIPS[k][lang]}" for i, k in enumerate(r.weakest, start=1)]
    else:
        lines.append(T["all_good"][lang])
    return "\n".join(lines)


def summary_for_agent(r: Result) -> str:
    """Compact description of a result for the LLM (tool output)."""
    dims = ", ".join(f"{q['domain']['en']}={r.answers[q['key']]}/3" for q in QUESTIONS)
    return (f"Level {LEVELS[r.level]['en'][0]} (avg {r.average}/3"
            f"{', capped at L1 by sponsorship/ownership gate' if r.capped else ''}). "
            f"Dimensions: {dims}. Weakest: {', '.join(r.weakest) or 'none'}.")


def summary_for_slack(r: Result) -> str:
    dims = " · ".join(f"{q['domain']['es']} {r.answers[q['key']]}" for q in QUESTIONS)
    return (f"📊 Assessment done: *{LEVELS[r.level]['es'][0]}* ({r.average:.1f}/3"
            f"{', capped' if r.capped else ''})\n{dims}")
