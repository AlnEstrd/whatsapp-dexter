"""Lead records: the system of record for follow-up (SQLite ``leads`` + ``lead_activity``).

Synchronous and DB-only on purpose: callers in the WhatsApp path wrap every call in
:func:`app.leads.safe` so a lead failure never breaks a reply. Side effects that talk to
the outside (Slack alerts, Zoho) run later from :func:`app.leads.effects` and the
background watcher, driven by the state written here (``hot_alerted_at``, ``zoho_dirty``).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import time
from typing import Any, Iterable, Optional

from .. import db
from ..config import get_settings
from . import scoring

logger = logging.getLogger("leads")

ROLE_LEVELS = ("owner_exec", "director", "manager", "individual", "unknown")
COMPANY_SIZES = ("1-10", "11-50", "51-250", "251+", "unknown")
# Lifecycle. Dexter moves leads up to "sql" by itself; the rest is set by Alan.
STAGES = ["new", "engaged", "mql", "sql", "contacted", "meeting", "proposal", "won", "lost", "nurture",
          "opted_out"]
DEXTER_STAGES = ("new", "engaged", "mql", "sql")
HUMAN_STAGES = ("contacted", "meeting", "proposal", "won", "lost", "nurture")
CONTACT_CHANNELS = ("personal_whatsapp", "call", "email", "dexter_template")
PROFILE_FIELDS = ("name", "company", "role", "role_level", "company_size", "industry", "email",
                  "interest_note")
DECLINE_PAUSE_DAYS = 30
MAX_ASKS = 2
WINDOW_SECONDS = 24 * 3600

try:
    from zoneinfo import ZoneInfo
    MX = ZoneInfo("America/Mexico_City")
except Exception:  # noqa: BLE001
    MX = dt.timezone(dt.timedelta(hours=-6))


def now() -> float:
    return time.time()


def enabled() -> bool:
    return get_settings().leads_enabled and db.enabled()


def mx_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, MX).strftime("%Y-%m-%d")


def mask(user_id: Optional[str]) -> str:
    return "••" + (user_id or "")[-4:] if user_id else "—"


# --------------------------------------------------------------------------- #
# Scrubbing (free text the agent or a form writes)
# --------------------------------------------------------------------------- #
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def scrub(text: Optional[str], limit: int = 200) -> str:
    """Drop emails, URLs, phone numbers and long digit runs; collapse whitespace; cap length."""
    t = _EMAIL.sub("[email]", text or "")
    t = _URL.sub("[link]", t)
    t = _PHONE.sub("[número]", t)
    t = re.sub(r"\d{6,}", "[número]", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def clean_text(value: Any, limit: int = 80) -> Optional[str]:
    t = re.sub(r"\s+", " ", str(value or "")).strip()
    return t[:limit] if t else None


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


def valid_email(email: Optional[str]) -> bool:
    return bool(email and len(email) <= 254 and EMAIL_RE.match(email.strip()))


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
_JSON_FIELDS = ("quiz_scores_json", "weakest_domains_json", "topics_json")


def _dict(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for k in _JSON_FIELDS:
        try:
            d[k[:-5]] = json.loads(d.get(k) or ("{}" if k == "topics_json" else "null"))
        except ValueError:
            d[k[:-5]] = None
    d["consent_followup"] = bool(d.get("consent_followup"))
    d["contactable"] = bool(d.get("contactable"))
    d["zoho_dirty"] = bool(d.get("zoho_dirty"))
    return d


def get(lead_id: str) -> Optional[dict]:
    rows = db.query("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    return _dict(rows[0]) if rows else None


def by_user(user_id: str) -> Optional[dict]:
    if not user_id:
        return None
    rows = db.query("SELECT * FROM leads WHERE user_id = ?", (user_id,))
    return _dict(rows[0]) if rows else None


def by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    rows = db.query("SELECT * FROM leads WHERE email = ?", (email.strip().lower(),))
    return _dict(rows[0]) if rows else None


def _new_id() -> str:
    return "L" + secrets.token_hex(4)


def update(lead_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    for k in list(fields):
        if k in ("quiz_scores", "weakest_domains", "topics"):
            fields[f"{k}_json"] = db.dumps(fields.pop(k))
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE leads SET {cols} WHERE lead_id = ?", (*fields.values(), lead_id))


def activity(lead_id: str, type_: str, points: int = 0, data: Optional[dict] = None,
             ts: Optional[float] = None) -> None:
    db.execute("INSERT INTO lead_activity (lead_id, ts, type, points, data_json) VALUES (?, ?, ?, ?, ?)",
               (lead_id, now() if ts is None else ts, type_, int(points), db.dumps(data or {})))


def activity_rows(lead_id: str) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM lead_activity WHERE lead_id = ? ORDER BY ts, id", (lead_id,)):
        d = dict(r)
        try:
            d["data"] = json.loads(d.pop("data_json") or "{}")
        except ValueError:
            d["data"] = {}
        out.append(d)
    return out


def is_suppressed(user_id: Optional[str] = None, email: Optional[str] = None) -> bool:
    keys = [k for k in (user_id and f"wa:{user_id}", email and f"email:{email.lower()}") if k]
    if not keys:
        return False
    q = "SELECT 1 FROM lead_suppressions WHERE key IN (%s)" % ",".join("?" * len(keys))
    return bool(db.query(q, keys))


def ensure(user_id: Optional[str], *, profile_name: Optional[str] = None, lang: Optional[str] = None,
           source: str = "organic", email: Optional[str] = None, reason: str = "") -> tuple[dict, bool]:
    """The lead for ``user_id`` (or ``email`` when there is no phone), created if missing.
    Returns (lead, created)."""
    lead = by_user(user_id) if user_id else None
    if lead is None and email and not user_id:
        lead = by_email(email)
    if lead is not None:
        changes = {}
        if profile_name and profile_name != lead.get("wa_profile_name"):
            changes["wa_profile_name"] = clean_text(profile_name, 80)
        if lang and lang != lead.get("lang"):
            changes["lang"] = lang
        if changes:
            update(lead["lead_id"], **changes)
            lead.update(changes)
        return lead, False
    t = now()
    lead_id = _new_id()
    opted = None
    if is_suppressed(user_id, email):
        opted = t
    db.execute(
        """INSERT INTO leads (lead_id, user_id, wa_profile_name, lang, source, email, first_seen, last_seen,
               last_inbound_at, opted_out_at, stage, stage_updated_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, user_id or None, clean_text(profile_name, 80), lang, source,
         email.strip().lower() if email else None, t, t, t if user_id and source == "organic" else None,
         opted, "opted_out" if opted else "new", t, t, t))
    activity(lead_id, "created", 0, {"reason": reason, "source": source})
    return get(lead_id), True


