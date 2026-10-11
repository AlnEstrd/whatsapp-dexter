"""FastAPI service: provider-agnostic WhatsApp webhooks -> agent -> reply.

    POST /webhooks/twilio        Twilio WhatsApp (Sandbox / sender)
    GET  /webhooks/meta          Meta webhook verification (alias: /webhook)
    POST /webhooks/meta          Meta Cloud API messages + account events
    POST /admin/reply            Human replies to a user (X-Admin-Token)
    POST /admin/release          Hand a user back to the bot
    POST /admin/close            Close a conversation (user gets a closing message)
    GET  /admin/metrics          KPIs overview, ?range=7d (X-Admin-Token; debugging aid)
    POST /admin/digest           Post the metrics digest to Slack now, ?range=yesterday
    POST /admin/mcp/revoke-all   Revoke every MCP connector token
    /mcp, /authorize, /token...  Read-only MCP connector for claude.ai (MCP_ENABLED)
    POST /slack/events           Slack Events API (thread replies, take/bot)
    POST /admin/send             Bot-initiated message via WA_PROVIDER
    GET  /admin/usage            Today's token usage + top users (X-Admin-Token)
    GET  /admin/leads[.csv]      Leads (X-Admin-Token); see app/leads/web.py for the rest
    POST /contact                Web form "Que Alan me contacte"
    GET  /dashboard              Owner dashboard (DASHBOARD_ENABLED)
    GET  /                       Health: enabled channels, provider, agent

Inbound messages are answered on the channel they arrived on, so Twilio and
Meta can run side by side during a migration.

Run with:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import time
import unicodedata
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from . import attachments, db, docs, guard, leads, limits, on_request, sessions, slack_bridge, status, store, voice
from .agent import get_agent, pop_action, pop_documents, pop_gaps, pop_unlocked, turn_stats
from .agent import tools_called as agent_tools_called
from .analytics import digest, events, metrics, mcp_server, pricing, topics
from .channels import (
    Channel,
    InboundMessage,
    SendError,
    enabled_channels,
    get_channel,
    get_default_channel,
    normalize_user_id,
)
from .config import get_settings
from .escalation import HANDOFF_REPLY, alert_provider_event, escalate, notify_slack, wants_human
from .flows import menu, quiz
from .flows.types import Outgoing
from .formatting import strip_greeting, to_whatsapp
from .handoff import hand_off
from .leads import flows as lead_flows
from .leads import web as leads_web
from .resilience import with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

def restore_state() -> bool:
    """Startup: open/migrate the DB, import the old JSON files once, load every
    conversation back into memory and purge expired ones. Sessions whose history and
    quiz came back are adopted by this process, so they get no restart apology.
    Returns False when recovery failed (the apology path then still applies)."""
    s = get_settings()
    if not db.enabled():
        logger.info("DB_PATH empty: state is memory-only (boot %s)", sessions.BOOT_ID)
        return False
    imported = imported_threads = 0
    try:  # a broken legacy JSON file must not stop the SQLite restore
        db.conn()
        pricing.sync_table()
        imported = sessions.import_legacy(s.sessions_file)
        imported_threads = store.import_legacy_threads(s.slack_state_file)
    except Exception:  # noqa: BLE001
        logger.exception("Import of the legacy JSON state failed; continuing with the DB")
    try:
        n_sessions = sessions.load_all()
        counts = store.load_all()
        n_quiz = quiz.load_all()
    except Exception as exc:  # noqa: BLE001
        logger.exception("State recovery from %s failed", s.db_path)
        db._alert_once(f"recovery: {exc.__class__.__name__}")
        return False
    run_retention()
    recovered = 0
    for uid, sess in list(sessions.all_sessions().items()):
        if sess.get("state") != "open" or sess.get("boot") == sessions.BOOT_ID:
            continue
        ctx_ok = not sess.get("context") or bool(store.has_history(uid) or quiz.last_result(uid))
        quiz_ok = not sess.get("quiz") or quiz.active(uid)
        if ctx_ok and quiz_ok:
            sessions.mark_recovered(uid)
            recovered += 1
    logger.info("State restored (boot %s): %d sessions (%d recovered), %d histories, %d quizzes, "
                "%d threads; imported %d sessions + %d threads from JSON", sessions.BOOT_ID, n_sessions,
                recovered, counts["histories"], n_quiz, counts["threads"], imported, imported_threads)
    return True


def run_retention() -> int:
    """Purge closed/idle conversations older than RETENTION_DAYS and old webhook ids."""
    s = get_settings()
    users = db.safe("retention", lambda: db.purge(s.retention_days, s.processed_retention_days), default=[])
    for uid in users or []:
        store.forget(uid)
        sessions.forget(uid)
    leads.purge()  # leads have their own retention (consented: LEAD_RETENTION_DAYS)
    return len(users or [])


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    restore_state()
    on_request.load()  # validate entries at startup (bad files are logged and skipped)
    if slack_bridge.enabled():
        await slack_bridge.load_bot_user_id()
        tasks.append(asyncio.create_task(slack_bridge.window_watcher()))
    tasks.append(asyncio.create_task(lifecycle_watcher()))
    tasks.append(asyncio.create_task(recover_after_restart()))
    if leads.enabled():
        tasks.append(asyncio.create_task(leads.watcher()))  # reminders, digest, Zoho outbox
    async with mcp_server.running():   # no-op unless MCP_ENABLED (and its secrets are strong)
        yield
    for t in tasks:
        t.cancel()
    events.drain()


app = FastAPI(title="WhatsApp Agentic Bot POC", lifespan=lifespan)
app.include_router(leads_web.router)

FALLBACK_REPLY = "Sorry, something went wrong on my end handling that. Please try again."


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/")
async def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "channels_enabled": enabled_channels(),
        "outbound_provider": s.wa_provider,
        "agent_mode": "echo" if (s.agent_mode == "echo" or not s.anthropic_api_key) else "claude",
        "slack_alerts": bool(s.slack_webhook_url or s.slack_threads_enabled),
        "slack_threads": s.slack_threads_enabled,
        "handoff_target": "flex" if s.flex_enabled else "slack",
    }


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
@app.post("/webhooks/twilio")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    await _ingest("twilio", request, background_tasks)
    # Empty TwiML: we reply asynchronously via the REST API instead.
    return Response(content="<Response></Response>", media_type="text/xml")


@app.get("/webhooks/meta")
@app.get("/webhook")  # legacy path, keeps the existing Meta dashboard config working
async def meta_verify(request: Request) -> Response:
    s = get_settings()
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and s.whatsapp_verify_token and \
            p.get("hub.verify_token") == s.whatsapp_verify_token:
        logger.info("Meta webhook verified")
        return Response(content=p.get("hub.challenge", ""), media_type="text/plain")
    logger.warning("Meta webhook verification failed")
    return Response(status_code=403)


@app.post("/webhooks/meta")
@app.post("/webhook")
async def meta_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    await _ingest("meta", request, background_tasks)
    return {"status": "received"}


async def _ingest(channel_name: str, request: Request, background_tasks: BackgroundTasks) -> None:
    channel = get_channel(channel_name)
    if channel is None:
        logger.warning("Webhook for disabled channel '%s' (missing credentials)", channel_name)
        raise HTTPException(status_code=503, detail=f"channel {channel_name} not configured")

    raw = await request.body()
    if not await channel.verify_request(request, raw):
        logger.warning("Rejected %s webhook: bad signature", channel_name)
        raise HTTPException(status_code=403, detail="invalid signature")

    parsed = await channel.parse_webhook(request, raw)
    for event in parsed.events:
        logger.info("Provider event %s/%s", event.channel, event.kind)
        if event.channel == "meta":
            status.record_meta_event(event.kind, event.data)  # verification state for the live status
        background_tasks.add_task(alert_provider_event, event)
    for st in parsed.statuses:  # metrics only (msg_status, read receipts)
        background_tasks.add_task(events.status, st)

    # Only real messages get here: Meta "statuses" (sent/delivered/read) never become
    # InboundMessages (failed ones are surfaced as provider events above).
    allowed = get_settings().allowed_senders_list
    for msg in parsed.messages:
        # Idempotency first: providers retry, and the same id must never be handled twice.
        if await asyncio.to_thread(store.seen_before, msg.message_id, channel_name):
            logger.info("Duplicate delivery %s ignored", msg.message_id)
            continue
        if allowed and msg.user_id not in allowed:
            logger.info("Ignoring non-allowlisted sender %s", msg.user_id)
            continue
        background_tasks.add_task(handle_inbound, channel, msg)


# --------------------------------------------------------------------------- #
# Core flow (provider-agnostic)
# --------------------------------------------------------------------------- #
async def send_reply(channel: Channel, msg: InboundMessage, text: str) -> bool:
    text = to_whatsapp(text)  # markdown -> WhatsApp (** -> *), before send, log and Slack mirror
    try:
        wamid = await with_retry(
            lambda: channel.send_text(msg.reply_to, text),
            attempts=3,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.send",
        )
        events.outbound(msg.user_id, wamid)
        store.log_turn(msg.user_id, "bot", text)
        sessions.touch_bot(msg.user_id)
        await slack_bridge.mirror_bot(msg.user_id, text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not deliver reply to %s", msg.user_id)
        events.record("error", msg.user_id, kind=exc.__class__.__name__, where="send")
        events.record("handoff", msg.user_id, step="handoff", reason="delivery_failure")
        reason = f"Reply delivery failed on {channel.name}: {exc}"
        if not (slack_bridge.enabled() and await slack_bridge.escalate_in_thread(msg, reason)):
            await escalate(msg, reason)
        return False


async def send_out(channel: Channel, msg: InboundMessage, out: Outgoing) -> bool:
    """Send text, as an interactive list (or reply buttons) when the channel supports it.
    Falls back to plain text (which already numbers the options)."""
    send_options = getattr(channel, "send_options", None)
    send_buttons = getattr(channel, "send_buttons", None)
    body = out.interactive_text or out.text
    if out.options and out.buttons and send_buttons:
        sender = lambda: send_buttons(msg.reply_to, body, [(o.id, o.title) for o in out.options])  # noqa: E731
    elif out.options and send_options:
        sender = lambda: send_options(msg.reply_to, body, out.button, out.options)  # noqa: E731
    else:
        sender = None
    if sender is not None:
        try:
            wamid = await with_retry(
                sender,
                attempts=2,
                is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
                label=f"{channel.name}.send_options",
            )
            events.outbound(msg.user_id, wamid)
            store.log_turn(msg.user_id, "bot", body)
            sessions.touch_bot(msg.user_id)
            await slack_bridge.mirror_bot(msg.user_id, body)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("Interactive send failed for %s; falling back to text", msg.user_id)
    return await send_reply(channel, msg, out.text)


NO_FILES = {"es": "Por ahora no puedo enviar archivos por este canal. Escribe *hablar con Alan* si lo necesitas.",
            "en": "I can't send files on this channel right now. Type *talk to Alan* if you need it."}
DOC_FAILED = {"es": "No pude enviar el archivo. Inténtalo de nuevo en un momento o escribe *hablar con Alan*.",
              "en": "I couldn't send the file. Please try again in a moment or type *talk to Alan*."}
NO_RESULT = {"es": "Aún no has hecho el diagnóstico. Escribe *diagnóstico* para empezar (3 min); al final te envío tu reporte en PDF.",
             "en": "You haven't taken the assessment yet. Type *assessment* to start (3 min); at the end I'll send you your PDF report."}
UNKNOWN_SLIDE = {"es": "No encontré esa lámina.", "en": "I couldn't find that slide."}


async def send_file(channel: Channel, msg: InboundMessage, doc: docs.Doc) -> bool:
    """Send a generated PDF or image. A failure never breaks the chat: the user gets a
    short text instead and the owner a note in the Slack thread."""
    lang = store.get_lang(msg.user_id) or "es"
    is_image = doc.mime.startswith("image/")
    sender = getattr(channel, "send_image" if is_image else "send_document", None)
    if sender is None:
        await send_reply(channel, msg, NO_FILES[lang])
        return False
    extra = {"mime": doc.mime} if is_image else {}
    try:
        wamid = await with_retry(
            lambda: sender(msg.reply_to, doc.data, doc.filename, doc.caption, **extra),
            attempts=2,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.send_document",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not deliver %s to %s", doc.filename, msg.user_id)
        events.record("error", msg.user_id, kind=exc.__class__.__name__, where="send_file")
        await send_reply(channel, msg, DOC_FAILED[lang])
        await _thread_note(msg, f"⚠️ Could not deliver {doc.filename}: {exc}")
        return False
    events.outbound(msg.user_id, wamid)
    _record_doc(msg.user_id, doc)
    note = f"📎 {doc.filename}"
    leads.engage(msg.user_id, "file_sent", msg.profile_name)
    store.log_turn(msg.user_id, "bot", note)
    await slack_bridge.mirror_bot(msg.user_id, note)
    return True


DOC_EVENTS = {"slides": ("slides_sent", "slides"), "topic_pdf": ("topic_pdf_sent", "topic_pdf"),
              "report": ("report_sent", "report")}


def _record_doc(user_id: str, doc: docs.Doc) -> None:
    if doc.kind in DOC_EVENTS:
        etype, step = DOC_EVENTS[doc.kind]
        extra = {"slide": doc.slide, "format": "image" if doc.mime.startswith("image/") else "pdf"} \
            if doc.kind == "slides" else {}
        events.record(etype, user_id, step=step, pages=doc.pages, **extra)


async def _send_deck(channel: Channel, msg: InboundMessage, lang: str, slide: Optional[str] = None,
                     fmt: str = "image") -> None:
    """Whole deck (PDF) or one slide: an inline image by default, a PDF if asked."""
    try:
        doc = docs.make_slide_image(slide, lang) if (slide and fmt == "image") else docs.make_deck(slide, lang)
    except ValueError:
        await send_reply(channel, msg, f"{UNKNOWN_SLIDE[lang]}\n\n{docs.slide_index_text(lang)}")
        return
    sent = await send_file(channel, msg, doc)
    if sent:
        leads.signal(msg.user_id, "pdf_requested", {"slide": slide or "all"}, topic=f"slides:{slide or 'all'}")
    if sent and slide is None:
        await send_reply(channel, msg, docs.slide_index_text(lang))


async def _send_assessment_pdf(channel: Channel, msg: InboundMessage, result: quiz.Result) -> bool:
    try:
        # "Preparado para: Name, Company" once the lead gave them; the WhatsApp name otherwise (as before)
        doc = docs.make_assessment(result, leads.report_name(msg.user_id) or msg.profile_name)
    except Exception:  # noqa: BLE001
        logger.exception("Assessment PDF failed to build for %s", msg.user_id)
        await _thread_note(msg, "⚠️ The assessment PDF failed to build.")
        return False
    return await send_file(channel, msg, doc)


async def _thread_note(msg: InboundMessage, text: str) -> None:
    """Owner-facing note (not sent to the customer)."""
    if slack_bridge.enabled():
        try:
            await slack_bridge._thread_post(msg.user_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("thread note failed")
    else:
        await notify_slack(f"{msg.profile_name or msg.user_id}: {text}")


LEAD_HANDOFF_REPLY = {
    "es": "¡Va! Ya le avisé a Alan: te va a escribir él mismo por este chat en cuanto pueda. "
          "Si quieres, adelántale qué te interesa (tu empresa, un bot así, la plática) para que llegue con contexto. 🤝",
    "en": "Done! I've let Alan know: he'll write to you himself in this chat as soon as he can. "
          "If you like, tell him upfront what you're interested in (your company, a bot like this, the talk) "
          "so he comes with context. 🤝",
}


def _handoff_reply(user_id: str) -> str:
    """Leads (they read the making-of) get a personal line from Alan's side; others the usual one."""
    if on_request.enabled() and on_request.is_lead(user_id):
        return LEAD_HANDOFF_REPLY["en" if _lang_of(user_id) == "en" else "es"]
    return HANDOFF_REPLY


