"""Topic taxonomy for attendee messages (stable ids, ES/EN labels).

Labels come for free from the off-topic guard (it returns ``{"on_topic", "topic"}``); messages
that skip the guard are labelled from the route they took (menu, quiz, close...).
"""
from __future__ import annotations

import json
import re
from typing import Optional

# id -> (es, en). Ids are stable: metrics, the digest and the MCP tools key on them.
TOPICS: dict[str, tuple[str, str]] = {
    # the 9 quiz domains
    "adopcion": ("Adopción", "Adoption"),
    "estrategia_valor": ("Estrategia y valor", "Strategy and value"),
    "gobernanza_riesgo": ("Gobernanza y riesgo", "Governance and risk"),
    "liderazgo": ("Liderazgo", "Leadership"),
    "roles_ownership": ("Roles y ownership", "Roles and ownership"),
    "personas_cultura": ("Personas y cultura", "People and culture"),
    "procesos": ("Procesos", "Processes"),
    "datos_plataforma": ("Datos y plataforma", "Data and platform"),
    "medicion_costo": ("Medición y costo", "Measurement and cost"),
    # everything else
    "sobre_alan": ("Sobre Alan", "About Alan"),
    "la_platica_general": ("La plática (general)", "The talk (general)"),
    "laminas": ("Láminas", "Slides"),
    "diagnostico": ("Diagnóstico", "Assessment"),
    "contacto_alan": ("Contactar a Alan", "Contact Alan"),
    "making_of": ("Cómo se hizo Dexter", "Making of Dexter"),
    "saludo_menu": ("Saludo / menú", "Greeting / menu"),
    "despedida": ("Despedida", "Goodbye"),
    "fuera_de_tema": ("Fuera de tema", "Off topic"),
    "otro": ("Otro", "Other"),
}
OTHER = "otro"

# Quiz domain key (app/flows/quiz.py) -> topic id.
QUIZ_DOMAINS = {
    "adoption": "adopcion", "strategy": "estrategia_valor", "governance": "gobernanza_riesgo",
    "leadership": "liderazgo", "ownership": "roles_ownership", "people": "personas_cultura",
    "process": "procesos", "data": "datos_plataforma", "yield": "medicion_costo",
}

# Menu option -> topic (messages routed by the menu never reach the guard).
MENU = {
    "menu:talk": "la_platica_general", "menu:quiz": "diagnostico", "menu:about": "sobre_alan",
    "menu:human": "contacto_alan", "menu:slides": "laminas", "menu:report": "diagnostico",
}

# Route -> topic, for messages that skipped the guard and had no more specific label.
ROUTE = {
    "menu": "saludo_menu", "quiz": "diagnostico", "close": "despedida", "handoff": "contacto_alan",
    "offtopic_warning": "fuera_de_tema", "file_reply": "laminas", "human": "contacto_alan",
}

# Topics that describe *what the conversation is about* (a short follow-up inherits them).
SUBSTANTIVE = set(TOPICS) - {"saludo_menu", "despedida", "fuera_de_tema", "otro", "contacto_alan"}


def label(topic_id: str, lang: str = "es") -> str:
    es, en = TOPICS.get(topic_id, TOPICS[OTHER])
    return en if lang == "en" else es


def valid(topic_id: Optional[str]) -> str:
    """Unknown, empty or malformed ids become ``otro``."""
    t = (topic_id or "").strip().lower()
    return t if t in TOPICS else OTHER


def from_route(route: Optional[str], previous: Optional[str] = None) -> str:
    """Label for a message the guard didn't see. An agent turn without a guard label
    (a short follow-up like "¿y en ventas?") keeps the conversation's last topic."""
    if route in ROUTE:
        return ROUTE[route]
    if route == "agent" and previous in SUBSTANTIVE:
        return previous
    return OTHER


def guard_prompt_suffix() -> str:
    ids = ", ".join(TOPICS)
    return (
        "\n\nReply with JSON only, no other text: "
        '{"on_topic": true|false, "topic": "<id>"}. on_topic is false exactly when the message '
        "is OUT of scope. topic is the closest id from this list: " + ids + ". Use the 9 quiz "
        "domains (adopcion ... medicion_costo) for questions about that area of AI in companies, "
        "la_platica_general for the talk as a whole, making_of for how this bot/Dexter was built, "
        "fuera_de_tema when OUT of scope, otro if nothing fits."
    )


def parse_guard(text: str) -> tuple[Optional[bool], Optional[str]]:
    """Classifier output -> (on_topic or None if unparseable, topic id or None).

    Accepts the JSON format and the legacy one-word IN/OUT (then topic is None)."""
    raw = (text or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("on_topic"), bool):
            return obj["on_topic"], valid(obj.get("topic") if isinstance(obj.get("topic"), str) else None)
    up = raw.upper()
    if up.startswith("OUT"):
        return False, None
    if up.startswith("IN"):
        return True, None
    return None, None
