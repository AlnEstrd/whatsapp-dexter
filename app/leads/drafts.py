"""Prefilled drafts for Alan's *personal* WhatsApp (dashboard "Escribir desde mi WhatsApp").

Nothing is sent automatically: the link opens Alan's WhatsApp app with the text in the box,
he edits it and sends it himself. Dexter never sees those messages, which is why the
dashboard has "Marcar contactado".
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from ..flows import quiz

DRAFT = {
    "es": ("Hola {first}, soy Alan Estrada. Gracias por platicar con Dexter después de la plática de "
           "Gobernanza de IA. {hook} ¿Te late una llamada de 20 minutos esta semana para verlo en tu empresa?"),
    "en": ("Hi {first}, this is Alan Estrada. Thanks for chatting with Dexter after the AI Governance "
           "talk. {hook} Would a 20-minute call this week work to look at it for your company?"),
}
HOOK_NOTE = {"es": "Me quedé con lo que comentaste: {note}.", "en": "I noted what you mentioned: {note}."}
HOOK_DOMAIN = {"es": "Vi que en tu diagnóstico el punto a trabajar es *{domain}*.",
               "en": "I saw that in your assessment the area to work on is *{domain}*."}
HOOK_NONE = {"es": "Me dio gusto ver tu interés en el tema.", "en": "Glad to see your interest in the topic."}
NEUTRAL = {"es": "", "en": ""}


def _first(lead: dict) -> str:
    name = lead.get("name") or lead.get("wa_profile_name") or ""
    return name.split()[0] if name.strip() else ""


def draft(lead: dict) -> str:
    lang = "en" if lead.get("lang") == "en" else "es"
    weakest = lead.get("weakest_domains") or []
    if lead.get("interest_note"):
        hook = HOOK_NOTE[lang].format(note=lead["interest_note"].rstrip("."))
    elif weakest:
        q = next((q for q in quiz.QUESTIONS if q["key"] == weakest[0]), None)
        hook = HOOK_DOMAIN[lang].format(domain=q["domain"][lang]) if q else HOOK_NONE[lang]
    else:
        hook = HOOK_NONE[lang]
    first = _first(lead)
    text = DRAFT[lang].format(first=first, hook=hook)
    return text.replace("Hola , ", "Hola, ").replace("Hi , ", "Hi, ")


def wa_link(lead: dict) -> Optional[str]:
    """https://wa.me/<E.164 without +>?text=<urlencoded draft>, or None without a phone."""
    phone = lead.get("user_id")
    if not phone:
        return None
    return f"https://wa.me/{phone}?text={quote(draft(lead), safe='')}"