# --------------------------------------------------------------------------- #
# Engagement, sessions, topics
# --------------------------------------------------------------------------- #
def add_intent(lead_id: str, kind: str, data: Optional[dict] = None) -> int:
    """Log an intent signal; returns the points it earned (0 once its max is reached)."""
    earned = db.query("SELECT COUNT(*) FROM lead_activity WHERE lead_id = ? AND type = ? AND points > 0",
                      (lead_id, kind))[0][0]
    pts = scoring.points_for(kind, earned)
    activity(lead_id, kind, pts, data)
    return pts


def count_activity(lead_id: str, type_: str) -> int:
    return db.query("SELECT COUNT(*) FROM lead_activity WHERE lead_id = ? AND type = ?",
                    (lead_id, type_))[0][0]


def touch_session(lead: dict, opened_at: Optional[float]) -> None:
    """Count a new conversation; a new one on another (Mexico City) day is a returning visit."""
    if not opened_at:
        return
    last = lead.get("last_session_at")
    if last and opened_at <= last + 1:
        return
    changes: dict[str, Any] = {"last_session_at": opened_at}
    if last:
        changes["sessions"] = int(lead.get("sessions") or 1) + 1
        if mx_day(opened_at) != mx_day(last):
            add_intent(lead["lead_id"], "returning_visit", {"day": mx_day(opened_at)})
    update(lead["lead_id"], **changes)
    lead.update(changes)


def touch_inbound(user_id: str, opened_at: Optional[float] = None) -> Optional[dict]:
    """Inbound WhatsApp message from an existing lead: last_seen, window, sessions."""
    lead = by_user(user_id)
    if lead is None:
        return None
    t = now()
    update(lead["lead_id"], last_seen=t, last_inbound_at=t)
    lead.update(last_seen=t, last_inbound_at=t)
    touch_session(lead, opened_at)
    return lead