async def _to_human(channel: Channel, msg: InboundMessage, reason: str, kind: str = "other") -> None:
    reply = _handoff_reply(msg.user_id)  # before the handoff: it doesn't change, but be explicit
    if kind == "user_asked":
        await _lead_handoff(channel, msg, reason)
    await hand_off(msg, reason, kind)
    await send_reply(channel, msg, reply)


# --------------------------------------------------------------------------- #
# Leads (app/leads/): consent, profiling, contact form. All no-ops with LEADS_ENABLED=false.
# --------------------------------------------------------------------------- #
async def _ask_consent(channel: Channel, msg: InboundMessage, moment: str) -> bool:
    if not leads.enabled():
        return False
    out = leads.safe("consent ask", lambda: lead_flows.ask_consent(msg.user_id, _lang_of(msg.user_id), moment))
    if out is None:
        return False
    await send_out(channel, msg, out)
    return True


async def _lead_handoff(channel: Channel, msg: InboundMessage, reason: str) -> None:
    """The user asked for Alan: record it (+intent) and ask for follow-up consent first."""
    if not leads.enabled():
        return
    leads.handoff(msg.user_id, reason, msg.profile_name)
    await _ask_consent(channel, msg, "handoff")


def _contact_form_on() -> bool:
    return leads.enabled() and get_settings().contact_form_enabled


