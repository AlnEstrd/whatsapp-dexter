"""Re-engagement outside the 24 h window: approved WhatsApp templates only.

Rules (enforced by :func:`check`): only contactable leads, only a configured template
(TEMPLATE_FOLLOWUP_ES / _EN, picked by ``lead.lang``), at most one per lead every 7 days,
one lead at a time (Slack ``followup`` + confirmation, the dashboard button, or
``POST /admin/leads/{id}/followup``). Never bulk. Templates are created by Alan in
WhatsApp Manager; this module only sends them (Meta Cloud API).
"""
from __future__ import annotations

import logging
from typing import Optional

from ..channels import get_channel
from ..config import get_settings
from . import core

logger = logging.getLogger("leads.templates")

MIN_DAYS_BETWEEN = 7
NEUTRAL_NAME = {"es": "qué tal", "en": "there"}   # "Hola {{1}}," when the name is unknown


def template_for(lead: dict) -> tuple[Optional[str], str]:
    s = get_settings()
    lang = "en" if lead.get("lang") == "en" else "es"
    name = (s.template_followup_en or None) if lang == "en" else (s.template_followup_es or None)
    return name, lang


def check(lead: Optional[dict]) -> Optional[str]:
    """None when a template may be sent, else the reason (shown to Alan)."""
    if lead is None:
        return "lead not found"
    if not lead.get("contactable"):
        return "not contactable (no consent or opted out)"
    if not lead.get("user_id"):
        return "no WhatsApp number (email-only lead)"
    name, lang = template_for(lead)
    if not name:
        return f"template not configured (TEMPLATE_FOLLOWUP_{lang.upper()})"
    last = lead.get("last_template_at")
    if last and core.now() - last < MIN_DAYS_BETWEEN * 86400:
        return f"a template was sent less than {MIN_DAYS_BETWEEN} days ago"
    if get_channel("meta") is None:
        return "Meta channel not configured"
    return None


def payload(lead: dict) -> dict:
    name, lang = template_for(lead)
    first = (lead.get("name") or lead.get("wa_profile_name") or "").split()
    var = first[0] if first else NEUTRAL_NAME[lang]
    return {
        "messaging_product": "whatsapp",
        "to": lead["user_id"],
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": "es_MX" if lang == "es" else "en"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": var}]}],
        },
    }


async def send_followup(lead_id: str, by: str) -> tuple[bool, str]:
    """Send the follow-up template. Returns (sent, note for Alan)."""
    lead = core.get(lead_id)
    reason = check(lead)
    if reason:
        return False, f"Not sent: {reason}."
    channel = get_channel("meta")
    body = payload(lead)
    try:
        await channel._post("/messages", body)  # noqa: SLF001 (same Graph endpoint as text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("template send failed for %s: %s", lead_id, exc)
        return False, f"Not sent: WhatsApp error ({str(exc)[:200]})."
    core.update(lead_id, last_template_at=core.now())
    core.activity(lead_id, "followup_sent", 0, {"template": body["template"]["name"], "by": by})
    return True, f"Template `{body['template']['name']}` sent."