def engage(user_id: str, reason: str, *, profile_name: Optional[str] = None, lang: Optional[str] = None,
           opened_at: Optional[float] = None) -> dict:
    """Meaningful engagement (quiz start, agent question, handoff, file, disclosure)."""
    lead, created = ensure(user_id, profile_name=profile_name, lang=lang, reason=reason)
    if created:
        if opened_at:
            update(lead["lead_id"], last_session_at=opened_at)
            lead["last_session_at"] = opened_at
    else:
        update(lead["lead_id"], last_seen=now())
        touch_session(lead, opened_at)
    return lead


def add_topic(lead_id: str, topic: str) -> None:
    lead = get(lead_id)
    if lead is None or not topic:
        return
    topics = dict(lead.get("topics") or {})
    topics[topic] = int(topics.get(topic, 0)) + 1
    top = dict(sorted(topics.items(), key=lambda kv: -kv[1])[:10])
    update(lead_id, topics=top)


def record_quiz(lead_id: str, result) -> None:
    """Quiz finished: keep level, per-domain scores, weakest domains; intent points."""
    update(lead_id, quiz_level=int(result.level), quiz_scores=dict(result.answers),
           weakest_domains=list(result.weakest))
    add_intent(lead_id, "quiz_completed", {"level": result.level, "average": result.average})
    if result.level <= 1:
        add_intent(lead_id, "quiz_need", {"level": result.level})


def set_profile(lead_id: str, source: str = "agent", **fields: Any) -> list[str]:
    """Fill profile fields (never blanks an existing value). Returns the fields changed."""
    lead = get(lead_id)
    if lead is None:
        return []
    changes: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in PROFILE_FIELDS or v is None or v == "":
            continue
        if k == "role_level":
            v = v if v in ROLE_LEVELS else None
        elif k == "company_size":
            v = v if v in COMPANY_SIZES else None
        elif k == "email":
            v = v.strip().lower() if valid_email(v) else None
            if v and (other := by_email(v)) and other["lead_id"] != lead_id:
                v = None  # belongs to another lead; never steal it
        elif k == "interest_note":
            v = scrub(v, 200) or None
        else:
            v = clean_text(v, 120 if k == "industry" else 80)
        if v is not None and v != lead.get(k):
            changes[k] = v
    if not changes:
        return []
    before = scoring.fit_score(lead)
    update(lead_id, **changes)
    lead.update(changes)
    delta = scoring.fit_score(lead) - before
    activity(lead_id, "profile", 0, {"fields": sorted(changes), "source": source})
    if delta:
        activity(lead_id, "fit_change", delta, {"fields": sorted(changes), "fit": scoring.fit_score(lead)})
    return sorted(changes)


# --------------------------------------------------------------------------- #
# Consent and opt-out
# --------------------------------------------------------------------------- #
def consent_askable(lead: Optional[dict]) -> bool:
    """Lead-level rules: never after yes or opt-out, max 2 asks, 30 days after a no."""
    if lead is None or lead.get("consent_followup") or lead.get("opted_out_at"):
        return False
    if is_suppressed(lead.get("user_id"), lead.get("email")):
        return False
    if int(lead.get("consent_asked_count") or 0) >= MAX_ASKS:
        return False
    declined = lead.get("consent_declined_at")
    return not (declined and now() - declined < DECLINE_PAUSE_DAYS * 86400)


def mark_asked(lead_id: str, moment: str) -> None:
    lead = get(lead_id)
    update(lead_id, consent_asked_count=int(lead.get("consent_asked_count") or 0) + 1,
           consent_last_asked_at=now())
    activity(lead_id, "consent_asked", 0, {"moment": moment})


def consent_on() -> bool:
    """CONSENT_WELCOME_ENABLED: consent_type and follow-up status are written only then."""
    return get_settings().consent_welcome_enabled


CONSENTED = ("explicit", "tacit_notice")  # Alan may write once personally (contactable_personal)


