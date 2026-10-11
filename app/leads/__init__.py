"""Leads: turn talk attendees into a follow-up list Alan can act on.

    core.py       lead record, consent, opt-out, stages, retention (SQLite, system of record)
    scoring.py    fit + intent weights, decay, tiers, the "why" behind every point
    flows.py      WhatsApp steps: consent question, post-quiz profiling, "Que Alan me contacte"
    alerts.py     Slack: hot alert, thread commands, SLA/window reminders, daily digest
    templates.py  follow-up templates outside the 24 h window (one lead at a time)
    drafts.py     wa.me drafts for Alan's personal WhatsApp
    zoho.py       Zoho CRM push (outbox) + daily status pull + backfill CLI
    web.py        /admin/leads*, POST /contact, /dashboard

This module is the facade main.py and agent.py call. Every hook is a no-op with
LEADS_ENABLED=false and swallows its own errors: a lead, Slack or Zoho failure never breaks
or delays a WhatsApp reply (side effects run after the replies, in :func:`effects`).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from .. import sessions, store
from ..config import get_settings
from . import core

logger = logging.getLogger("leads")

_signals: set[str] = set()             # users whose agent turn flagged a buying signal
_contact_asked: set[str] = set()       # users whose agent turn called request_contact


def enabled() -> bool:
    """LEADS_ENABLED and a database (leads live in SQLite only)."""
    from .. import db
    return get_settings().leads_enabled and db.enabled()


def safe(label: str, fn: Callable[[], Any], default: Any = None) -> Any:
    if not enabled():
        return default
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("leads %s failed", label)
        return default


def _opened_at(uid: str) -> Optional[float]:
    return (sessions.get(uid) or {}).get("opened_at")


def _lang(uid: str) -> str:
    return store.get_lang(uid) or (sessions.get(uid) or {}).get("lang") or "es"


# --------------------------------------------------------------------------- #
# Hooks from the WhatsApp path (sync, DB only)
# --------------------------------------------------------------------------- #
def on_inbound(uid: str) -> None:
    safe("inbound", lambda: core.touch_inbound(uid, _opened_at(uid)))


def engage(uid: str, reason: str, profile_name: Optional[str] = None) -> Optional[dict]:
    def _do():
        lead = core.engage(uid, reason, profile_name=profile_name, lang=_lang(uid), opened_at=_opened_at(uid))
        core.recompute(lead["lead_id"])
        return lead
    return safe(f"engage/{reason}", _do)


def signal(uid: str, kind: str, data: Optional[dict] = None, topic: Optional[str] = None) -> None:
    """Intent signal for an existing lead (call :func:`engage` first)."""
    def _do():
        lead = core.by_user(uid)
        if lead is None:
            return
        if kind:
            core.add_intent(lead["lead_id"], kind, data)
        if topic:
            core.add_topic(lead["lead_id"], topic)
        core.recompute(lead["lead_id"])
    safe(f"signal/{kind}", _do)


def topic(uid: str, topic_id: str) -> None:
    signal(uid, "", topic=topic_id)


def quiz_done(uid: str, result, profile_name: Optional[str] = None) -> None:
    def _do():
        lead, _ = core.ensure(uid, profile_name=profile_name, lang=_lang(uid), reason="quiz")
        core.record_quiz(lead["lead_id"], result)
        core.add_topic(lead["lead_id"], "quiz")
        core.recompute(lead["lead_id"])
    safe("quiz", _do)


_BUSINESS = re.compile(
    r"\b(mi|mis|nuestra|nuestro|nuestras|our|my)\s+(empresa|compa[ñn][ií]a|negocio|equipo|organizaci[oó]n|"
    r"despacho|área|area|company|business|team|organi[sz]ation|firm)\b|implementar|implement|cotiza|precio|"
    r"cu[aá]nto cobra|servicios|consultor[ií]a|pricing|price|services|consulting", re.I)


def has_business_context(uid: str, reason: str = "") -> bool:
    lines = [t for role, t in store.transcript(uid) if role == "user"][-6:]
    return bool(_BUSINESS.search(" ".join([reason, *lines])))


def handoff(uid: str, reason: str, profile_name: Optional[str] = None) -> None:
    """User asked to talk to Alan. +25 when it's about their own company."""
    def _do():
        lead = core.engage(uid, "handoff", profile_name=profile_name, lang=_lang(uid), opened_at=_opened_at(uid))
        business = has_business_context(uid, reason) or bool(lead.get("company")) \
            or core.count_activity(lead["lead_id"], "buying_signal") > 0
        core.add_intent(lead["lead_id"], "handoff_business" if business else "handoff_requested",
                        {"reason": core.scrub(reason, 120)})
        core.recompute(lead["lead_id"])
    safe("handoff", _do)


