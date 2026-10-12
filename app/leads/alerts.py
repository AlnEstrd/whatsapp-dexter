"""Speed to lead in Slack: hot alerts, thread commands, SLA / window reminders, daily digest.

- Hot alert: once per lead, top-level in SLACK_LEADS_CHANNEL_ID (fallback SLACK_CHANNEL_ID),
  mentioning SLACK_ALERT_USER_ID, when a contactable lead is hot.
- Commands, in the lead's conversation thread or in its alert thread:
  ``contacted`` ``meeting`` ``proposal`` ``won`` ``lost`` ``nurture`` ``note <text>`` ``followup``
  (then ``✅ confirm``).
- Reminders: hot + contactable + still ``sql`` after LEAD_SLA_HOURS -> one mention; one more
  2 h before the WhatsApp 24 h window closes, if it is still open.
- Digest: once a day at METRICS_DIGEST_HOUR (Mexico City), skipped on empty days.

Without the Slack app (bot token) the alert and digest go through the Incoming Webhook.
Every function here logs and carries on: Slack never breaks WhatsApp handling.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Optional

from .. import db, slack_bridge, store
from ..config import get_settings
from ..escalation import notify_slack
from ..flows import quiz
from . import consent, core, flows, followups, scoring

logger = logging.getLogger("leads.alerts")

ROLE_LABEL = {"owner_exec": "Dueño(a)/Dir. general", "director": "Dirección de área", "manager": "Gerencia",
              "individual": "Otro rol"}
SIZE_LABEL = {"1-10": "1–10 personas", "11-50": "11–50 personas", "51-250": "51–250 personas",
              "251+": "más de 250 personas"}
STAGE_COMMANDS = ("contacted", "meeting", "proposal", "won", "lost", "nurture")
COMMAND_HELP = "Comandos en este hilo: `contacted` · `meeting` · `proposal` · `won` · `lost` · `nurture` · `note <texto>` · `followup`"
CONFIRM_SECONDS = 600
WINDOW_REMIND_BEFORE = 2 * 3600

_pending_followups: dict[str, tuple[str, float]] = {}  # thread_ts -> (lead_id, expires)


def _owner() -> str:
    uid = get_settings().slack_alert_user_id
    return f"<@{uid}>" if uid else "<!here>"


def permalink(channel: Optional[str], ts: Optional[str]) -> Optional[str]:
    if not channel or not ts:
        return None
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def conversation_link(lead: dict) -> Optional[str]:
    t = store.get_thread(lead.get("user_id") or "") if lead.get("user_id") else None
    return permalink(t.get("channel_id"), t.get("thread_ts")) if t else None


def dashboard_link(lead: dict) -> Optional[str]:
    s = get_settings()
    if not s.dashboard_active:
        return None
    return f"{s.dashboard_base_url.rstrip('/')}/dashboard/leads/{lead['lead_id']}"


def domain_names(keys: list, lang: str = "es") -> str:
    names = {q["key"]: q["domain"][lang] for q in quiz.QUESTIONS}
    return ", ".join(names.get(k, k) for k in (keys or [])[:2]) or "—"


def who(lead: dict) -> str:
    parts = [lead.get("name") or lead.get("wa_profile_name"), lead.get("company"),
             ROLE_LABEL.get(lead.get("role_level") or ""), SIZE_LABEL.get(lead.get("company_size") or "")]
    parts = [slack_bridge.escape(p) for p in parts if p]
    return " · ".join(parts) or "sin datos"


def summary(lead: dict, rows: Optional[list] = None) -> list[str]:
    """The lead summary shared by the Slack alert and the Zoho note (no transcript)."""
    rows = core.activity_rows(lead["lead_id"]) if rows is None else rows
    lines = [f"*Score {lead['score']}* ({lead['tier']}; fit {lead['fit_score']} + intent {lead['intent_score']}): "
             + scoring.top_reasons(lead, rows)]
    if lead.get("quiz_level") is not None:
        lines.append(f"Diagnóstico: L{lead['quiz_level']} · débiles: {domain_names(lead.get('weakest_domains') or [])}")
    if lead.get("interest_note"):
        lines.append(f"Nota: {slack_bridge.escape(lead['interest_note'])}")
    reqs = core.open_requests(lead["lead_id"])
    if reqs:
        r = reqs[-1]
        lines.append(f"📇 Solicitud: {flows.contact_summary(r)}"
                     + (f" · {slack_bridge.escape(r['note'])}" if r.get("note") else ""))
    if lead.get("user_id"):
        lines.append(f"WhatsApp: ventana {core.window_text(lead)}")
    elif lead.get("email"):
        lines.append("Solo correo (sin WhatsApp)")
    if lead.get("consent_ts"):
        lines.append("Consentimiento: " + dt.datetime.fromtimestamp(lead["consent_ts"], core.MX).strftime("%Y-%m-%d %H:%M")
                     + f" (v {lead.get('consent_text_version')})")
    return lines


def alert_text(lead: dict, tag: Optional[str] = None) -> str:
    head = f"🔥 {_owner()} {tag + ' · ' if tag else ''}Lead caliente: {who(lead)}"
    lines = [head, *summary(lead)]
    if (link := conversation_link(lead)):
        lines.append(f"Conversación: <{link}|abrir hilo>")
    if (link := dashboard_link(lead)):
        lines.append(f"Dashboard: <{link}|abrir lead>")
    lines.append(COMMAND_HELP)
    return "\n".join(lines)


async def post(text: str, thread_ts: Optional[str] = None) -> Optional[str]:
    """Post in the leads channel (bot token) or, without the Slack app, via the webhook."""
    s = get_settings()
    if s.slack_bot_token and s.leads_channel_id:
        return await slack_bridge.post_message(s.leads_channel_id, text, thread_ts=thread_ts)
    if thread_ts is None:
        await notify_slack(text)
    return None


async def thread_note(lead: dict, text: str) -> None:
    """Owner-facing note in the lead's conversation thread (or alert thread)."""
    try:
        uid = lead.get("user_id")
        if uid and slack_bridge.enabled() and store.get_thread(uid):
            await slack_bridge._thread_post(uid, text)  # noqa: SLF001
        elif lead.get("slack_alert_ts"):
            await post(text, thread_ts=lead["slack_alert_ts"])
        elif not slack_bridge.enabled():
            await notify_slack(f"{lead.get('name') or lead.get('wa_profile_name') or core.mask(uid)}: {text}")
    except Exception:  # noqa: BLE001
        logger.exception("lead thread note failed")