def contactable_personal(lead: dict) -> bool:
    """Explicit yes, or the welcome shown without a "no" (tacit notice): one personal follow-up."""
    return lead.get("consent_type") in CONSENTED and not lead.get("opted_out_at")


def followup_status_for(lead: dict) -> Optional[str]:
    if lead.get("opted_out_at") or lead.get("consent_type") in ("declined", "opted_out"):
        return "not_needed"
    if lead.get("contacted_at"):
        return "contacted"
    return "pending" if contactable_personal(lead) else None


def sync_followup(lead_id: str) -> None:
    lead = get(lead_id)
    if lead is not None:
        status = followup_status_for(lead)
        if status != lead.get("followup_status"):
            update(lead_id, followup_status=status)


def set_consent(lead_id: str, ctype: str, *, version: Optional[str] = None, via: str = "",
                ts: Optional[float] = None) -> None:
    """Consent state from the welcome, a button, an email or a form: explicit | tacit_notice | declined |
    opted_out. consent_ts is the time of the welcome for a tacit notice, of the answer otherwise.
    BAJA is final: a stale tap or a later form never lifts an opt-out."""
    lead = get(lead_id)
    if lead is None or (lead.get("opted_out_at") and ctype != "opted_out"):
        return
    t = now() if ts is None else ts
    changes: dict[str, Any] = {"consent_type": ctype, "consent_followup": int(ctype == "explicit"),
                               "contactable": int(ctype == "explicit")}  # contactable = explicit (see recompute)
    if ctype in ("explicit", "tacit_notice", "declined"):
        changes["consent_ts"] = t
    if version:
        changes["consent_text_version"] = version
    if ctype == "declined":
        changes["consent_declined_at"] = t
    elif ctype == "explicit":
        changes["consent_declined_at"] = None
    update(lead_id, **changes)
    if bool(lead.get("contactable")) != bool(changes["contactable"]):
        mark_dirty(lead_id)  # recompute() would not see the change: the Zoho outbox must hear about it
    activity(lead_id, "consent", 0, {"to": ctype, "via": via, "version": version})
    sync_followup(lead_id)


def record_consent(lead_id: str, yes: bool, version: str, via: str) -> None:
    """A yes from a form (contact request, web form) or a consent button; a no from a button."""
    if consent_on():
        set_consent(lead_id, "explicit" if yes else "declined", version=version, via=via)
        return
    t = now()
    if yes:
        lead = get(lead_id)
        changes: dict[str, Any] = dict(consent_followup=1, consent_ts=t, consent_text_version=version,
                                       consent_declined_at=None)
        if lead and lead.get("opted_out_at"):  # explicit new consent lifts an earlier opt-out
            changes.update(opted_out_at=None, stage="engaged", stage_source="dexter", stage_updated_at=t)
            keys = [k for k in (lead.get("user_id") and f"wa:{lead['user_id']}",
                                lead.get("email") and f"email:{lead['email']}") if k]
            for k in keys:
                db.execute("DELETE FROM lead_suppressions WHERE key = ?", (k,))
        update(lead_id, **changes)
        activity(lead_id, "consent_yes", 0, {"version": version, "via": via})
    else:
        update(lead_id, consent_declined_at=t)
        activity(lead_id, "consent_no", 0, {"version": version, "via": via})


def suppress(user_id: Optional[str], email: Optional[str] = None) -> None:
    t = now()
    for k in (user_id and f"wa:{user_id}", email and f"email:{email.lower()}"):
        if k:
            db.execute("INSERT OR IGNORE INTO lead_suppressions (key, opted_out_at) VALUES (?, ?)", (k, t))


def opt_out(user_id: str, via: str = "keyword") -> Optional[dict]:
    """BAJA: never contact again. Returns the lead (None if this number never became one, legacy mode)."""
    suppress(user_id)
    lead = by_user(user_id)
    if lead is None:
        if not consent_on():
            return None
        lead, _ = ensure(user_id, reason="opt_out")  # suppressed just above: created as opted out
    t = now()
    changes: dict[str, Any] = dict(opted_out_at=t, consent_followup=0, contactable=0, stage="opted_out",
                                   stage_source="dexter", stage_updated_at=t)
    if consent_on():
        changes["consent_type"] = "opted_out"
    update(lead["lead_id"], **changes)
    activity(lead["lead_id"], "opted_out", 0, {"via": via, "from_stage": lead.get("stage")})
    close_requests(lead["lead_id"], status="cancelled")
    if consent_on():
        sync_followup(lead["lead_id"])
    mark_dirty(lead["lead_id"], force=True)
    return get(lead["lead_id"])


