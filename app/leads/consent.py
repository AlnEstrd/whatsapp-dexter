"""Consent welcome and email capture (WhatsApp side). The rules live here; main.py only routes.

Per lead (SQLite ``leads``, see core.set_consent):
    tacit_notice  the welcome was shown and the person did not object (set when it is shown)
    explicit      "Perfecto", or an email given (the email upgrades consent)
    declined      "No, gracias"
    opted_out     BAJA / STOP: final, stale taps and later forms never lift it

Per conversation (the session record, ``consent`` key): shown, pending (buttons still open for one
message: Twilio's 1/2 and labels count only then), menu_pending / menu_sent (the menu list goes out
once), email_asked (one email ask per conversation) and email_ask (a pending email answer, which
survives the farewell for EMAIL_WINDOW seconds).

Nothing here raises into a WhatsApp reply: callers wrap these in leads.safe().
Emails are never logged or put in events; events record only ``email_captured=true``.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .. import db, intents, sessions, store
from ..analytics import events
from ..config import get_settings
from ..flows import quiz, welcome
from . import core

logger = logging.getLogger("leads.consent")

EMAIL_WINDOW = 15 * 60          # an email typed after the farewell is still captured for this long
_EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")


def now() -> float:
    return sessions.now()


def enabled() -> bool:
    """The consent welcome: CONSENT_WELCOME_ENABLED and leads (which need the database)."""
    s = get_settings()
    return s.consent_welcome_enabled and s.leads_enabled and db.enabled()


def email_enabled() -> bool:
    s = get_settings()
    return s.email_capture_enabled and s.leads_enabled and db.enabled()


def state(uid: str) -> dict:
    return (sessions.get(uid) or {}).get("consent") or {}


def _note(uid: str, **fields) -> None:
    if sessions.get(uid) is not None:
        sessions.note(uid, consent={**state(uid), **fields})


# --------------------------------------------------------------------------- #
# The welcome
# --------------------------------------------------------------------------- #
def welcome_due(uid: str) -> bool:
    """Show the consent welcome now? Once per conversation; a new lead, or one whose stored copy
    version differs from the current one. Opt-outs never get it again."""
    if not enabled() or state(uid).get("shown"):
        return False
    lead = core.by_user(uid)
    if lead is None:
        return True
    if lead.get("opted_out_at") or lead.get("consent_type") == "opted_out":
        return False
    return lead.get("consent_text_version") != welcome.CONSENT_TEXT_VERSION


def show_welcome(uid: str, lang: str, profile_name: Optional[str]) -> None:
    """Create the lead row (overrides the meaningful-engagement rule) and record the notice."""
    lead, _ = core.ensure(uid, profile_name=profile_name, lang=lang, reason="welcome")
    lead_id = lead["lead_id"]
    if lead.get("consent_type") in (None, "tacit_notice"):
        core.set_consent(lead_id, "tacit_notice", version=welcome.CONSENT_TEXT_VERSION, via="welcome")
    else:  # a yes or a no already given stands; only the copy version moves on
        core.update(lead_id, consent_text_version=welcome.CONSENT_TEXT_VERSION)
    core.activity(lead_id, "welcome", 0, {"version": welcome.CONSENT_TEXT_VERSION, "lang": lang})
    events.record("consent_shown", uid, step="consent", lang=lang, version=welcome.CONSENT_TEXT_VERSION)
    core.recompute(lead_id)


def mark_shown(uid: str) -> None:
    """The welcome went out in this conversation: its buttons are open for the next message."""
    _note(uid, shown=True, pending=True, menu_pending=not state(uid).get("menu_sent"))


def in_conversation(uid: str) -> bool:
    return bool(state(uid).get("shown"))


def clear_pending(uid: str) -> None:
    _note(uid, pending=False)


def is_pending(uid: str) -> bool:
    return bool(state(uid).get("pending"))


def record_choice(uid: str, yes: bool, version: str, via: str) -> bool:
    """A tap (or a typed label) on the welcome's buttons. False after an opt-out: nothing changes."""
    lead = core.by_user(uid) or core.ensure(uid, reason="consent")[0]
    if lead.get("opted_out_at"):
        return False
    ctype = "explicit" if yes else "declined"
    core.set_consent(lead["lead_id"], ctype, version=version, via=via)
    core.recompute(lead["lead_id"])
    events.record("consent_choice", uid, step="consent", choice=ctype, version=version)
    _note(uid, shown=True, pending=False)
    return True


def note_tacit(uid: str) -> None:
    """The buttons were ignored and the person typed instead: tacit_notice (kept if already set)."""
    events.record("consent_choice", uid, step="consent", choice="tacit_notice")
    lead = core.by_user(uid)
    if lead is not None and lead.get("consent_type") is None:
        core.set_consent(lead["lead_id"], "tacit_notice", via="typed")


# --------------------------------------------------------------------------- #
# The menu list: once per conversation, after the welcome's answer or the first typed reply
# --------------------------------------------------------------------------- #
def mark_menu_sent(uid: str) -> None:
    _note(uid, menu_sent=True, menu_pending=False)


def menu_sent(uid: str) -> bool:
    return bool(state(uid).get("menu_sent"))