async def hot_alert(lead_id: str, tag: Optional[str] = None, force: bool = False) -> bool:
    """Post the hot alert once (``force``: a new contact request re-alerts)."""
    lead = core.get(lead_id)
    if lead is None or not lead.get("contactable") or lead.get("tier") != "hot":
        return False
    if lead.get("hot_alerted_at") and not force:
        return False
    core.update(lead_id, hot_alerted_at=core.now())  # before posting: never twice, even on a slow Slack
    try:
        ts = await post(alert_text(lead, tag))
        if ts:
            core.update(lead_id, slack_alert_ts=ts, slack_alert_channel=get_settings().leads_channel_id)
        core.activity(lead_id, "hot_alert", 0, {"tag": tag or ""})
        from . import zoho
        if get_settings().zoho_enabled:
            zoho.enqueue(lead_id, "note", {"title": f"Dexter: lead caliente{' · ' + tag if tag else ''}",
                                            "text": "\n".join(summary(core.get(lead_id)))})
        return True
    except Exception:  # noqa: BLE001
        logger.exception("hot alert failed for %s", lead_id)
        return False


# --------------------------------------------------------------------------- #
# Commands from Slack threads
# --------------------------------------------------------------------------- #
def lead_for_thread(thread_ts: str) -> Optional[dict]:
    uid = store.user_for_thread(thread_ts)
    if uid:
        return core.by_user(uid)
    rows = db.query("SELECT lead_id FROM leads WHERE slack_alert_ts = ?", (thread_ts,))
    return core.get(rows[0]["lead_id"]) if rows else None


def parse_command(text: str) -> Optional[tuple[str, str]]:
    t = (text or "").strip()
    low = t.lower().replace(":white_check_mark:", "").replace("✅", "").strip()
    if low in STAGE_COMMANDS or low in ("followup", "confirm"):
        return low, ""
    if low.startswith("note ") and len(t) > 5:
        return "note", t.split(None, 1)[1]
    return None


async def handle_command(channel: Optional[str], thread_ts: str, text: str, by: str) -> bool:
    """Slack thread message -> lead command. False when it isn't one (normal handling)."""
    if not core.enabled():
        return False
    cmd = parse_command(text)
    if cmd is None:
        return False
    try:
        lead = db.safe("lead for thread", lambda: lead_for_thread(thread_ts))
    except Exception:  # noqa: BLE001
        lead = None
    if lead is None:
        return False
    name, arg = cmd
    reply_channel = channel or get_settings().slack_channel_id

    async def reply(msg: str) -> None:
        await slack_bridge.post_message(reply_channel, msg, thread_ts=thread_ts)

    lid = lead["lead_id"]
    if name in STAGE_COMMANDS:
        db.safe("set stage", lambda: core.set_stage(lid, name, "slack", note=f"by <@{by}>"))
        await reply(f"✅ Etapa → *{name}*" + (" · Zoho se actualiza en breve." if get_settings().zoho_enabled else "."))
    elif name == "note":
        db.safe("note", lambda: core.add_note(lid, arg, "slack"))
        if get_settings().zoho_enabled:
            from . import zoho
            db.safe("zoho note", lambda: zoho.enqueue(lid, "note", {"title": "Nota de Alan (Slack)", "text": arg}))
        await reply("📝 Nota guardada.")
    elif name == "followup":
        from . import templates
        reason = templates.check(lead)
        if reason:
            await reply(f"⚠️ No se puede enviar plantilla: {reason}.")
        else:
            tpl, _ = templates.template_for(lead)
            _pending_followups[thread_ts] = (lid, time.time() + CONFIRM_SECONDS)
            await reply(f"¿Envío la plantilla `{tpl}` a {who(lead)}? Responde `✅ confirm` en los próximos 10 min.")
    elif name == "confirm":
        pending = _pending_followups.pop(thread_ts, None)
        if not pending or pending[1] < time.time() or pending[0] != lid:
            await reply("No hay un `followup` pendiente de confirmar en este hilo.")
        else:
            from . import templates
            ok, note = await templates.send_followup(lid, f"slack:{by}")
            await reply(("✅ " if ok else "⚠️ ") + note)
    return True


