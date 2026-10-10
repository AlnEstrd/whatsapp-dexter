"""FastAPI service: provider-agnostic WhatsApp webhooks -> agent -> reply.

    POST /webhooks/twilio        Twilio WhatsApp (Sandbox / sender)
    GET  /webhooks/meta          Meta webhook verification (alias: /webhook)
    POST /webhooks/meta          Meta Cloud API messages + account events
    POST /admin/reply            Human replies to a user (X-Admin-Token)
    POST /admin/release          Hand a user back to the bot
    POST /admin/close            Close a conversation (user gets a closing message)
    POST /slack/events           Slack Events API (thread replies, take/bot)
    POST /admin/send             Bot-initiated message via WA_PROVIDER
    GET  /                       Health: enabled channels, provider, agent

Inbound messages are answered on the channel they arrived on, so Twilio and
Meta can run side by side during a migration.

Run with:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from . import attachments, docs, guard, sessions, slack_bridge, store
from .agent import get_agent, pop_action, pop_documents
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
from .formatting import to_whatsapp
from .handoff import hand_off
from .resilience import with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    if slack_bridge.enabled():
        slack_bridge.restore_state()
        await slack_bridge.load_bot_user_id()
        tasks.append(asyncio.create_task(slack_bridge.window_watcher()))
    loaded = sessions.load(get_settings().sessions_file)
    logger.info("Sessions loaded: %d (boot %s)", loaded, sessions.BOOT_ID)
    tasks.append(asyncio.create_task(lifecycle_watcher()))
    tasks.append(asyncio.create_task(recover_after_restart()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="WhatsApp Agentic Bot POC", lifespan=lifespan)

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
        background_tasks.add_task(alert_provider_event, event)

    allowed = get_settings().allowed_senders_list
    for msg in parsed.messages:
        if store.seen_before(msg.message_id):
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
        await with_retry(
            lambda: channel.send_text(msg.reply_to, text),
            attempts=3,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.send",
        )
        store.log_turn(msg.user_id, "bot", text)
        sessions.touch_bot(msg.user_id)
        await slack_bridge.mirror_bot(msg.user_id, text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not deliver reply to %s", msg.user_id)
        reason = f"Reply delivery failed on {channel.name}: {exc}"
        if not (slack_bridge.enabled() and await slack_bridge.escalate_in_thread(msg, reason)):
            await escalate(msg, reason)
        return False


async def send_out(channel: Channel, msg: InboundMessage, out: Outgoing) -> bool:
    """Send text, as an interactive list when the channel supports it.
    Falls back to plain text (which already numbers the options)."""
    send_options = getattr(channel, "send_options", None)
    if out.options and send_options:
        try:
            await with_retry(
                lambda: send_options(msg.reply_to, out.text, out.button, out.options),
                attempts=2,
                is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
                label=f"{channel.name}.send_options",
            )
            store.log_turn(msg.user_id, "bot", out.text)
            sessions.touch_bot(msg.user_id)
            await slack_bridge.mirror_bot(msg.user_id, out.text)
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
        await with_retry(
            lambda: sender(msg.reply_to, doc.data, doc.filename, doc.caption, **extra),
            attempts=2,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.send_document",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not deliver %s to %s", doc.filename, msg.user_id)
        await send_reply(channel, msg, DOC_FAILED[lang])
        await _thread_note(msg, f"⚠️ Could not deliver {doc.filename}: {exc}")
        return False
    note = f"📎 {doc.filename}"
    store.log_turn(msg.user_id, "bot", note)
    await slack_bridge.mirror_bot(msg.user_id, note)
    return True


async def _send_deck(channel: Channel, msg: InboundMessage, lang: str, slide: Optional[str] = None,
                     fmt: str = "image") -> None:
    """Whole deck (PDF) or one slide: an inline image by default, a PDF if asked."""
    try:
        doc = docs.make_slide_image(slide, lang) if (slide and fmt == "image") else docs.make_deck(slide, lang)
    except ValueError:
        await send_reply(channel, msg, f"{UNKNOWN_SLIDE[lang]}\n\n{docs.slide_index_text(lang)}")
        return
    if await send_file(channel, msg, doc) and slide is None:
        await send_reply(channel, msg, docs.slide_index_text(lang))


async def _send_assessment_pdf(channel: Channel, msg: InboundMessage, result: quiz.Result) -> bool:
    try:
        doc = docs.make_assessment(result, msg.profile_name)
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


async def _to_human(channel: Channel, msg: InboundMessage, reason: str) -> None:
    await hand_off(msg, reason)
    await send_reply(channel, msg, HANDOFF_REPLY)


async def _start_quiz(channel: Channel, msg: InboundMessage, lang: str) -> None:
    for out in quiz.start(msg.user_id, lang):
        await send_out(channel, msg, out)
    sessions.touch_bot(msg.user_id, quiz=True)


async def _quiz_step(channel: Channel, msg: InboundMessage) -> None:
    before = quiz.last_result(msg.user_id)
    outs = quiz.answer(msg.user_id, msg.text, msg.reply_id)
    result = quiz.last_result(msg.user_id)
    finished = result is not None and result is not before
    for i, out in enumerate(outs):
        await send_out(channel, msg, out)
        if finished and i == 0 and getattr(channel, "send_document", None):
            # result text first, then the PDF report, then the follow-up line (skipped silently where files can't be sent)
            await _send_assessment_pdf(channel, msg, result)
    if finished:
        await _thread_note(msg, quiz.summary_for_slack(result))
    sessions.touch_bot(msg.user_id, quiz=quiz.active(msg.user_id), context=finished or None)


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


async def _handle_media(channel: Channel, msg: InboundMessage) -> None:
    """Photo / screenshot / PDF from the attendee: validate, then let Claude look at it."""
    uid = msg.user_id
    lang = menu.detect_lang(msg.text, store.get_lang(uid)) if msg.text else (store.get_lang(uid) or "es")
    if quiz.active(uid):
        await send_reply(channel, msg, MEDIA_NOTE["quiz"][lang])
        return
    if not hasattr(channel, "download_media") or not msg.media:
        await send_reply(channel, msg, MEDIA_NOTE["channel"][lang])
        return
    try:
        async with typing(channel, msg):  # downloading/validating can take a few seconds
            att = await attachments.load(channel, msg.media)
    except attachments.Rejected as exc:
        await send_reply(channel, msg, MEDIA_NOTE[exc.reason][lang])
        return
    except Exception:  # noqa: BLE001
        logger.exception("Media download failed for %s", uid)
        await send_reply(channel, msg, MEDIA_NOTE["failed"][lang])
        return
    await _run_agent(channel, msg, msg.text, attachments=[att])


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
                     attachments: Optional[list] = None) -> bool:
    """Agent turn + any action a tool requested. False if the agent failed (handed off)."""
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
        await _to_human(channel, msg, f"Agent error: {exc}")
        return False

    sessions.touch_bot(msg.user_id, context=True)
    action = pop_action(msg.user_id)
    documents = pop_documents(msg.user_id)
    if action and action["action"] == "close":
        # end_conversation: the agent's own text is dropped, the farewell is the only message
        for doc in documents:
            await send_file(channel, msg, doc)
        await close_chat(msg.user_id, "user_intent", farewell=action.get("farewell"), user_text=text)
        return True
    if reply or not (action or documents):
        await send_bubbles(channel, msg, reply or FALLBACK_REPLY)
    for doc in documents:
        await send_file(channel, msg, doc)
    if action and action["action"] == "report":
        result = quiz.last_result(msg.user_id)
        if result is not None:
            await _send_assessment_pdf(channel, msg, result)
    elif action and action["action"] == "quiz":
        await _start_quiz(channel, msg, action.get("lang") or store.get_lang(msg.user_id) or "es")
    elif action and action["action"] == "human":
        await hand_off(msg, f"Talk to Alan: {action.get('reason', '')}".strip())
    return True


async def _route_flows(channel: Channel, msg: InboundMessage, is_new: bool) -> bool:
    """Talk-companion routing. Returns True when the message was handled."""
    uid = msg.user_id
    lang = menu.detect_lang(msg.text, store.get_lang(uid))
    store.set_lang(uid, lang)
    sessions.set_lang(uid, lang)

    if quiz.active(uid):
        await _quiz_step(channel, msg)
        return True

    choice = msg.reply_id if (msg.reply_id or "").startswith("menu:") else menu.typed_choice(msg.text)
    slide = menu.slide_request(msg.text)
    if slide:
        await _send_deck(channel, msg, lang, *slide)
    elif choice == "menu:slides":
        await _send_deck(channel, msg, lang)
    elif choice == "menu:report":
        result = quiz.last_result(uid)
        if result is not None:
            await _send_assessment_pdf(channel, msg, result)
        else:
            await send_reply(channel, msg, NO_RESULT[lang])
    elif choice == "menu:quiz":
        await _start_quiz(channel, msg, lang)
    elif choice == "menu:talk":
        await send_out(channel, msg, menu.talk_overview(lang))
    elif choice == "menu:about":
        await _run_agent(channel, msg, "¿Quién es Alan Estrada?" if lang == "es" else "Who is Alan Estrada?")
    elif choice == "menu:human":
        await _to_human(channel, msg, "User asked to talk to Alan")
    elif menu.is_greeting(msg.text):
        await send_out(channel, msg, menu.welcome(lang, msg.profile_name))
    elif not await guard.on_topic(uid, msg.text):
        await _off_topic(channel, msg, lang)
    else:
        ok = await _run_agent(channel, msg, msg.text)
        if ok and is_new and sessions.is_open(uid) and not store.in_handoff(uid) and not quiz.active(uid):
            await send_out(channel, msg, menu.welcome(lang, msg.profile_name))
    return True


# --------------------------------------------------------------------------- #
# Conversation lifecycle: close, idle, restart recovery, off-topic
# --------------------------------------------------------------------------- #
LIFECYCLE_INTERVAL = 30  # seconds between idle checks
RESTART_NOTICE_MAX_AGE = 23 * 3600  # stay inside WhatsApp's 24h free-form window
RESTART_DELAY = 3  # seconds after startup before sending restart notices


def _lang_of(user_id: str) -> str:
    s = sessions.get(user_id) or {}
    return store.get_lang(user_id) or s.get("lang") or "es"


async def _notify(user_id: str, text: str) -> bool:
    """System message to the user on their last channel (doesn't count as activity)."""
    s = sessions.get(user_id) or {}
    channel = get_channel(s.get("channel_name", ""))
    if channel is None or not s.get("reply_to"):
        logger.warning("Cannot notify %s: no channel/address on record", user_id)
        return False
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


# reason -> who closed it (closing message key, Slack label)
CLOSE_REASONS = {"user_keyword": "user", "user_intent": "user", "idle": "bot", "admin": "admin"}


async def close_chat(user_id: str, reason: str, slack_cleanup: bool = True, *,
                     farewell: Optional[str] = None, user_text: Optional[str] = None) -> bool:
    """The one close routine (reason = "user_keyword" | "user_intent" | "idle" | "admin"):
    closing message to the user, then wipe agent memory, quiz, transcript, handoff and idle
    timers. The next message starts fresh. ``farewell``: the agent's goodbye (user_intent)."""
    by = CLOSE_REASONS[reason]
    if not sessions.is_open(user_id) and by != "admin":
        return False
    lang = _lang_of(user_id)
    if reason == "user_intent":
        closing = sessions.farewell_text(to_whatsapp(farewell or ""), lang)
    else:
        closing = sessions.text(f"close_{by}", lang)
    await _notify(user_id, closing)
    sessions.mark_closed(user_id, by, reason)
    quiz._states.pop(user_id, None)
    pop_action(user_id)
    pop_documents(user_id)
    store.reset(user_id)
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
                sessions.mark_closed(uid, "bot")  # would have been closed while we were down
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


async def lifecycle_watcher() -> None:  # started from the app lifespan
    while True:
        await asyncio.sleep(LIFECYCLE_INTERVAL)
        try:
            await check_idle()
        except Exception:  # noqa: BLE001
            logger.exception("Idle check failed")


async def _off_topic(channel: Channel, msg: InboundMessage, lang: str) -> None:
    """Off-topic message: static warning (no main-agent tokens); escalate after the max."""
    n = sessions.bump_offtopic(msg.user_id)
    mx = get_settings().offtopic_max_warnings
    if n <= mx:
        await send_reply(channel, msg, sessions.text("offtopic_warn", lang, n=n, max=mx))
        await _thread_note(msg, f"🚧 Off-topic warning {n}/{mx}.")
    else:
        await hand_off(msg, f"Off-topic after {mx} warnings")
        await send_reply(channel, msg, sessions.text("offtopic_escalate", lang))


async def handle_inbound(channel: Channel, msg: InboundMessage) -> None:
    try:
        await channel.mark_as_read(msg)
    except Exception:  # noqa: BLE001
        logger.warning("mark_as_read failed for %s", msg.message_id)

    restart_notice = sessions.needs_restart_notice(msg.user_id)
    sessions.touch_user(msg.user_id, channel.name, msg.reply_to, msg.profile_name)

    bridge = slack_bridge.enabled()
    if bridge:  # mirror every customer message, whatever the mode
        await slack_bridge.ensure_thread(msg)
        await slack_bridge.mirror_customer(msg)

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
        return

    if restart_notice and get_settings().restart_notice:
        await _send_restart_notice(msg.user_id)

    is_new = not store.transcript(msg.user_id)
    store.log_turn(msg.user_id, "user", msg.text or f"({msg.msg_type})")

    # Goodbye keywords: before the guard and the agent (zero tokens). In the quiz, its own exit
    # words (salir, ...) only stop the quiz; with CLOSE_ON_INTENT other closers close the chat.
    intent = get_settings().close_on_intent
    if msg.text and msg.media is None and not (intent and msg.reply_id) \
            and sessions.is_close_request(msg.text, widened=intent) \
            and (not quiz.active(msg.user_id) or (intent and not quiz.is_exit(msg.text))):
        if intent:
            lang = menu.detect_lang(msg.text, store.get_lang(msg.user_id))
            store.set_lang(msg.user_id, lang)
            sessions.set_lang(msg.user_id, lang)
        await close_chat(msg.user_id, "user_keyword", user_text=msg.text)
        return

    if not msg.text and msg.media is None:
        await send_reply(channel, msg, "Por ahora solo entiendo texto, fotos y PDFs. / Text, photos and PDFs only for now.")
        return

    if msg.text and wants_human(msg.text):
        await hand_off(msg, "User asked for a human")
        await send_reply(channel, msg, HANDOFF_REPLY)
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
    if store.slack_event_seen(payload.get("event_id", "")):
        logger.info("Duplicate Slack event %s ignored (retry=%s)", payload.get("event_id"), retry)
    elif payload.get("type") == "event_callback":
        background_tasks.add_task(slack_bridge.handle_slack_event, payload)
    return Response(content='{"ok":true}', media_type="application/json")