async def _apply_flow(channel: Channel, msg: InboundMessage, res: lead_flows.FlowResult) -> None:
    uid = msg.user_id
    for out in res.outs:
        await send_out(channel, msg, out)
    if res.report_name and (result := quiz.last_result(uid)) is not None:
        await _send_assessment_pdf(channel, msg, result)
    if res.contact:
        lead = leads.safe("lead", lambda: leads.core.by_user(uid))
        if lead:
            await leads.contact_confirmed(lead["lead_id"], res.contact, lead_flows.CONTACT_FORM_VERSION,
                                          "whatsapp_form")
    if res.handoff:
        _route(msg, "handoff")
        await _to_human(channel, msg, "User asked to talk to Alan", "user_asked")
        return
    if res.ask_consent:
        await _ask_consent(channel, msg, res.ask_consent)
    sessions.touch_bot(uid)


async def _start_contact(channel: Channel, msg: InboundMessage, lang: str) -> None:
    _route(msg, "lead_form")
    leads.engage(msg.user_id, "contact_form", msg.profile_name)
    res = leads.safe("contact form", lambda: lead_flows.start_contact(msg.user_id, lang))
    if res is not None:
        await _apply_flow(channel, msg, res)


async def _opt_out(channel: Channel, msg: InboundMessage) -> None:
    """BAJA / STOP: zero tokens, any mode (bot, quiz, human). Recorded, confirmed once."""
    uid = msg.user_id
    _route(msg, "opt_out")
    human = store.in_handoff(uid)
    quiz.discard(uid)
    leads.safe("clear flow", lambda: lead_flows.clear(uid))
    lead = leads.opt_out(uid)
    lang = menu.detect_lang(msg.text, store.get_lang(uid)) if not human else _lang_of(uid)
    store.log_turn(uid, "user", msg.text)
    await send_reply(channel, msg, lead_flows.TEXT["opted_out"][lang])
    await leads.opt_out_effects(lead, human)


async def _start_quiz(channel: Channel, msg: InboundMessage, lang: str,
                      known: Optional[dict] = None) -> None:
    leads.engage(msg.user_id, "quiz_start", msg.profile_name)
    before = quiz.last_result(msg.user_id)
    await _send_quiz(channel, msg, quiz.start(msg.user_id, lang, known=known), before)


async def _quiz_step(channel: Channel, msg: InboundMessage) -> None:
    before = quiz.last_result(msg.user_id)
    await _send_quiz(channel, msg, quiz.answer(msg.user_id, msg.text, msg.reply_id), before)


async def _send_quiz(channel: Channel, msg: InboundMessage, outs: list, before) -> None:
    """Send the quiz's messages; when they finish it (new result), the PDF goes right after the result."""
    result = quiz.last_result(msg.user_id)
    finished = result is not None and result is not before
    for i, out in enumerate(outs):
        await send_out(channel, msg, out)
        if finished and i == len(outs) - 2 and getattr(channel, "send_document", None):
            # result text first, then the PDF report, then the follow-up line (skipped silently where files can't be sent)
            await _send_assessment_pdf(channel, msg, result)
    if finished:
        status.bump("assessments")
        await _thread_note(msg, quiz.summary_for_slack(result))
    sessions.touch_bot(msg.user_id, quiz=quiz.active(msg.user_id), context=finished or None)
    if finished and leads.enabled():
        # Never gates the result: optional role / size / name questions, then the consent ask.
        leads.quiz_done(msg.user_id, result, msg.profile_name)
        res = leads.safe("profiling", lambda: lead_flows.start_profile(msg.user_id, result.lang))
        if res is not None:
            await _apply_flow(channel, msg, res)


TYPING_REFRESH_SECONDS = 20  # WhatsApp drops the indicator after ~25 s


@contextlib.asynccontextmanager
async def typing(channel: Channel, msg: InboundMessage):
    """Keep "escribiendo..." visible while the body runs, where supported.

    Best-effort: a failed indicator is logged and never blocks the reply.
    Sending the reply clears it on WhatsApp's side; we just stop refreshing.
    """
    send_typing = getattr(channel, "send_typing", None)
    if send_typing is None or not msg.message_id:
        yield
        return

    async def _send() -> bool:
        try:
            await send_typing(msg)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("typing indicator failed for %s", msg.message_id)
            return False

    async def _keep_alive() -> None:
        while True:
            await asyncio.sleep(TYPING_REFRESH_SECONDS)
            if not await _send():
                return

    if not await _send():  # first one right away, before the agent starts
        yield
        return
    task = asyncio.create_task(_keep_alive())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


MEDIA_NOTE = {
    "unsupported_voice": {  # voice notes on (TRANSCRIBE_API_KEY set)
        "es": "Puedo leer fotos (JPG, PNG, WebP), PDFs y escuchar notas de voz. Videos y otros archivos no. "
              "¿Me escribes tu pregunta o me mandas una foto, PDF o audio?",
        "en": "I can read photos (JPG, PNG, WebP) and PDFs and listen to voice notes, not video or other files. "
              "Could you type your question or send a photo, PDF or voice note?",
    },
    "unsupported": {
        "es": "Por ahora puedo leer fotos (JPG, PNG, WebP) y PDFs. Audios, videos y otros archivos no. "
              "¿Me escribes tu pregunta o me mandas una foto/PDF?",
        "en": "For now I can read photos (JPG, PNG, WebP) and PDFs, not audio, video or other files. "
              "Could you type your question or send a photo/PDF?",
    },
    "too_large": {
        "es": "Ese archivo es muy grande (máx. 4 MB en fotos y 8 MB en PDF). ¿Puedes mandarlo más ligero?",
        "en": "That file is too big (max 4 MB for photos, 8 MB for PDFs). Could you send a lighter one?",
    },
    "too_many_pages": {
        "es": f"Ese PDF tiene demasiadas páginas (máx. {attachments.MAX_PDF_PAGES}). Mándame las páginas que te interesan.",
        "en": f"That PDF has too many pages (max {attachments.MAX_PDF_PAGES}). Send me just the pages you care about.",
    },
    "unreadable": {
        "es": "No pude abrir ese archivo (¿está dañado o protegido con contraseña?). ¿Lo intentas de nuevo?",
        "en": "I couldn't open that file (is it damaged or password-protected?). Could you try again?",
    },
    "failed": {
        "es": "No pude descargar tu archivo. ¿Lo mandas otra vez?",
        "en": "I couldn't download your file. Could you send it again?",
    },
    "quiz": {
        "es": "Estamos en el diagnóstico: responde con los botones o escribe *salir*. Después reviso tu archivo.",
        "en": "We're in the assessment: answer with the buttons or type *exit*. Then I'll look at your file.",
    },
    "channel": {
        "es": "Por este canal todavía no puedo leer archivos; escríbeme tu pregunta.",
        "en": "I can't read files on this channel yet; please type your question.",
    },
}

VOICE_NOTE = {
    "too_large": {
        "es": "Ese audio es muy largo para mí (máx. unos 15 minutos). ¿Me lo mandas en partes más cortas?",
        "en": "That audio is too long for me (about 15 minutes max). Could you send it in shorter parts?",
    },
    "unsupported": {
        "es": "No pude abrir ese formato de audio. ¿Me mandas una nota de voz de WhatsApp o me lo escribes?",
        "en": "I couldn't open that audio format. Could you send a WhatsApp voice note or type it?",
    },
    "empty": {
        "es": "No alcancé a entender el audio. ¿Me lo mandas otra vez o me lo escribes?",
        "en": "I couldn't make out the audio. Could you send it again or type it?",
    },
    "failed": {
        "es": "No pude escuchar tu nota de voz. ¿Me la mandas otra vez o me lo escribes?",
        "en": "I couldn't process your voice note. Could you send it again or type it?",
    },
}
VOICE_PREFIX = "[Voice note] "


