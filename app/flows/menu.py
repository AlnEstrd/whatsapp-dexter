"""Welcome menu and language detection for the talk companion bot."""
from __future__ import annotations

import re
from typing import Optional

from .. import intents
from .types import Option, Outgoing

GREETINGS = {
    "hola", "hi", "hello", "hey", "buenas", "buenos dias", "buenos días", "buenas tardes",
    "buenas noches", "menu", "menú", "inicio", "start", "empezar", "ayuda", "help", "opciones",
    "options", "hola!", "qr",
}

_EN = {"the", "what", "how", "why", "who", "is", "are", "you", "your", "about", "talk", "hello",
       "hi", "please", "can", "does", "do", "my", "company", "and", "with", "this", "assessment",
       "english", "thanks", "thank", "which", "where"}
_ES = {"el", "la", "los", "las", "que", "qué", "como", "cómo", "por", "para", "quien", "quién",
       "es", "son", "tu", "mi", "empresa", "plática", "platica", "hola", "gracias", "de", "del",
       "una", "un", "con", "sobre", "español", "cuál", "cual", "dónde", "donde", "y"}


def detect_lang(text: str, current: Optional[str] = None) -> str:
    """'en' or 'es'. Keeps `current` unless the text clearly leans the other way."""
    words = re.findall(r"[a-záéíóúñü]+", (text or "").lower())
    en = sum(w in _EN for w in words)
    es = sum(w in _ES for w in words) + 2 * bool(re.search(r"[áéíóúñ¿¡]", text or ""))
    if en > es and en >= 1:
        return "en"
    if es > en and es >= 1:
        return "es"
    return current or "es"


def is_greeting(text: str) -> bool:
    t = re.sub(r"[!¡.?¿,]", "", (text or "").strip().lower())
    return t in GREETINGS


TEXT = {
    "welcome": {
        "es": ("👋 ¡Hola{name}! Soy Dexter, jefe de Estrada AI Lab. Aquí destilo lo que estamos probando, empezando por la plática *Gobernanza de IA* de Alan Estrada.\n\n"
               "¿Qué quieres hacer?\n"
               "1. 📚 Explorar la plática\n"
               "2. 📊 Diagnóstico de madurez en IA (3 min)\n"
               "3. 👤 Conocer a Alan\n"
               "4. 💬 Hablar con Alan\n"
               "5. 📄 Láminas en PDF\n\n"
               "También puedes escribirme tu pregunta directamente."),
        "en": ("👋 Hi{name}! I'm Dexter, lab chief at Estrada AI Lab. I distill what we're testing here, starting with Alan Estrada's talk *AI Governance*.\n\n"
               "What would you like to do?\n"
               "1. 📚 Explore the talk\n"
               "2. 📊 AI maturity assessment (3 min)\n"
               "3. 👤 About Alan\n"
               "4. 💬 Talk to Alan\n"
               "5. 📄 Slides (PDF)\n\n"
               "You can also just type your question."),
    },
    # First message of a new chat that is already a question: introduce, no menu, then answer.
    "intro": {
        "es": "👋 Hola{name}, soy Dexter, jefe de Estrada AI Lab. Estoy aquí para resolver tus dudas sobre lo más nuevo del lab.",
        "en": "👋 Hi{name}, I'm Dexter, lab chief at Estrada AI Lab. I'm here to answer your questions about what's new at the lab.",
    },
    # The menu list's one-line hint (the body of the list; its plain-text fallback follows it).
    "hint": {
        "es": "Puedes explorar la plática, hacer el diagnóstico de madurez en 3 minutos o conocer a Alan.",
        "en": "You can explore the talk, take the 3-minute AI maturity assessment, or get to know Alan.",
    },
    # ...and after that first answer: offer the menu, don't show it.
    "offer": {
        "es": "Puedes seguir preguntándome lo que quieras, o escribe *menu* para ver todo lo que puedo hacer.",
        "en": "Keep asking me anything, or type *menu* to see everything I can do.",
    },
    "button": {"es": "Ver opciones", "en": "See options"},
    "talk": {
        "es": ("En una frase: la IA cambia las *actividades* de un puesto, no sus responsabilidades. "
               "Por eso, para que dé resultados, hay que rediseñar roles y dejar claro qué persona "
               "responde por cada cosa que hace la IA.\n\n"
               "La plática arranca con las \"computers\" humanas de la NASA, explica por qué 3 de cada 4 "
               "empresas todavía no le ven valor y cierra con qué hacer en los primeros 90 días.\n\n"
               "¿Por dónde empezamos: la regla 10-20-70, un ejemplo real o el diagnóstico de tu empresa?"),
        "en": ("In one line: AI changes the *activities* in a role, not its responsibilities. "
               "So to get results you have to redesign roles and be clear about which person answers "
               "for everything the AI does.\n\n"
               "The talk opens with NASA's human \"computers\", explains why 3 in 4 companies still "
               "see no value, and closes with what to do in the first 90 days.\n\n"
               "Where do you want to start: the 10-20-70 rule, a real example, or a diagnostic of your company?"),
    },
}