# --------------------------------------------------------------------------- #
# Reminders and digest (background watcher)
# --------------------------------------------------------------------------- #
async def check_reminders() -> None:
    s = get_settings()
    t = core.now()
    rows = db.query("""SELECT lead_id FROM leads WHERE contactable = 1 AND tier = 'hot' AND stage = 'sql'
                       AND hot_alerted_at IS NOT NULL""")
    for r in rows:
        lead = core.get(r["lead_id"])
        try:
            link = dashboard_link(lead)
            extra = f" · <{link}|abrir en el dashboard>" if link else ""
            if not lead.get("sla_reminded_at") and t - lead["hot_alerted_at"] >= s.lead_sla_hours * 3600:
                core.update(lead["lead_id"], sla_reminded_at=t)
                await _remind(lead, f"⏰ {_owner()} {who(lead)} sigue sin contacto después de "
                                    f"{s.lead_sla_hours:g} h (score {lead['score']}){extra}")
            is_open, left = core.window(lead)
            if is_open and left <= WINDOW_REMIND_BEFORE and not lead.get("window_reminded_at"):
                core.update(lead["lead_id"], window_reminded_at=t)
                await _remind(lead, f"⌛ {_owner()} la ventana de WhatsApp de {who(lead)} cierra en "
                                    f"{int(left // 60)} min; después solo con plantilla{extra}")
        except Exception:  # noqa: BLE001
            logger.exception("reminder failed for %s", r["lead_id"])


async def _remind(lead: dict, text: str) -> None:
    if lead.get("slack_alert_ts"):
        await post(text, thread_ts=lead["slack_alert_ts"])
    else:
        await post(text)


def digest_text(since: float) -> Optional[str]:
    new = db.query("SELECT tier, COUNT(*) n FROM leads WHERE created_at >= ? GROUP BY tier", (since,))
    by_tier = {r["tier"]: r["n"] for r in new}
    acts = db.query("SELECT type, data_json FROM lead_activity WHERE ts >= ? AND type IN "
                    "('consent_asked', 'consent_yes', 'opted_out', 'stage')", (since,))
    asked = sum(1 for a in acts if a["type"] == "consent_asked")
    yes = sum(1 for a in acts if a["type"] == "consent_yes")
    opt = sum(1 for a in acts if a["type"] == "opted_out")
    moves = [json.loads(a["data_json"] or "{}") for a in acts if a["type"] == "stage"]
    moves = [m for m in moves if m.get("source") != "dexter"]
    sla = get_settings().lead_sla_hours * 3600
    overdue = db.query("""SELECT COUNT(*) FROM leads WHERE contactable = 1 AND tier = 'hot' AND stage = 'sql'
                          AND hot_alerted_at IS NOT NULL AND hot_alerted_at < ?""", (core.now() - sla,))[0][0]
    pending = followups.digest_line()  # None when nothing is pending: the line is skipped
    if not (sum(by_tier.values()) or asked or yes or opt or moves or overdue or pending):
        return None
    if consent.enabled():
        consent_line = consent.digest_line(since)
    else:
        consent_line = f"Consentimiento: {yes} sí · {asked} preguntas" + (f" ({min(1, yes / asked):.0%})" if asked else "")
    lines = ["📈 *Leads, últimas 24 h*",
             f"Nuevos: {sum(by_tier.values())} (🔥 {by_tier.get('hot', 0)} · 🌤 {by_tier.get('warm', 0)} · "
             f"❄️ {by_tier.get('cold', 0)})",
             consent_line or "Consentimiento: sin nuevos",
             f"Hot sin contactar > {get_settings().lead_sla_hours:g} h: {overdue}",
             f"Bajas: {opt}"]
    if moves:
        lines.append("Movimientos: " + ", ".join(f"{m.get('from')}→{m.get('to')}" for m in moves[:10]))
    if pending:
        lines.append(pending)
    return "\n".join(lines)


async def maybe_digest() -> bool:
    s = get_settings()
    now_mx = dt.datetime.fromtimestamp(core.now(), core.MX)
    if now_mx.hour < s.metrics_digest_hour:
        return False
    day = now_mx.strftime("%Y-%m-%d")
    if not db.safe("digest flag", lambda: db.set_flag_once(f"leads_digest:{day}"), default=False):
        return False
    text = db.safe("digest", lambda: digest_text(core.now() - 86400))
    if text:
        await post(text)
        return True
    return False


def reset() -> None:  # tests
    _pending_followups.clear()