def _media_note(reason: str, lang: str) -> str:
    if reason == "unsupported" and voice.enabled():
        reason = "unsupported_voice"
    return MEDIA_NOTE[reason][lang]


async def _handle_media(channel: Channel, msg: InboundMessage) -> None:
    """Photo / screenshot / PDF from the attendee: validate, then let Claude look at it."""
    uid = msg.user_id
    lang = menu.detect_lang(msg.text, store.get_lang(uid)) if msg.text else (store.get_lang(uid) or "es")
    if quiz.active(uid):
        _route(msg, "file_note")
        await send_reply(channel, msg, MEDIA_NOTE["quiz"][lang])
        return
    if not hasattr(channel, "download_media") or not msg.media:
        _route(msg, "file_note")
        await send_reply(channel, msg, MEDIA_NOTE["channel"][lang])
        return
    blocked = limits.model_block(uid)
    if blocked:
        await _limit_notice(channel, msg, blocked)
        return
    try:
        async with typing(channel, msg):  # downloading/validating can take a few seconds
            att = await attachments.load(channel, msg.media)
    except attachments.Rejected as exc:
        events.record("file_in", uid, kind=msg.msg_type, accepted=False, reject_reason=exc.reason)
        _route(msg, "file_note")
        await send_reply(channel, msg, _media_note(exc.reason, lang))
        return
    except Exception:  # noqa: BLE001
        logger.exception("Media download failed for %s", uid)
        events.record("file_in", uid, kind=msg.msg_type, accepted=False, reject_reason="fetch_failed")
        _route(msg, "file_note")
        await send_reply(channel, msg, MEDIA_NOTE["failed"][lang])
        return
    events.record("file_in", uid, kind=att.kind, accepted=True, reject_reason=None)
    await _run_agent(channel, msg, msg.text, attachments=[att])


async def _handle_voice(channel: Channel, msg: InboundMessage, is_new: bool) -> None:
    """Voice note: transcribe it, then handle the text as if it had been typed
    (goodbye / human keywords, quiz answer, menu, guard, agent)."""
    uid = msg.user_id
    lang = store.get_lang(uid) or "es"
    if not hasattr(channel, "download_media") or not msg.media:
        _route(msg, "file_note")
        await send_reply(channel, msg, MEDIA_NOTE["channel"][lang])
        return
    blocked = limits.model_block(uid)
    if blocked:
        await _limit_notice(channel, msg, blocked)
        return
    try:
        async with typing(channel, msg):
            transcript = await voice.load_transcript(channel, msg.media)
    except attachments.Rejected as exc:
        events.record("file_in", uid, kind="audio", accepted=False, reject_reason=exc.reason)
        _route(msg, "file_note")
        await send_reply(channel, msg, VOICE_NOTE[exc.reason][lang])
        return
    except Exception:  # noqa: BLE001  (download or transcription service failed)
        events.record("file_in", uid, kind="audio", accepted=False, reject_reason="failed")
        logger.exception("Voice note failed for %s", uid)
        _route(msg, "file_note")
        await send_reply(channel, msg, VOICE_NOTE["failed"][lang])
        return
    if not transcript:
        _route(msg, "file_note")
        await send_reply(channel, msg, VOICE_NOTE["empty"][lang])
        return
    events.record("file_in", uid, kind="audio", accepted=True, reject_reason=None)
    store.log_turn(uid, "user", f"🎙️ {transcript}")
    await _thread_note(msg, f"🎙️ Transcript: {transcript}")
    voiced = dataclasses.replace(msg, text=transcript, media=None, transcribed=True)
    if await _closer_or_human(channel, voiced):
        return
    if get_settings().flows_enabled:
        await _route_flows(channel, voiced, is_new)
    else:
        await _run_agent(channel, voiced, voiced.text)


MAX_BUBBLES = 3
BUBBLE_PAUSE_PER_CHAR = 0.02  # seconds of "typing" per character of the next bubble
BUBBLE_PAUSE_MIN, BUBBLE_PAUSE_MAX = 0.8, 2.5


def split_bubbles(text: str, max_bubbles: int = MAX_BUBBLES) -> list[str]:
    """Split an agent reply into WhatsApp bubbles at blank lines (paragraphs).

    Lists stay together (their lines are single-newline separated). Anything
    beyond ``max_bubbles`` is folded into the last bubble.
    """
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(parts) <= 1:
        return [text.strip()] if text and text.strip() else []
    if len(parts) <= max_bubbles:
        return parts
    return parts[: max_bubbles - 1] + ["\n\n".join(parts[max_bubbles - 1:])]


async def send_bubbles(channel: Channel, msg: InboundMessage, text: str) -> bool:
    """Send a reply as 1-3 bubbles, with "typing..." and a short pause between them."""
    bubbles = split_bubbles(text) or [text]
    send_typing = getattr(channel, "send_typing", None)
    for i, bubble in enumerate(bubbles):
        if i:
            if send_typing and msg.message_id:
                with contextlib.suppress(Exception):
                    await send_typing(msg)
            pause = min(BUBBLE_PAUSE_MAX, max(BUBBLE_PAUSE_MIN, len(bubble) * BUBBLE_PAUSE_PER_CHAR))
            await asyncio.sleep(pause)
        if not await send_reply(channel, msg, bubble):
            return False  # delivery failed and was escalated; don't send the rest
    return True