MENU_OPTIONS = {
    "es": [("menu:talk", "Explorar la plática", "Preguntas sobre el contenido y las fuentes"),
           ("menu:quiz", "Diagnóstico de IA", "9 preguntas, ~3 min, nivel L0–L3 y siguientes pasos"),
           ("menu:about", "Conocer a Alan", "Quién es y cómo contactarlo"),
           ("menu:human", "Hablar con Alan", "Te responde una persona por este chat"),
           ("menu:slides", "Láminas en PDF", "Toda la presentación o una lámina")],
    "en": [("menu:talk", "Explore the talk", "Questions about the content and sources"),
           ("menu:quiz", "AI maturity quiz", "9 questions, ~3 min, L0-L3 level and next steps"),
           ("menu:about", "About Alan", "Who he is and how to reach him"),
           ("menu:human", "Talk to Alan", "A person replies in this chat"),
           ("menu:slides", "Slides (PDF)", "Full deck or one slide (in Spanish)")],
}

# Typed shortcuts for the menu (when the list isn't used).
TYPED = {
    "1": "menu:talk", "2": "menu:quiz", "3": "menu:about", "4": "menu:human", "5": "menu:slides",
    "laminas": "menu:slides", "láminas": "menu:slides", "slides": "menu:slides",
    "presentacion": "menu:slides", "presentación": "menu:slides", "deck": "menu:slides",
    "pdf": "menu:report", "reporte": "menu:report", "mi reporte": "menu:report",
    "report": "menu:report", "my report": "menu:report", "reporte pdf": "menu:report",
    "diagnostico": "menu:quiz", "diagnóstico": "menu:quiz", "quiz": "menu:quiz",
    "assessment": "menu:quiz", "evaluacion": "menu:quiz", "evaluación": "menu:quiz",
    "test": "menu:quiz", "encuesta": "menu:quiz",
    "hablar con alan": "menu:human", "talk to alan": "menu:human",
}


_SLIDE_RE = re.compile(r"^(?:l[aá]mina|slide)\s*#?\s*(\d{1,2}|[ab])(?:\s+(pdf|imagen|image))?$", re.I)


def slide_request(text: str) -> Optional[tuple[str, str]]:
    """'lámina 8' -> ('8', 'image'); 'slide A pdf' -> ('A', 'pdf'). The id is validated later
    by docs.make_deck. A single slide is an image unless the user adds 'pdf'."""
    m = _SLIDE_RE.match(re.sub(r"[!¡.?¿,]", "", (text or "").strip()))
    if not m:
        return None
    return m.group(1).upper(), ("pdf" if (m.group(2) or "").lower() == "pdf" else "image")


def welcome(lang: str, name: Optional[str] = None) -> Outgoing:
    first = f", {name.split()[0]}" if name else ""
    return Outgoing(
        text=TEXT["welcome"][lang].format(name=first),
        options=[Option(i, t, d) for i, t, d in MENU_OPTIONS[lang]],
        button=TEXT["button"][lang],
    )


# The menu list's plain-text fallback (channels without lists show these numbered lines).
MENU_LINES = {
    "es": ["1. 📚 Explorar la plática", "2. 📊 Diagnóstico de madurez en IA (3 min)", "3. 👤 Conocer a Alan",
           "4. 💬 Hablar con Alan", "5. 📄 Láminas en PDF"],
    "en": ["1. 📚 Explore the talk", "2. 📊 AI maturity assessment (3 min)", "3. 👤 About Alan",
           "4. 💬 Talk to Alan", "5. 📄 Slides (PDF)"],
}


def is_info_request(text: str) -> bool:
    """An opening request for information ("Hola, quisiera más información", "más info", "what can
    you do?"): after an optional greeting, the whole message is one of intents.INFO_REQUESTS."""
    t = intents.normalize(text)
    for g in intents.GREETING_PREFIXES:
        if t.startswith(g + " "):
            t = t[len(g) + 1:]
            break
    return t in intents.INFO_REQUESTS


def menu_list(lang: str) -> Outgoing:
    """The menu list on its own: after the consent welcome, or after the first answer of a chat."""
    hint = TEXT["hint"][lang]
    return Outgoing(
        text=hint + "\n\n" + "\n".join(MENU_LINES[lang]),
        options=[Option(i, t, d) for i, t, d in MENU_OPTIONS[lang]],
        button=TEXT["button"][lang],
        interactive_text=hint,
    )


def welcome_pair(lang: str, name: Optional[str] = None) -> tuple[Outgoing, Outgoing]:
    """Welcome for an opening request: a one-line welcome, then the menu list with a hint. Both
    messages are one bundle, with no model call."""
    first = f", {name.split()[0]}" if name else ""
    hello = TEXT["welcome"][lang].split("\n\n")[0].format(name=first)
    return Outgoing(text=hello), menu_list(lang)


def intro(lang: str, name: Optional[str] = None) -> str:
    first = f", {name.split()[0]}" if name else ""
    return TEXT["intro"][lang].format(name=first)


def menu_offer(lang: str) -> str:
    return TEXT["offer"][lang]


def typed_choice(text: str) -> Optional[str]:
    t = re.sub(r"[!¡.?¿]", "", (text or "").strip().lower())
    return TYPED.get(t)


def talk_overview(lang: str) -> Outgoing:
    return Outgoing(text=TEXT["talk"][lang])
