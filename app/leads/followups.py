"""Pendientes de seguimiento: the people Alan writes to personally after the talk.

The list is every lead with followup_status = pending: explicit yes or the tacit notice, not declined,
not opted out, not contacted yet. Oldest first. Dexter never writes these; the dashboard gives Alan a
wa.me link with a short draft (opened in his own WhatsApp, sent by him), a mailto: link when there is an
email, and "Marcar contactado" to close the item.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from .. import db
from ..analytics import events
from ..flows import welcome
from . import core

FOLLOWUP_CHANNELS = ("personal_whatsapp", "call", "email", "other")

# The draft is a starting point Alan edits before he sends it. "{who}" is the first name, if known.
DRAFT = {
    "es": "Hola{who}, soy Alan, gracias por escribirle a Dexter en la plática de Gobernanza de IA. ¿Cómo te fue con {topic}?",
    "en": "Hi{who}, this is Alan, thanks for chatting with Dexter at the AI Governance talk. How did {topic} go?",
}
TOPIC = {"es": ("el diagnóstico", "la plática"), "en": ("the assessment", "the talk")}
MAIL_SUBJECT = {"es": "Gobernanza de IA: recursos de la plática", "en": "AI Governance talk: resources"}


def _lang(lead: dict) -> str:
    return "en" if lead.get("lang") == "en" else "es"


def pending(limit: int = 500) -> list[dict]:
    """Oldest first_seen first. Same rule as followup_status = 'pending' (see core.followup_status_for)."""
    rows = db.query("""SELECT * FROM leads WHERE followup_status = 'pending'
                       AND consent_type IN ('explicit', 'tacit_notice') AND opted_out_at IS NULL
                       ORDER BY COALESCE(first_seen, created_at), lead_id LIMIT ?""", (int(limit),))
    return [core._dict(r) for r in rows]  # noqa: SLF001  (same package)


def oldest_days(rows: list[dict]) -> Optional[int]:
    if not rows:
        return None
    first = min(r.get("first_seen") or r.get("created_at") or core.now() for r in rows)
    return int((core.now() - first) // 86400)


def digest_line() -> Optional[str]:
    """"Pendientes de seguimiento: N (más antiguo: X días)" for the Slack digest; None when N = 0."""
    rows = pending(limit=100000)
    if not rows:
        return None
    return f"Pendientes de seguimiento: {len(rows)} (más antiguo: {oldest_days(rows)} días)"


def draft(lead: dict) -> str:
    lang = _lang(lead)
    first = welcome.first_name(lead.get("name") or lead.get("wa_profile_name"))
    topic = TOPIC[lang][0 if lead.get("quiz_level") is not None else 1]
    return DRAFT[lang].format(who=f" {first}" if first else "", topic=topic)


def wa_link(lead: dict) -> Optional[str]:
    """https://wa.me/<number>?text=<draft>, opened in Alan's own WhatsApp. None without a number."""
    phone = lead.get("user_id")
    if not phone:
        return None
    return f"https://wa.me/{phone}?text={quote(draft(lead), safe='')}"


def mailto(lead: dict) -> Optional[str]:
    email = lead.get("email")
    if not email:
        return None
    return f"mailto:{email}?subject={quote(MAIL_SUBJECT[_lang(lead)], safe='')}"


def mark_contacted(lead_id: str, channel: str, note: Optional[str] = None) -> Optional[dict]:
    """Alan contacted the lead: followup_status = contacted, with channel, time and an optional note.
    The stage moves to "contacted" too. None when the lead is not on the list."""
    lead = core.get(lead_id)
    if lead is None or lead.get("followup_status") != "pending":
        return None
    channel = channel if channel in FOLLOWUP_CHANNELS else "other"
    core.update(lead_id, contacted_at=core.now(), contacted_channel=channel,
                followup_note=core.scrub(note, 300) if note else None)
    core.sync_followup(lead_id)
    core.set_stage(lead_id, "contacted", "dashboard", channel=channel)
    events.record("followup_marked", lead.get("user_id"), step="followup", channel=channel)
    return core.get(lead_id)
