"""Consent welcome: the first message of every new lead (app/leads/consent.py drives the state).

One place for the copy, the buttons and how a tap or a typed answer is read. Bump
CONSENT_TEXT_VERSION whenever the wording changes: every lead is then shown the new text once.
Nothing here calls a model; it is all text and matching.
"""
from __future__ import annotations

from typing import Optional

from .. import intents
from .types import Option, Outgoing

CONSENT_TEXT_VERSION = "2026-10-11"
# Copy version of the three-moment ask (app/leads/flows.py). Taps on its buttons record this version.
LEGACY_VERSION = "2026-10-v1"

# Greeting: "¡Hola, {name}!" when the WhatsApp profile name is a person's first name, else "¡Hola!".
GREET = {"es": ("¡Hola, {name}!", "¡Hola!"), "en": ("Hi, {name}!", "Hi!")}
# The paragraph break before "Alan" is the only change to the agreed copy: the source joined two paragraphs.
CONSENT_BODY = {
    "es": ("{greet} Soy Dexter, jefe del laboratorio de Estrada AI Lab. Aquí destilo lo que estamos probando: "
           "la plática, los slides, un diagnóstico de madurez en IA para tu empresa y más.\n\n"
           "Alan podría escribirte después para ver cómo te fue y si te puede ayudar. Si prefieres que no, "
           "toca *No, gracias* o escribe *BAJA* cuando quieras. Aviso de privacidad: alanestrada.space/privacy"),
    "en": ("{greet} I'm Dexter, lab chief at Estrada AI Lab. This is where I distill what we're testing: "
           "the talk, the slides, an AI maturity assessment for your company, and more.\n\n"
           "Alan might reach out later to see how it went and whether he can help. If you'd rather he didn't, "
           "tap *No, thanks* or type *STOP* anytime. Privacy notice: alanestrada.space/privacy"),
}
BUTTONS = {"es": ("Perfecto", "No, gracias"), "en": ("Sounds good", "No, thanks")}
YES_LABELS = {"perfecto", "sounds good"}     # normalised (intents.normalize) button titles
NO_LABELS = {"no gracias", "no thanks"}

COPY = {
    "yes_ack": {"es": "¡Gracias! Anotado. Si cambias de opinión, escribe *BAJA* cuando quieras.",
                "en": "Thanks! Noted. If you change your mind, type *STOP* anytime."},
    "no_ack": {"es": "Entendido, no te escribiremos después. Sigue explorando cuando quieras 🙂",
               "en": "Got it, we won't reach out later. Keep exploring anytime 🙂"},
    "opted_out": {"es": "Listo, te di de baja. No te volveremos a escribir.",
                  "en": "Done, you're unsubscribed. We won't contact you again."},
    "skip": {"es": "Va, sin problema. Sigo aquí para lo que necesites.",
             "en": "No problem. I'm still here for anything you need."},
    # Email ask after the quiz report ("report") or inside the farewell of a user-initiated close ("farewell").
    "email_ask_report": {"es": "¿Te lo mando también por correo, con los recursos de la plática? Escribe tu correo u *Omitir*.",
                         "en": "Want it by email too, with the talk resources? Type your email or *Skip*."},
    "email_ask_farewell": {"es": "Antes de irte: ¿me dejas tu correo para mandarte recursos y avisarte de la próxima plática? (u *Omitir*)",
                           "en": "Before you go: want to leave your email for resources and the next talk? (or *Skip*)"},
    "email_thanks": {"es": "¡Gracias! Lo anoté.", "en": "Thanks! Got it."},
    "email_retry": {"es": "Ese correo no se ve bien, ¿lo revisas? u *Omitir*",
                    "en": "That email doesn't look right, could you check it? Or *Skip*"},
}
EMAIL_SKIP = {"omitir", "skip", "no", "no gracias", "no thanks"}


def _lang(lang: Optional[str]) -> str:
    return lang if lang in ("es", "en") else "es"


def first_name(profile_name: Optional[str]) -> Optional[str]:
    """The WhatsApp profile's first name when it looks like a person's name: letters (accents
    allowed) and spaces, at most 20 characters. Anything else (digits, emoji, a long name) -> None."""
    p = (profile_name or "").strip()
    if not p or len(p) > 20 or not all(c.isalpha() or c.isspace() for c in p):
        return None
    words = p.split()
    return words[0] if words else None


def consent_text(lang: str, profile_name: Optional[str] = None) -> str:
    lang = _lang(lang)
    name = first_name(profile_name)
    greet = GREET[lang][0].format(name=name) if name else GREET[lang][1]
    return CONSENT_BODY[lang].format(greet=greet)


def consent_out(lang: str, profile_name: Optional[str] = None) -> Outgoing:
    """The welcome with its two buttons. Meta sends reply buttons; Twilio gets the numbered text."""
    lang = _lang(lang)
    body = consent_text(lang, profile_name)
    yes, no = BUTTONS[lang]
    return Outgoing(
        text=f"{body}\n\n1. {yes}\n2. {no}",
        options=[Option(f"consent:yes:{CONSENT_TEXT_VERSION}", yes),
                 Option(f"consent:no:{CONSENT_TEXT_VERSION}", no)],
        buttons=True,
        interactive_text=body,
    )


def parse_reply(reply_id: Optional[str]) -> tuple[Optional[bool], Optional[str]]:
    """A consent button id -> (True for "Perfecto" / False for "No, gracias", copy version tapped).
    Ids from the three-moment ask ("consent:yes") carry no version: they are LEGACY_VERSION."""
    parts = (reply_id or "").split(":")
    if len(parts) < 2 or parts[0] != "consent" or parts[1] not in ("yes", "no"):
        return None, None
    version = parts[2] if len(parts) > 2 and parts[2] else LEGACY_VERSION
    return parts[1] == "yes", version


def typed_choice(text: str, numeric: bool) -> Optional[bool]:
    """A typed answer to the welcome buttons: the label, or "1"/"2" on channels without buttons."""
    t = intents.normalize(text)
    if numeric:
        if t == "1":
            return True
        if t == "2":
            return False
    if t in YES_LABELS:
        return True
    if t in NO_LABELS:
        return False
    return None