def menu_due(uid: str) -> bool:
    s = sessions.get(uid) or {}
    c = s.get("consent") or {}
    return bool(c.get("menu_pending") and not c.get("menu_sent") and s.get("state") == "open"
                and not store.in_handoff(uid) and not quiz.active(uid))


# --------------------------------------------------------------------------- #
# Email capture
# --------------------------------------------------------------------------- #
def digest_line(since: float) -> Optional[str]:
    """Consent mix and email capture rate of the leads that arrived since ``since`` (Slack digest)."""
    rows = db.query("""SELECT COALESCE(consent_type, 'none') AS t, COUNT(*) AS n,
                              SUM(CASE WHEN email IS NOT NULL THEN 1 ELSE 0 END) AS mail
                       FROM leads WHERE first_seen >= ? GROUP BY t""", (since,))
    if not rows:
        return None
    by = {r["t"]: r["n"] for r in rows}
    total = sum(by.values())
    mail = sum(r["mail"] or 0 for r in rows)
    return (f"Consentimiento (nuevos): {by.get('explicit', 0)} sí · {by.get('tacit_notice', 0)} aviso · "
            f"{by.get('declined', 0)} no · {by.get('opted_out', 0)} BAJA · correo: {mail} ({mail / total:.0%})")


def email_due(uid: str) -> bool:
    """Ask for an email now? Once per conversation, never in the quiz or human mode, never when one is
    stored or the person declined / opted out."""
    if not email_enabled() or state(uid).get("email_asked") or quiz.active(uid) or store.in_handoff(uid):
        return False
    lead = core.by_user(uid)
    return bool(lead and not lead.get("email") and not lead.get("opted_out_at")
                and lead.get("consent_type") not in ("declined", "opted_out"))


def mark_email_asked(uid: str, where: str, lang: str) -> None:
    _note(uid, email_asked=True, email_ask={"where": where, "lang": lang, "tries": 0})
    events.record("email_ask", uid, step="email", where=where)


def farewell_line(uid: str, lang: str) -> Optional[str]:
    """The extra line of a user-initiated farewell, when the ask is due (and it is now pending)."""
    if not email_due(uid):
        return None
    lang = lang if lang in ("es", "en") else "es"
    mark_email_asked(uid, "farewell", lang)
    return welcome.COPY["email_ask_farewell"][lang]


def pending_email(uid: str) -> Optional[dict]:
    """The pending email ask, if it still stands. After a farewell it lasts EMAIL_WINDOW."""
    s = sessions.get(uid) or {}
    ask = (s.get("consent") or {}).get("email_ask")
    if not ask:
        return None
    if s.get("state") != "open" and now() - (s.get("closed_at") or 0) > EMAIL_WINDOW:
        clear_email(uid)
        return None
    return ask


def clear_email(uid: str) -> None:
    _note(uid, email_ask=None)


def parse_email(text: str) -> tuple[Optional[str], bool]:
    """(the one valid address in the message or None, whether the message tries to be one).
    An attempt is any message with an "@" that did not give a valid address."""
    found = [m.rstrip(".").lower() for m in _EMAIL_FIND.findall(text or "")]
    if len(found) == 1 and core.valid_email(found[0]):
        return found[0], True
    return None, "@" in (text or "")


def capture(uid: str, email: str, via: str) -> bool:
    """Store the email and upgrade consent to explicit (unless declined or opted out). False when it
    cannot be stored: declined, opted out, or the address already belongs to another lead."""
    email = (email or "").strip().lower()
    if not core.valid_email(email):
        return False
    lead = core.by_user(uid) or core.ensure(uid, reason="email")[0]
    if lead.get("opted_out_at") or lead.get("consent_type") in ("declined", "opted_out"):
        return False
    other = core.by_email(email)
    if other is not None and other["lead_id"] != lead["lead_id"]:
        return False
    t = now()
    core.update(lead["lead_id"], email=email, email_consent_ts=t, email_text_version=welcome.CONSENT_TEXT_VERSION)
    core.set_consent(lead["lead_id"], "explicit", via="email", ts=t)
    core.activity(lead["lead_id"], "email_captured", 0, {"via": via})
    core.recompute(lead["lead_id"])
    events.record("email_captured", uid, step="email", email_captured=True, where=via)
    return True


def email_reply(uid: str, text: str) -> str:
    """Read a message while an email is pending, without a model call. Returns:
    "captured" (stored) | "skip" (Omitir / skip / no) | "retry" (invalid, asked once more) |
    "drop" (invalid again: the ask is dropped) | "pass" (not about the email: route as usual)."""
    ask = pending_email(uid)
    if ask is None:
        return "pass"
    if intents.normalize(text) in welcome.EMAIL_SKIP:
        clear_email(uid)
        return "skip"
    email, attempt = parse_email(text)
    if email and capture(uid, email, via=ask.get("where", "")):
        clear_email(uid)
        return "captured"
    if email or attempt:
        tries = int(ask.get("tries") or 0) + 1
        if tries < 2:
            _note(uid, email_ask={**ask, "tries": tries})
            return "retry"
        clear_email(uid)
        return "drop"
    clear_email(uid)
    return "pass"