# --------------------------------------------------------------------------- #
# Scoring, tiers, stages
# --------------------------------------------------------------------------- #
def _stage_idx(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else 0


def recompute(lead_id: str) -> Optional[dict]:
    """Recompute fit/intent/score/tier/contactable and move the stage forward (never past sql,
    never over a stage Alan set). Returns the updated lead, with ``_tier_changed``."""
    lead = get(lead_id)
    if lead is None:
        return None
    rows = activity_rows(lead_id)
    fit = scoring.fit_score(lead)
    intent = scoring.intent_score(rows)
    total = fit + intent
    tier = scoring.tier_for(total, lead.get("tier_override"))
    contactable = (bool(lead.get("consent_followup")) or lead.get("consent_type") == "explicit") \
        and not lead.get("opted_out_at")
    changes: dict[str, Any] = {}
    for k, v in (("fit_score", fit), ("intent_score", intent), ("score", total), ("tier", tier),
                 ("contactable", int(contactable))):
        if (int(lead[k]) if k == "contactable" else lead[k]) != v:
            changes[k] = v
    stage = lead.get("stage") or "new"
    if stage in DEXTER_STAGES:
        target = "new"
        if intent > 0 or fit > 0 or lead.get("quiz_level") is not None:
            target = "engaged"
        if contactable and tier == "warm":
            target = "mql"
        if contactable and tier == "hot":
            target = "sql"
        if _stage_idx(target) > _stage_idx(stage):
            changes.update(stage=target, stage_source="dexter", stage_updated_at=now())
            activity(lead_id, "stage", 0, {"from": stage, "to": target, "source": "dexter"})
    tier_changed = "tier" in changes
    if tier_changed:
        activity(lead_id, "tier", 0, {"from": lead.get("tier"), "to": tier, "score": total})
    if changes:
        update(lead_id, **changes)
    if tier_changed or "stage" in changes or "contactable" in changes:
        mark_dirty(lead_id)
    lead = get(lead_id)
    lead["_tier_changed"] = tier_changed
    return lead


def set_stage(lead_id: str, stage: str, source: str, *, channel: Optional[str] = None,
              note: Optional[str] = None, sync: bool = True) -> Optional[dict]:
    """Stage set by a person (slack | zoho | admin | dashboard). Dexter uses :func:`recompute`."""
    lead = get(lead_id)
    if lead is None or stage not in STAGES:
        return None
    t = now()
    data = {"from": lead.get("stage"), "to": stage, "source": source}
    if channel:
        data["channel"] = channel
    if note:
        data["note"] = scrub(note, 300)
    update(lead_id, stage=stage, stage_source=source, stage_updated_at=t)
    activity(lead_id, "stage", 0, data)
    if stage in HUMAN_STAGES:
        close_requests(lead_id)
    if sync:  # a stage pulled from Zoho isn't pushed back
        mark_dirty(lead_id)
    return get(lead_id)


def add_note(lead_id: str, text: str, source: str) -> None:
    activity(lead_id, "note", 0, {"text": scrub(text, 500), "source": source})


def set_tier_override(lead_id: str, tier: str, reason: str) -> None:
    update(lead_id, tier_override=tier)
    activity(lead_id, "tier_override", 0, {"tier": tier, "reason": reason})


# --------------------------------------------------------------------------- #
# Zoho dirty flag (the outbox lives in app/leads/zoho.py)
# --------------------------------------------------------------------------- #
def in_sync_scope(lead: dict) -> bool:
    if get_settings().zoho_sync_scope == "all_engaged":
        return (lead.get("stage") or "new") != "new" or bool(lead.get("zoho_id"))
    return bool(lead.get("contactable")) or bool(lead.get("zoho_id"))


def mark_dirty(lead_id: str, force: bool = False) -> None:
    update(lead_id, zoho_dirty=1)
    if get_settings().zoho_enabled:
        from . import zoho
        lead = get(lead_id)
        if lead and (force or in_sync_scope(lead)):
            zoho.enqueue(lead_id, "upsert")


# --------------------------------------------------------------------------- #
# WhatsApp window, contact requests
# --------------------------------------------------------------------------- #
def window(lead: dict) -> tuple[bool, float]:
    """(open, seconds left) of the 24 h customer-service window."""
    last = lead.get("last_inbound_at")
    if not last or not lead.get("user_id"):
        return False, 0.0
    left = last + WINDOW_SECONDS - now()
    return left > 0, max(0.0, left)


def window_text(lead: dict) -> str:
    is_open, left = window(lead)
    if not is_open:
        return "cerrada (se necesita plantilla)"
    h, m = int(left // 3600), int(left % 3600 // 60)
    return f"abierta ({h} h {m:02d} min)"


def add_contact_request(lead_id: str, channel: str, best_time: Optional[str], topic: Optional[str],
                        note: Optional[str]) -> int:
    cur = db.execute("""INSERT INTO contact_requests (lead_id, ts, channel, best_time, topic, note, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'open')""",
                     (lead_id, now(), channel, best_time, topic, scrub(note, 1000) if note else None))
    activity(lead_id, "contact_request", 0, {"channel": channel, "best_time": best_time, "topic": topic})
    return cur.lastrowid


def open_requests(lead_id: Optional[str] = None) -> list[dict]:
    if lead_id:
        rows = db.query("SELECT * FROM contact_requests WHERE lead_id = ? AND status = 'open' ORDER BY ts",
                        (lead_id,))
    else:
        rows = db.query("SELECT * FROM contact_requests WHERE status = 'open' ORDER BY ts")
    return [dict(r) for r in rows]


def close_requests(lead_id: str, status: str = "done") -> int:
    cur = db.execute("UPDATE contact_requests SET status = ?, done_at = ? WHERE lead_id = ? AND status = 'open'",
                     (status, now(), lead_id))
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Queries (dashboard, admin, digest)
# --------------------------------------------------------------------------- #
def list_leads(*, tier: Optional[str] = None, stage: Optional[str] = None, contactable: Optional[bool] = None,
               source: Optional[str] = None, since: Optional[float] = None, q: Optional[str] = None,
               limit: int = 200) -> list[dict]:
    where, params = [], []
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if stage:
        where.append("stage = ?")
        params.append(stage)
    if contactable is not None:
        where.append("contactable = ?")
        params.append(int(contactable))
    if source:
        where.append("source = ?")
        params.append("organic" if source == "whatsapp" else source)
    if since:
        where.append("last_seen >= ?")
        params.append(since)
    if q:
        where.append("(LOWER(COALESCE(name,'')) LIKE ? OR LOWER(COALESCE(company,'')) LIKE ? "
                     "OR LOWER(COALESCE(wa_profile_name,'')) LIKE ?)")
        params += [f"%{q.lower()}%"] * 3
    sql = "SELECT * FROM leads" + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY score DESC, last_seen DESC LIMIT ?"
    return [_dict(r) for r in db.query(sql, (*params, int(limit)))]


def hot_not_reached(quiet_hours: float = 48) -> list[dict]:
    """Contactable, hot, still 'sql' and no inbound message from them in the last 48 h."""
    cutoff = now() - quiet_hours * 3600
    rows = db.query("""SELECT * FROM leads WHERE contactable = 1 AND tier = 'hot' AND stage = 'sql'
                       AND COALESCE(last_inbound_at, 0) < ? ORDER BY score DESC""", (cutoff,))
    return [_dict(r) for r in rows]


def funnel(since: Optional[float] = None) -> dict:
    """engaged -> consented -> warm -> hot -> contacted -> meeting -> won, with rates."""
    leads = list_leads(since=since, limit=100000)
    acts = db.query("SELECT lead_id, type, ts, data_json FROM lead_activity WHERE type IN ('stage', 'tier', 'opted_out')")
    ever_contacted, ever_meeting, ever_won = set(), set(), set()
    contacted_at: dict[str, float] = {}
    hot_at: dict[str, float] = {}
    for a in acts:
        d = json.loads(a["data_json"] or "{}")
        if a["type"] == "stage":
            if d.get("to") in HUMAN_STAGES and d.get("to") not in ("lost", "nurture"):
                ever_contacted.add(a["lead_id"])
                contacted_at.setdefault(a["lead_id"], a["ts"])
            if d.get("to") in ("meeting", "proposal", "won"):
                ever_meeting.add(a["lead_id"])
            if d.get("to") == "won":
                ever_won.add(a["lead_id"])
        elif a["type"] == "tier" and d.get("to") == "hot":
            hot_at.setdefault(a["lead_id"], a["ts"])
    ids = {x["lead_id"] for x in leads}
    engaged = len(leads)
    consented = sum(1 for x in leads if x["consent_followup"])  # explicit yes (a tacit notice is not a yes)
    opted = sum(1 for x in leads if x["opted_out_at"])
    warm = sum(1 for x in leads if x["tier"] in ("warm", "hot"))
    hot = sum(1 for x in leads if x["tier"] == "hot")
    times = sorted(contacted_at[i] - (hot_at.get(i) or contacted_at[i]) for i in contacted_at
                   if i in ids and i in hot_at)
    median = times[len(times) // 2] / 3600 if times else None
    sla = get_settings().lead_sla_hours * 3600
    breaches = sum(1 for x in leads if x["tier"] == "hot" and x["contactable"] and x["stage"] == "sql"
                   and x.get("hot_alerted_at") and now() - x["hot_alerted_at"] > sla)
    return {
        "engaged": engaged, "consented": consented, "warm": warm, "hot": hot,
        "contacted": len(ever_contacted & ids), "meeting": len(ever_meeting & ids),
        "won": len(ever_won & ids),
        "consent_rate": round(consented / engaged, 3) if engaged else 0.0,
        "opt_out_rate": round(opted / engaged, 3) if engaged else 0.0,
        "median_hours_to_contact_hot": round(median, 1) if median is not None else None,
        "sla_breaches": breaches,
    }


# --------------------------------------------------------------------------- #
# Retention and erase
# --------------------------------------------------------------------------- #
def _delete(lead_ids: Iterable[str]) -> None:
    with db.transaction() as c:
        for lid in lead_ids:
            for table in ("lead_activity", "zoho_outbox", "contact_requests", "leads"):
                c.execute(f"DELETE FROM {table} WHERE lead_id = ?", (lid,))


def purge(retention_days: float, lead_retention_days: float) -> int:
    """Leads with no consent, declined or opted out go with the conversation data (RETENTION_DAYS).
    Explicit consent and the tacit notice keep LEAD_RETENTION_DAYS from last_seen. An opt-out keeps only
    its suppression record (lead_suppressions), which outlives the row."""
    t = now()
    consented = ("(COALESCE(consent_type, '') IN ('explicit', 'tacit_notice') OR contactable = 1) "
                 "AND opted_out_at IS NULL")
    rows = db.query(f"""SELECT lead_id, zoho_id, user_id, opted_out_at FROM leads WHERE
                         ({consented} AND COALESCE(last_seen, created_at) < ?)
                      OR (NOT ({consented}) AND COALESCE(last_seen, created_at) < ?)""",
                    (t - lead_retention_days * 86400, t - retention_days * 86400))
    ids = [r["lead_id"] for r in rows]
    for r in rows:
        if r["opted_out_at"] and r["user_id"]:
            suppress(r["user_id"])  # keeps the record even for an opt-out that predates the table
    if ids:
        zoho_ids = [r["zoho_id"] for r in rows if r["zoho_id"]]
        _delete(ids)
        if zoho_ids and get_settings().zoho_enabled:
            from . import zoho
            for zid in zoho_ids:
                zoho.enqueue("-", "delete", {"zoho_id": zid})
        logger.info("Lead retention: purged %d lead(s)", len(ids))
    return len(ids)


def erase(lead_id: str) -> Optional[dict]:
    """ARCO erase. Keeps the suppression record only if the person opted out."""
    lead = get(lead_id)
    if lead is None:
        return None
    _delete([lead_id])
    if not lead.get("opted_out_at"):
        for k in (lead.get("user_id") and f"wa:{lead['user_id']}", lead.get("email") and f"email:{lead['email']}"):
            if k:
                db.execute("DELETE FROM lead_suppressions WHERE key = ?", (k,))
    return lead