def buying_signal(uid: str, kind: str, note: str) -> None:
    """Agent tool flag_buying_signal."""
    from . import scoring

    def _do():
        lead = core.engage(uid, "buying_signal", lang=_lang(uid), opened_at=_opened_at(uid))
        k = kind if kind in scoring.BUYING_SIGNAL_KINDS else "team_or_process_pain"
        core.add_intent(lead["lead_id"], "buying_signal", {"kind": k, "note": core.scrub(note, 200)})
        if k in scoring.weights().get("hot_kinds", []):
            core.set_tier_override(lead["lead_id"], "hot", k)
        if note and not lead.get("interest_note"):
            core.set_profile(lead["lead_id"], source="agent", interest_note=note)
        core.recompute(lead["lead_id"])
        _signals.add(uid)
    safe("buying_signal", _do)


def update_profile(uid: str, fields: dict) -> list[str]:
    """Agent tool update_lead_profile."""
    def _do():
        lead = core.engage(uid, "profile", lang=_lang(uid), opened_at=_opened_at(uid))
        changed = core.set_profile(lead["lead_id"], source="agent", **fields)
        core.recompute(lead["lead_id"])
        return changed
    return safe("profile", _do, default=[]) or []


def request_contact(uid: str) -> None:
    if enabled() and get_settings().contact_form_enabled:
        _contact_asked.add(uid)


def pop_signal(uid: str) -> bool:
    if uid in _signals:
        _signals.discard(uid)
        return True
    return False


def pop_contact(uid: str) -> bool:
    if uid in _contact_asked:
        _contact_asked.discard(uid)
        return True
    return False


def report_name(uid: str) -> Optional[str]:
    from .flows import report_name as _name
    return safe("report name", lambda: _name(core.by_user(uid)))


def opt_out(uid: str) -> Optional[dict]:
    return safe("opt-out", lambda: core.opt_out(uid))


def reset() -> None:  # tests
    _signals.clear()
    _contact_asked.clear()
    from . import alerts, zoho
    alerts.reset()
    zoho.reset()


# --------------------------------------------------------------------------- #
# Side effects (after the replies)
# --------------------------------------------------------------------------- #
async def effects(uid: str) -> None:
    """Hot alert once the lead is hot and contactable. Never raises."""
    if not enabled():
        return
    try:
        lead = core.by_user(uid)
        if lead and lead["tier"] == "hot" and lead["contactable"] and not lead.get("hot_alerted_at"):
            from . import alerts
            await alerts.hot_alert(lead["lead_id"])
    except Exception:  # noqa: BLE001
        logger.exception("lead effects failed")


async def contact_confirmed(lead_id: str, data: dict, version: str, via: str) -> None:
    """The form was confirmed (WhatsApp or web): request row, consent, hot, Slack, Zoho note."""
    from . import alerts
    try:
        core.add_contact_request(lead_id, data.get("channel") or "whatsapp", data.get("best_time"),
                                 data.get("topic"), data.get("note"))
        core.record_consent(lead_id, True, version, via)
        core.set_tier_override(lead_id, "hot", "contact_request")
        core.recompute(lead_id)
        await alerts.hot_alert(lead_id, tag="📇 Solicitud de contacto", force=True)
    except Exception:  # noqa: BLE001
        logger.exception("contact request handling failed for %s", lead_id)


async def opt_out_effects(lead: Optional[dict], human_mode: bool) -> None:
    if not lead:
        return
    from . import alerts
    note = "🚫 Opt-out (BAJA): no contactar. Consentimiento retirado" + (
        "; Zoho se actualiza." if get_settings().zoho_enabled else ".")
    if human_mode:
        note = f"⚠️ {alerts._owner()} " + note + " Estás en modo humano: no le escribas para seguimiento."
    await alerts.thread_note(lead, note)


WATCH_INTERVAL = 60


async def tick() -> None:
    """One pass of the background work: hot-alert safety net, reminders, digest, Zoho."""
    from . import alerts, zoho
    from .. import db
    for row in db.safe("hot due", lambda: db.query(
            "SELECT lead_id FROM leads WHERE tier = 'hot' AND contactable = 1 AND hot_alerted_at IS NULL"),
            default=[]) or []:
        await alerts.hot_alert(row["lead_id"])
    await alerts.check_reminders()
    await alerts.maybe_digest()
    if zoho.enabled():
        await zoho.process_due()
        await zoho.maybe_pull()


async def watcher() -> None:  # started from the app lifespan
    from . import zoho
    if zoho.enabled():
        try:
            await zoho.check_fields()
        except Exception:  # noqa: BLE001
            logger.exception("Zoho field check failed")
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        if not enabled():
            continue
        try:
            await tick()
        except Exception:  # noqa: BLE001
            logger.exception("leads watcher pass failed")


def purge() -> int:
    s = get_settings()
    return safe("retention", lambda: core.purge(s.retention_days, s.lead_retention_days), default=0) or 0