async def _run_agent(channel: Channel, msg: InboundMessage, text: str,
                     attachments: Optional[list] = None, topic: Optional[str] = None) -> bool:
    """Agent turn + any action a tool requested. False if the agent failed (handed off).
    ``topic``: label for this turn when the caller knows it (menu option, guard)."""
    blocked = limits.model_block(msg.user_id)
    if blocked:
        await _limit_notice(channel, msg, blocked)
        return True
    _route(msg, "agent")
    uid = msg.user_id
    if topic:
        _turn_topic[uid] = topic
    topic = _turn_topic.get(uid) or topics.from_route("agent", (sessions.get(uid) or {}).get("last_topic"))
    _turn_topic[uid] = topic
    lang = store.get_lang(uid) or "es"
    # Attendee text only (not the synthetic "¿Quién es Alan?" of a menu option), scrubbed.
    sample_id = events.sample(uid, msg.text, topic, lang) if (text == msg.text or attachments) else None
    leads.engage(msg.user_id, "agent_question", msg.profile_name)
    if msg.transcribed and text == msg.text:
        text = VOICE_PREFIX + text
    t0 = time.monotonic()
    try:
        # The Anthropic SDK already retries transient API errors (429/5xx), so
        # no outer retry here: re-running would duplicate history entries.
        async with typing(channel, msg):
            if attachments:
                reply = await get_agent().handle_message(msg.user_id, text, attachments=attachments)
            else:
                reply = await get_agent().handle_message(msg.user_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent failed for %s", msg.user_id)
        store.reset(msg.user_id)  # drop possibly half-written tool-use history
        pop_action(msg.user_id)
        pop_documents(msg.user_id)
        pop_gaps(msg.user_id)
        events.record("error", uid, kind=exc.__class__.__name__, where="agent")
        _agent_turn(uid, "error", t0, topic, sample_id)
        events.record("gap", uid, topic=topic, source="handoff", sample_id=sample_id)
        await _to_human(channel, msg, f"Agent error: {exc}", "agent_error")
        return False

    sessions.touch_bot(msg.user_id, context=True)
    action = pop_action(msg.user_id)
    documents = pop_documents(msg.user_id)
    outcome = {"close": "closed", "human": "handoff"}.get((action or {}).get("action"), "answered")
    if outcome == "answered" and turn_stats.get(uid, {}).get("exhausted"):
        outcome = "error"
    _agent_turn(uid, outcome, t0, topic, sample_id)
    for gap in pop_gaps(uid):
        note_id = events.sample(uid, gap["summary"], gap["topic"], lang, kind="gap_note")
        events.record("gap", uid, topic=gap["topic"], source="tool", sample_id=sample_id, note_id=note_id)
    for entry_id in pop_unlocked(msg.user_id):
        await _note_disclosure(msg, entry_id)
    if action and action["action"] == "close":
        # end_conversation: the agent's own text is dropped, the farewell is the only message
        for doc in documents:
            await send_file(channel, msg, doc)
        await close_chat(msg.user_id, "user_intent", farewell=action.get("farewell"), user_text=text)
        return True
    reply = strip_greeting(reply)  # the welcome / new-chat intro already said hello
    if reply or not (action or documents):
        await send_bubbles(channel, msg, reply or FALLBACK_REPLY)
        # Lead: Alan is always one message away. Every few answers without the invitation,
        # the bot adds it (not when the turn already hands off, quizzes or closes).
        if reply and not action and on_request.enabled() and on_request.lead_tick(msg.user_id, reply):
            lang = store.get_lang(msg.user_id) or "es"
            if (cta := on_request.lead_cta(msg.user_id, lang)):
                await send_reply(channel, msg, cta)
                store.add_to_last_reply(msg.user_id, cta)  # so the model knows it was just offered
    for doc in documents:
        if await send_file(channel, msg, doc):
            leads.signal(msg.user_id, "pdf_requested", {"file": doc.filename}, topic=f"pdf:{doc.filename[:40]}")
    signal = leads.pop_signal(msg.user_id)
    contact = leads.pop_contact(msg.user_id)
    if action and action["action"] == "report":
        result = quiz.last_result(msg.user_id)
        if result is not None:
            leads.signal(msg.user_id, "report_requested")
            await _send_assessment_pdf(channel, msg, result)
    elif action and action["action"] == "quiz":
        await _start_quiz(channel, msg, action.get("lang") or store.get_lang(msg.user_id) or "es",
                          known=action.get("known"))
    elif action and action["action"] == "human":
        reason = f"Talk to Alan: {action.get('reason', '')}".strip()
        await _lead_handoff(channel, msg, reason)
        await hand_off(msg, reason, "user_asked")
    elif contact and _contact_form_on() and not quiz.active(msg.user_id):
        await _start_contact(channel, msg, _lang_of(msg.user_id))
    elif signal:
        await _ask_consent(channel, msg, "buying_signal")
    return True


def _agent_turn(user_id: str, outcome: str, t0: float, topic: str, sample_id: Optional[str]) -> None:
    """agent_turn event: tools, model steps, tokens, latency and outcome of one user turn."""
    stats = turn_stats.pop(user_id, {})
    events.record("agent_turn", user_id, step="agent_turn", outcome=outcome, topic=topic,
                  tools=list(agent_tools_called.get(user_id, [])), steps=stats.get("steps", 0),
                  latency_ms=int((time.monotonic() - t0) * 1000),
                  input_tokens=stats.get("input_tokens", 0), output_tokens=stats.get("output_tokens", 0))
    fields = {"last_sample_id": sample_id}
    if topic in topics.SUBSTANTIVE:
        fields["last_topic"] = topic
    sessions.note(user_id, **fields)


async def _route_flows(channel: Channel, msg: InboundMessage, is_new: bool) -> bool:
    """Talk-companion routing. Returns True when the message was handled."""
    uid = msg.user_id
    lang = menu.detect_lang(msg.text, store.get_lang(uid))
    store.set_lang(uid, lang)
    sessions.set_lang(uid, lang)

    if quiz.active(uid):
        _route(msg, "quiz")
        await _quiz_step(channel, msg)
        return True

    if leads.enabled():  # a lead form in progress (profiling, contact request, menu 4 choice)
        res = None
        if lead_flows.current(uid):
            try:
                res = await lead_flows.step(uid, msg.text, msg.reply_id)
            except Exception:  # noqa: BLE001
                logger.exception("lead flow step failed")
                lead_flows.clear(uid)
        elif (msg.reply_id or "").startswith("contact:choice:"):  # tapped an older menu-4 list
            lead_flows.contact_choice(uid, lang)
            res = await lead_flows.step(uid, msg.text, msg.reply_id)
        if res is not None:
            _route(msg, "lead_form")
            await _apply_flow(channel, msg, res)
            return True
        if _contact_form_on() and not msg.reply_id and lead_flows.is_contact_request(msg.text):
            await _start_contact(channel, msg, lang)
            return True

    # On-request info: only for people who explicitly ask (no model call, works over budget).
    if on_request.enabled() and not msg.reply_id:
        if (entry := on_request.pdf_request(uid, msg.text)) is not None:
            _route(msg, "file_reply")
            await _send_dossier(channel, msg, entry, lang)
            return True
        if (entry := on_request.match(msg.text)) is not None:
            await _disclose(channel, msg, entry, lang)
            return True

    choice = msg.reply_id if (msg.reply_id or "").startswith("menu:") else menu.typed_choice(msg.text)
    slide = menu.slide_request(msg.text)
    if choice:
        _turn_topic[uid] = topics.MENU.get(choice, topics.OTHER)
        events.record("menu_choice", uid, option=choice)
    if slide:
        _turn_topic[uid] = "laminas"
        _route(msg, "file_reply")
        await _send_deck(channel, msg, lang, *slide)
    elif choice == "menu:slides":
        _route(msg, "file_reply")
        await _send_deck(channel, msg, lang)
    elif choice == "menu:report":
        _route(msg, "file_reply")
        result = quiz.last_result(uid)
        if result is not None:
            leads.signal(uid, "report_requested")
            await _send_assessment_pdf(channel, msg, result)
        else:
            await send_reply(channel, msg, NO_RESULT[lang])
    elif choice == "menu:quiz":
        _route(msg, "quiz")
        await _start_quiz(channel, msg, lang)
    elif choice == "menu:talk":
        _route(msg, "menu")
        events.record("menu_shown", uid, step="talk_overview", menu="talk")
        await send_out(channel, msg, menu.talk_overview(lang))
    elif choice == "menu:about":
        await _run_agent(channel, msg, "¿Quién es Alan Estrada?" if lang == "es" else "Who is Alan Estrada?",
                         topic="sobre_alan")
    elif choice == "menu:human" and _contact_form_on() and (msg.reply_id == "menu:human" or msg.text.strip() == "4"):
        _route(msg, "menu")  # option 4: talk now or be contacted later
        await send_out(channel, msg, lead_flows.contact_choice(uid, lang))
    elif choice == "menu:human":
        _route(msg, "handoff")
        await _to_human(channel, msg, "User asked to talk to Alan", "user_asked")
    elif menu.is_greeting(msg.text) or entry_source(msg.text) != "organic":  # QR prefill = "hola"
        _route(msg, "menu")
        _turn_topic[uid] = "saludo_menu"
        events.record("menu_shown", uid, step="menu", menu="welcome")
        await send_out(channel, msg, menu.welcome(lang, msg.profile_name))
    elif (blocked := limits.model_block(uid)):
        await _limit_notice(channel, msg, blocked)   # no guard / agent call; menu still works
    else:
        # A new chat that opens with a question: intro, answer, then a one-line offer of the
        # menu (not the menu itself; it goes out on a greeting, "menu" or "opciones").
        if is_new:
            await send_reply(channel, msg, menu.intro(lang, msg.profile_name))
        verdict = await guard.classify(uid, msg.text)
        if verdict.topic:
            _turn_topic[uid] = verdict.topic
        if not verdict.on_topic:
            await _off_topic(channel, msg, lang)
        else:
            ok = await _run_agent(channel, msg, msg.text)
            if ok and is_new and last_route.get(uid) == "agent" and sessions.is_open(uid) \
                    and not store.in_handoff(uid) and not quiz.active(uid) \
                    and not (leads.enabled() and lead_flows.current(uid)):  # a question/form is pending
                await send_reply(channel, msg, menu.menu_offer(lang))
    return True


async def _note_disclosure(msg: InboundMessage, entry_id: str) -> None:
    """Silent owner note + public counter, the first time an entry is given in a conversation."""
    status.bump("curious")
    events.record("on_request_disclosed", msg.user_id, step="on_request", id=entry_id)
    if leads.enabled():
        leads.engage(msg.user_id, "making_of", msg.profile_name)
        leads.signal(msg.user_id, "making_of", {"entry": entry_id}, topic=f"on_request:{entry_id}")
    entry = on_request.get(entry_id)
    if entry is not None and entry.notify_slack:
        lead = " · 🔥 possible lead: they were told they can talk to you" if entry.lead else ""
        try:
            await _thread_note(msg, f"🔑 On-request info shared: {entry_id}{lead}")
        except Exception:  # noqa: BLE001
            logger.exception("on-request Slack note failed")


async def _disclose(channel: Channel, msg: InboundMessage, entry: on_request.Entry, lang: str) -> None:
    """Fast path: send the entry's reply as written and seed the agent so follow-ups work."""
    _route(msg, "on_request")
    _turn_topic[msg.user_id] = "making_of"
    reply = on_request.response_for(entry, lang)
    cta = on_request.cta_for(entry, lang)
    first = on_request.unlock(msg.user_id, entry.id)
    # The agent sees the exchange (and, via its system prompt, the entry's detail) next turn.
    store.append_message(msg.user_id, "user", msg.text)
    store.append_message(msg.user_id, "assistant", [{"type": "text", "text": f"{reply}\n\n{cta}".strip()}])
    sessions.touch_bot(msg.user_id, context=True)
    await send_bubbles(channel, msg, reply)
    if cta:  # its own bubble: the human route right after the content
        await send_reply(channel, msg, cta)
    if first:
        await _note_disclosure(msg, entry.id)


async def _send_dossier(channel: Channel, msg: InboundMessage, entry: on_request.Entry, lang: str) -> None:
    try:
        doc = docs.make_dossier(entry, lang)
    except Exception:  # noqa: BLE001
        logger.exception("Dossier PDF failed for %s", entry.id)
        await send_reply(channel, msg, DOC_FAILED[lang])
        await _thread_note(msg, f"⚠️ The {entry.id} PDF failed to build.")
        return
    if await send_file(channel, msg, doc) and entry.lead and (cta := on_request.cta_for(entry, lang)):
        await send_reply(channel, msg, cta)


# --------------------------------------------------------------------------- #
# Conversation lifecycle: close, idle, restart recovery, off-topic
# --------------------------------------------------------------------------- #
LIFECYCLE_INTERVAL = 30  # seconds between idle checks
RESTART_NOTICE_MAX_AGE = 23 * 3600  # stay inside WhatsApp's 24h free-form window
RESTART_DELAY = 3  # seconds after startup before sending restart notices


def _lang_of(user_id: str) -> str:
    s = sessions.get(user_id) or {}
    return store.get_lang(user_id) or s.get("lang") or "es"


async def _notify(user_id: str, text: str, buttons: Optional[list] = None) -> bool:
    """System message to the user on their last channel (doesn't count as activity).
    ``buttons``: reply buttons in the same message where the channel supports them (CSAT)."""
    s = sessions.get(user_id) or {}
    channel = get_channel(s.get("channel_name", ""))
    if channel is None or not s.get("reply_to"):
        logger.warning("Cannot notify %s: no channel/address on record", user_id)
        return False
    send_buttons = getattr(channel, "send_buttons", None) if buttons else None
    if send_buttons:
        try:
            await send_buttons(s["reply_to"], text, buttons)
            store.log_turn(user_id, "bot", text)
            await slack_bridge.mirror_bot(user_id, text)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("Button message failed for %s; sending plain text", user_id)
    try:
        await with_retry(
            lambda: channel.send_text(s["reply_to"], text),
            attempts=3,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.notify",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Notify failed for %s", user_id)
        return False
    store.log_turn(user_id, "bot", text)
    await slack_bridge.mirror_bot(user_id, text)
    return True


# reason -> who closed it (closing message key, Slack label). The last four are for the
# metrics' close-reason vocabulary; today nothing but idle/user/admin closes a chat.
CLOSE_REASONS = {"user_keyword": "user", "user_intent": "user", "idle": "bot", "admin": "admin",
                 "offtopic_handoff": "bot", "limit": "bot", "error": "bot", "window_expired": "bot"}
CSAT_BUTTONS = [("csat:up", "👍"), ("csat:down", "👎")]
CSAT_THANKS = {"es": "¡Gracias!", "en": "Thanks!"}
CSAT_WINDOW = 24 * 3600


def _record_close(user_id: str, reason: str, sess: dict, **extra) -> None:
    """conversation_close event: reason, last step, turns, duration and whether the last bot
    message was read (Meta read receipts; None when unknown, e.g. Twilio)."""
    t = sess.get("closed_at") or sessions.now()
    read = sess.get("last_bot_read") if sess.get("last_bot_wamid") else None
    events.record("conversation_close", user_id, conversation_id=sess.get("conversation_id"),
                  reason=reason, last_step=sess.get("last_step"), turns=sess.get("turns", 0),
                  duration_s=int(t - (sess.get("opened_at") or t)), last_bot_msg_read=read, **extra)
    if reason == "idle" and sess.get("last_step") == "agent_turn" and read:
        # Read the answer and left without a word: count it as a (soft) gap.
        events.record("gap", user_id, conversation_id=sess.get("conversation_id"),
                      topic=sess.get("last_topic") or topics.OTHER, source="idle_after_answer",
                      sample_id=sess.get("last_sample_id"))


async def close_chat(user_id: str, reason: str, slack_cleanup: bool = True, *,
                     farewell: Optional[str] = None, user_text: Optional[str] = None) -> bool:
    """The one close routine (reason = "user_keyword" | "user_intent" | "idle" | "admin" | ...,
    see CLOSE_REASONS): closing message to the user, then wipe agent memory, quiz, transcript,
    handoff and idle timers. The next message starts fresh. ``farewell``: the agent's goodbye
    (user_intent). Logs the conversation_close event."""
    by = CLOSE_REASONS[reason]
    was_open = sessions.is_open(user_id)
    if not was_open and by != "admin":
        return False
    lang = _lang_of(user_id)
    if reason == "user_intent":
        closing = sessions.farewell_text(to_whatsapp(farewell or ""), lang)
    elif by == "bot" and reason != "idle":
        closing = sessions.text("close_system", lang)
    else:
        closing = sessions.text(f"close_{by}", lang)
    sess = sessions.get(user_id) or {}
    csat = bool(get_settings().csat_enabled and by == "user" and was_open
                and getattr(get_channel(sess.get("channel_name", "")), "send_buttons", None))
    await _notify(user_id, closing, buttons=CSAT_BUTTONS if csat else None)
    sessions.mark_closed(user_id, by, reason)
    if was_open:
        _record_close(user_id, reason, sess, csat_offered=csat)
    if csat:
        sessions.note(user_id, csat_pending={"conversation_id": sess.get("conversation_id"),
                                             "at": sessions.now(), "lang": lang})
    pop_action(user_id)
    pop_documents(user_id)
    store.wipe_conversation(user_id)  # agent history + quiz progress, one DB transaction
    store.end_handoff(user_id)
    quote = ""
    if by == "user" and user_text and get_settings().close_on_intent:
        quote = user_text.strip().replace("\n", " ")
        quote = quote if len(quote) <= 80 else quote[:79] + "…"
    try:
        if slack_cleanup and slack_bridge.enabled() and store.get_thread(user_id):
            label = {"user": "the customer", "bot": "the bot (inactivity)", "admin": "admin"}[by]
            note = f'🔒 Closed by user: "{quote}"' if quote else f"🔒 Conversation closed by {label}."
            await slack_bridge.close_thread(user_id, note)
        elif not slack_bridge.enabled():
            await notify_slack(f":lock: Conversation with {user_id} closed by {by}"
                               + (f': "{quote}"' if quote else "."))
    except Exception:  # noqa: BLE001
        logger.exception("Slack close note failed for %s", user_id)  # never blocks the close
    store.clear_transcript(user_id)
    return True


async def _close_from_slack(user_id: str) -> None:
    """Hook for the Slack `close` command / reaction: the bridge does its own thread cleanup."""
    await close_chat(user_id, "admin", slack_cleanup=False)


slack_bridge.close_hook = _close_from_slack
slack_bridge.lead_command_hook = leads.alerts.handle_command


async def _send_restart_notice(user_id: str) -> None:
    s = sessions.get(user_id) or {}
    lang = _lang_of(user_id)
    extra = sessions.text("restart_quiz", lang) if s.get("quiz") else ""
    if await _notify(user_id, sessions.text("restart", lang, quiz=extra)):
        sessions.restart_notified(user_id)
        if slack_bridge.enabled() and store.get_thread(user_id):
            await slack_bridge._thread_post(user_id, "♻️ Server restarted: chat history was lost; "
                                                     "the customer was notified.")


async def recover_after_restart() -> None:
    """On startup: tell recent users the restart wiped their chat; silently close stale sessions."""
    await asyncio.sleep(RESTART_DELAY)
    s = get_settings()
    try:
        stale = (s.idle_warn_minutes + s.idle_close_minutes) * 60
        for uid, sess in list(sessions.all_sessions().items()):
            if sess.get("state") == "open" and sess.get("boot") != sessions.BOOT_ID \
                    and stale > 0 and sessions.now() - sess.get("last_activity", 0) > stale:
                sessions.mark_closed(uid, "bot", "idle")  # would have been closed while we were down
                _record_close(uid, "idle", sess, while_down=True)
        if not s.restart_notice:
            return
        for uid in sessions.lost_by_restart(RESTART_NOTICE_MAX_AGE):
            if store.in_handoff(uid):
                continue  # a human is on it; the Slack thread still has the history
            await _send_restart_notice(uid)
    except Exception:  # noqa: BLE001
        logger.exception("Restart recovery failed")


async def check_idle() -> None:
    s = get_settings()
    skip = {uid for uid in sessions.all_sessions() if store.in_handoff(uid)}
    for uid, action in sessions.idle_actions(s.idle_warn_minutes * 60, s.idle_close_minutes * 60, skip):
        if not sessions.is_open(uid):
            continue  # closed (e.g. the user said goodbye) while this loop was awaiting
        if action == "warn":
            m = max(1, round(s.idle_close_minutes))
            if await _notify(uid, sessions.text("idle_warn", _lang_of(uid), m=m)):
                sessions.mark_warned(uid)
        else:
            await close_chat(uid, "idle")


RETENTION_INTERVAL = 24 * 3600  # purge once a day (and on startup)


async def lifecycle_watcher() -> None:  # started from the app lifespan
    last_purge = time.monotonic()
    while True:
        await asyncio.sleep(LIFECYCLE_INTERVAL)
        try:
            await check_idle()
        except Exception:  # noqa: BLE001
            logger.exception("Idle check failed")
        try:
            await digest.maybe_send()   # once a day at METRICS_DIGEST_HOUR; never raises
        except Exception:  # noqa: BLE001
            logger.exception("Metrics digest check failed")
        if time.monotonic() - last_purge >= RETENTION_INTERVAL:
            last_purge = time.monotonic()
            await asyncio.to_thread(run_retention)


async def _off_topic(channel: Channel, msg: InboundMessage, lang: str) -> None:
    """Off-topic message: static warning (no main-agent tokens); escalate after the max."""
    n = sessions.bump_offtopic(msg.user_id)
    mx = get_settings().offtopic_max_warnings
    _turn_topic[msg.user_id] = "fuera_de_tema"
    if n <= mx:
        _route(msg, "offtopic_warning")
        events.record("offtopic_warning", msg.user_id, step="offtopic_warning", n=n)
        await send_reply(channel, msg, sessions.text("offtopic_warn", lang, n=n, max=mx))
        await _thread_note(msg, f"🚧 Off-topic warning {n}/{mx}.")
    else:
        _route(msg, "handoff")
        await hand_off(msg, f"Off-topic after {mx} warnings", "offtopic")
        await send_reply(channel, msg, sessions.text("offtopic_escalate", lang))


# Last route taken per user (also logged): menu | quiz | agent | close | handoff |
# offtopic_warning | file_reply | file_note | limited | human | unsupported | on_request |
# consent | opt_out | lead_form. Used by the evals.
last_route: dict[str, str] = {}


def _route(msg: InboundMessage, name: str) -> None:
    last_route[msg.user_id] = name
    logger.info("route %s -> %s", limits.mask(msg.user_id), name)


async def _limit_notice(channel: Channel, msg: InboundMessage, kind: str) -> None:
    """A limit was hit: one static message per window (no model call) + a silent Slack note."""
    _route(msg, "limited")
    events.record("limit_hit", msg.user_id, limit=kind)
    if not limits.should_notify(msg.user_id, kind):
        return
    lang = menu.detect_lang(msg.text, store.get_lang(msg.user_id)) if msg.text else _lang_of(msg.user_id)
    await send_reply(channel, msg, limits.text(kind, lang))
    await _thread_note(msg, limits.note(kind))


def _is_closer(msg: InboundMessage) -> bool:
    """Goodbye keywords, checked before the guard and the agent (zero tokens). In the quiz its
    own exit words (salir, ...) only stop the quiz; with CLOSE_ON_INTENT other closers close."""
    if not msg.text or msg.media is not None:
        return False
    intent = get_settings().close_on_intent
    if quiz.active(msg.user_id) and not (intent and not msg.reply_id and not quiz.is_exit(msg.text)):
        return False
    return sessions.is_close_request(msg.text) or (intent and sessions.is_done_intent(msg.text))


async def _closer_or_human(channel: Channel, msg: InboundMessage) -> bool:
    """Goodbye and "talk to a human" keywords, which work before any limit or flow.
    True when the message was handled."""
    if _is_closer(msg):
        _route(msg, "close")
        if get_settings().close_on_intent:
            lang = menu.detect_lang(msg.text, store.get_lang(msg.user_id))
            store.set_lang(msg.user_id, lang)
            sessions.set_lang(msg.user_id, lang)
        await close_chat(msg.user_id, "user_keyword", user_text=msg.text)
        return True
    if msg.text and wants_human(msg.text):
        _route(msg, "handoff")
        reply = _handoff_reply(msg.user_id)
        await _lead_handoff(channel, msg, "User asked for a human")
        await hand_off(msg, "User asked for a human", "user_asked")
        await send_reply(channel, msg, reply)
        return True
    return False


# user_id -> topic label of the message being handled (guard label or route); see _record_user_msg
_turn_topic: dict[str, str] = {}


def _norm_entry(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def entry_source(text: str) -> str:
    """ENTRY_SOURCES maps a QR prefill text to a source ({"hola dexter": "qr_talk"});
    matched ignoring case, accents and punctuation. Anything else is "organic"."""
    raw = get_settings().entry_sources
    if raw:
        try:
            table = {_norm_entry(k): str(v) for k, v in json.loads(raw).items()}
        except (ValueError, AttributeError):
            logger.error("ENTRY_SOURCES is not a JSON object; ignoring it")
            table = {}
        hit = table.get(_norm_entry(text))
        if hit:
            return hit
    return "organic"


async def _csat_reply(channel: Channel, msg: InboundMessage) -> bool:
    """👍 / 👎 under the farewell: record it, thank once, don't reopen the chat. True if handled."""
    rid = msg.reply_id or ""
    if not rid.startswith("csat:"):
        return False
    pend = (sessions.get(msg.user_id) or {}).get("csat_pending")
    if not pend or sessions.now() - pend.get("at", 0) > CSAT_WINDOW:
        return False  # stale button: a normal message
    if pend.get("done"):
        return True   # already counted and thanked
    sessions.note(msg.user_id, csat_pending={**pend, "done": True})
    events.record("csat", msg.user_id, conversation_id=pend.get("conversation_id"),
                  value="up" if rid == "csat:up" else "down")
    try:
        await channel.send_text(msg.reply_to, CSAT_THANKS.get(pend.get("lang"), CSAT_THANKS["es"]))
    except Exception:  # noqa: BLE001
        logger.warning("CSAT thanks failed for %s", limits.mask(msg.user_id))
    return True


def _record_user_msg(msg: InboundMessage) -> None:
    """One user_msg event per inbound message: the route it took and its topic."""
    uid = msg.user_id
    route = last_route.get(uid, "unknown")
    topic = _turn_topic.pop(uid, None) or \
        topics.from_route(route, (sessions.get(uid) or {}).get("last_topic"))
    events.record("user_msg", uid, topic=topic, route=route, lang=store.get_lang(uid) or "es",
                  kind=msg.msg_type)


async def handle_inbound(channel: Channel, msg: InboundMessage) -> None:
    if await _csat_reply(channel, msg):
        return
    last_route.pop(msg.user_id, None)
    _turn_topic.pop(msg.user_id, None)
    try:
        await _handle_inbound(channel, msg)
    finally:
        _record_user_msg(msg)
        await leads.effects(msg.user_id)  # hot alert etc., after the replies; never raises


async def _handle_inbound(channel: Channel, msg: InboundMessage) -> None:
    try:
        await channel.mark_as_read(msg)
    except Exception:  # noqa: BLE001
        logger.warning("mark_as_read failed for %s", msg.message_id)

    restart_notice = sessions.needs_restart_notice(msg.user_id)
    was_open = sessions.is_open(msg.user_id)
    sessions.touch_user(msg.user_id, channel.name, msg.reply_to, msg.profile_name)
    store.touch_contact(msg.user_id, channel.name, msg.reply_to, msg.profile_name)
    if not was_open:
        events.record("conversation_start", msg.user_id, step="start", source=entry_source(msg.text),
                      lang=menu.detect_lang(msg.text, store.get_lang(msg.user_id)), channel=channel.name)

    bridge = slack_bridge.enabled()
    if bridge:  # mirror every customer message, whatever the mode
        await slack_bridge.ensure_thread(msg)
        await slack_bridge.mirror_customer(msg)

    lead_on = leads.enabled()
    if lead_on:
        leads.on_inbound(msg.user_id)
        if msg.text and msg.media is None and lead_flows.is_opt_out(msg.text):
            await _opt_out(channel, msg)
            return

    target = store.handoff_target(msg.user_id)
    if target == "flex":
        # While the Flex conversation is active, Twilio delivers the user's
        # messages to it, not to us. Reaching this webhook means the agent
        # wrapped up (conversation closed) -> the bot takes over again.
        store.end_handoff(msg.user_id)
        await notify_slack(f":leftwards_arrow_with_hook: {msg.profile_name or msg.user_id} is back with the bot (Flex conversation closed).")
    elif target == "slack":
        if not bridge:  # legacy webhook; with the bridge it's already in the thread
            await notify_slack(
                f":speech_balloon: ({msg.channel}) {msg.profile_name or msg.user_id} "
                f"[in human handoff]: {msg.text or '(non-text)'}"
            )
        store.log_turn(msg.user_id, "user", msg.text or f"({msg.msg_type})")
        _route(msg, "human")  # a person is in charge: never rate-limited
        if lead_on and msg.text:  # the consent question asked right before the handoff
            handled, _ = leads.safe("consent", lambda: lead_flows.answer_consent(
                msg.user_id, msg.text, msg.reply_id), default=(False, None)) or (False, None)
            if handled:
                lead = leads.safe("lead", lambda: leads.core.by_user(msg.user_id))
                await _thread_note(msg, "✅ Consentimiento de seguimiento: "
                                        + ("sí" if lead and lead.get("consent_followup") else "no"))
        return

    if restart_notice and get_settings().restart_notice:
        await _send_restart_notice(msg.user_id)

    is_new = not store.transcript(msg.user_id)
    store.log_turn(msg.user_id, "user", msg.text or f"({msg.msg_type})")

    if lead_on and msg.text and msg.media is None:  # answer to the consent question (zero tokens)
        handled, ack = leads.safe("consent", lambda: lead_flows.answer_consent(
            msg.user_id, msg.text, msg.reply_id), default=(False, None)) or (False, None)
        if handled:
            _route(msg, "consent")
            await send_reply(channel, msg, ack)
            return

    if await _closer_or_human(channel, msg):
        return

    if not msg.text and msg.media is None:
        _route(msg, "unsupported")
        await send_reply(channel, msg, "Por ahora solo entiendo texto, fotos y PDFs. / Text, photos and PDFs only for now.")
        return

    # Message caps (after "adiós" / "humano", which always work). Quiz answers don't count.
    listen = voice.is_voice(msg) and voice.enabled()
    is_file = msg.media is not None and (msg.msg_type in ("image", "document") or listen)
    exempt = msg.media is None and quiz.active(msg.user_id)
    kind = limits.check_inbound(msg.user_id, is_file=is_file, exempt=exempt)
    if kind:
        await _limit_notice(channel, msg, kind)
        return

    if listen:
        await _handle_voice(channel, msg, is_new)
        return

    if msg.media is not None:
        await _handle_media(channel, msg)
        return

    if get_settings().flows_enabled:
        await _route_flows(channel, msg, is_new)
        return

    await _run_agent(channel, msg, msg.text)


# --------------------------------------------------------------------------- #
# Admin (human-in-the-loop)
# --------------------------------------------------------------------------- #
class ReplyIn(BaseModel):
    to: str
    text: str
    channel: Optional[str] = None  # defaults to WA_PROVIDER
    release: bool = False          # hand the user back to the bot after sending


class ReleaseIn(BaseModel):
    user_id: str


def _check_admin(token: Optional[str]) -> None:
    expected = get_settings().admin_token
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="admin disabled or bad token")


def _resolve_channel(name: Optional[str]) -> Channel:
    channel = get_channel(name) if name else get_default_channel()
    if channel is None:
        raise HTTPException(status_code=400, detail=f"channel '{name or get_settings().wa_provider}' not configured")
    return channel


@app.post("/admin/reply")
async def admin_reply(body: ReplyIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    channel = _resolve_channel(body.channel)
    await channel.send_text(body.to, body.text)
    user_id = normalize_user_id(body.to)
    store.log_turn(user_id, "human", body.text)
    await slack_bridge.mirror_human(user_id, "admin", body.text)
    if body.release:
        store.end_handoff(user_id)
        if slack_bridge.enabled():
            await slack_bridge.release_control(user_id)
    return {"sent": True, "channel": channel.name}


@app.post("/admin/send")
async def admin_send(body: ReplyIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    """Bot-initiated message. Note: on Meta, outside the 24h window you need a template."""
    return await admin_reply(body, x_admin_token)


@app.post("/admin/release")
async def admin_release(body: ReleaseIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    user_id = normalize_user_id(body.user_id)
    store.end_handoff(user_id)
    if slack_bridge.enabled():
        await slack_bridge.release_control(user_id, note=True)
    return {"released": True}


@app.get("/admin/usage")
async def admin_usage(x_admin_token: Optional[str] = Header(None)) -> dict:
    """Today's (America/Mexico_City) token usage and the top 10 users, numbers masked."""
    _check_admin(x_admin_token)
    return await asyncio.to_thread(limits.summary)


@app.get("/admin/metrics")
async def admin_metrics(range: str = "7d", x_admin_token: Optional[str] = Header(None)) -> dict:
    """KPI overview (America/Mexico_City days), user ids masked. Debugging aid; Claude reads the
    same numbers through the MCP connector."""
    _check_admin(x_admin_token)
    try:
        return await asyncio.to_thread(metrics.overview, range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/admin/digest")
async def admin_digest(range: str = "yesterday", x_admin_token: Optional[str] = Header(None)) -> dict:
    """Post the metrics digest to Slack now (doesn't mark the day as sent)."""
    _check_admin(x_admin_token)
    try:
        return {"posted": await digest.send(range)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/admin/mcp/revoke-all")
async def admin_mcp_revoke_all(x_admin_token: Optional[str] = Header(None)) -> dict:
    """Revoke every MCP access and refresh token (claude.ai must reconnect)."""
    _check_admin(x_admin_token)
    return {"revoked": await asyncio.to_thread(mcp_server.revoke_all)}


@app.post("/admin/close")
async def admin_close(body: ReleaseIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    user_id = normalize_user_id(body.user_id)
    closed = await close_chat(user_id, "admin")
    return {"closed": closed}


# --------------------------------------------------------------------------- #
# Slack Events API (thread-per-conversation bridge)
# --------------------------------------------------------------------------- #
@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw = await request.body()
    if not slack_bridge.verify_signature(
        raw,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
    ):
        logger.warning("Rejected Slack event: bad signature or stale timestamp")
        raise HTTPException(status_code=403, detail="invalid signature")

    payload = json.loads(raw or b"{}")
    if payload.get("type") == "url_verification":
        return Response(content=json.dumps({"challenge": payload.get("challenge", "")}),
                        media_type="application/json")

    retry = request.headers.get("X-Slack-Retry-Num")
    if await asyncio.to_thread(store.slack_event_seen, payload.get("event_id", "")):
        logger.info("Duplicate Slack event %s ignored (retry=%s)", payload.get("event_id"), retry)
    elif payload.get("type") == "event_callback":
        background_tasks.add_task(slack_bridge.handle_slack_event, payload)
    return Response(content='{"ok":true}', media_type="application/json")


# --------------------------------------------------------------------------- #
# Read-only MCP connector (app/analytics/mcp_server.py). The paths are always routed; they
# answer 404 unless MCP_ENABLED and the lifespan started the server.
# --------------------------------------------------------------------------- #
mcp_server.mount(app)
