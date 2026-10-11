"""Slack thread-per-conversation bridge (two-way, needs a Slack *app*).

Every WhatsApp conversation gets one thread in SLACK_CHANNEL_ID:

  parent   💬 New chat — Alan (+523312345678) · meta
  replies  👤 customer text / 🤖 bot reply / 🧑 human reply (via /admin/reply)
           🚨 <@owner> escalated: ...   (broadcast to the channel)

From inside a thread the owner can:
  take  (or ✋ on the parent)   -> bot pauses, thread replies go to WhatsApp
  bot   (or ✅ on the parent)   -> bot resumes
  close (or 🔒 on the parent)   -> bot resumes, agent memory cleared, thread
                                   unlinked: the next message opens a new thread

Only active when SLACK_BOT_TOKEN + SLACK_CHANNEL_ID are set; otherwise the
legacy Incoming Webhook (app/escalation.py) is used unchanged.
Slack failures never break WhatsApp handling: everything here logs and
carries on.

Thread links and modes are persisted in SQLite (app/store.py -> slack_threads),
so restarts keep existing threads working. Single process.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from . import store
from .channels import InboundMessage, SendError, get_channel
from .config import get_settings
from .resilience import with_retry

logger = logging.getLogger("slack_bridge")

SLACK_API = "https://slack.com/api"
MAX_TEXT = 3000
WINDOW_WARN_AFTER = 23 * 3600          # warn when the 24h CS window has <1h left
WINDOW_CHECK_INTERVAL = 300            # seconds between background window checks
TAKE_REACTION = "raised_hand"
RELEASE_REACTION = "white_check_mark"
CLOSE_REACTION = "lock"
IGNORED_SUBTYPES = {"bot_message", "message_changed", "message_deleted", "channel_join",
                    "channel_leave", "thread_broadcast_deleted"}

_bot_user_id: Optional[str] = None     # cached from auth.test / event authorizations


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return get_settings().slack_threads_enabled


def _persist(user_id: str) -> None:
    store.save_thread(user_id)


def restore_state() -> int:
    """Called at startup (after store.load_all): one-time import of the JSON snapshot
    older versions wrote (SLACK_STATE_FILE). Returns threads imported."""
    return store.import_legacy_threads(get_settings().slack_state_file)


def escape(text: str) -> str:
    """Slack mrkdwn escaping + truncation for untrusted text."""
    text = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def unescape(text: str) -> str:
    """Slack sends &, <, > escaped; undo that before forwarding to WhatsApp."""
    return (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _owner_mention() -> str:
    uid = get_settings().slack_alert_user_id
    return f"<@{uid}>" if uid else "<!here>"


def verify_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """Slack request signing: v0=HMAC-SHA256(secret, "v0:{ts}:{body}"), 5 min skew."""
    secret = get_settings().slack_signing_secret
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(store.now() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# --------------------------------------------------------------------------- #
# Thin Slack Web API client
# --------------------------------------------------------------------------- #
async def _api(method: str, payload: dict) -> dict:
    token = get_settings().slack_bot_token
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{SLACK_API}/{method}",
            json=payload,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"},
        )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed ({resp.status_code}): {data.get('error') or resp.text[:200]}")
    return data


async def post_message(channel: str, text: str, thread_ts: str | None = None,
                       reply_broadcast: bool = False) -> Optional[str]:
    """Returns the message ts, or None on failure (logged, never raised)."""
    payload: dict[str, Any] = {"channel": channel, "text": text, "unfurl_links": False,
                               "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
        if reply_broadcast:
            payload["reply_broadcast"] = True
    try:
        return (await _api("chat.postMessage", payload)).get("ts")
    except Exception:  # noqa: BLE001
        logger.exception("Slack chat.postMessage failed")
        return None


async def add_reaction(channel: str, ts: str, name: str) -> bool:
    try:
        await _api("reactions.add", {"channel": channel, "timestamp": ts, "name": name})
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Slack reactions.add(%s) failed", name)
        return False


async def load_bot_user_id() -> Optional[str]:
    """Cache our own bot user id (auth.test) so we ignore our own events."""
    global _bot_user_id
    try:
        _bot_user_id = (await _api("auth.test", {})).get("user_id")
    except Exception:  # noqa: BLE001
        logger.warning("Slack auth.test failed; relying on bot_id / authorizations")
    return _bot_user_id


async def post_top_level(text: str) -> bool:
    """Channel-level (non-threaded) post, used for provider alerts."""
    return await post_message(get_settings().slack_channel_id, text) is not None


# --------------------------------------------------------------------------- #
# WhatsApp -> Slack
# --------------------------------------------------------------------------- #
async def _thread_post(user_id: str, text: str, reply_broadcast: bool = False) -> Optional[str]:
    t = store.get_thread(user_id)
    if not t:
        return None
    return await post_message(t["channel_id"], text, thread_ts=t["thread_ts"],
                              reply_broadcast=reply_broadcast)


async def ensure_thread(msg: InboundMessage) -> Optional[dict]:
    """Open a thread on the first message, or after SLACK_THREAD_IDLE_HOURS
    without customer messages (WhatsApp 24h customer-service window closed)."""
    if not enabled():
        return None
    try:
        s = get_settings()
        t = store.get_thread(msg.user_id)
        idle = s.slack_thread_idle_hours * 3600
        if t and store.now() - t["last_customer_at"] <= idle:
            t.update(channel_name=msg.channel, reply_to=msg.reply_to,
                     profile_name=msg.profile_name or t.get("profile_name"))
            _persist(msg.user_id)
            return t

        name = escape(msg.profile_name or (t or {}).get("profile_name") or "Unknown")
        ts = await post_message(s.slack_channel_id,
                                f"💬 New chat — {name} (+{msg.user_id}) · {msg.channel}")
        if not ts:
            return t  # keep the old thread (if any) rather than lose mirroring
        mode = "human" if store.handoff_target(msg.user_id) == "slack" else "bot"
        state = {
            "thread_ts": ts,
            "channel_id": s.slack_channel_id,
            "last_customer_at": store.now(),
            "mode": mode,
            "channel_name": msg.channel,
            "reply_to": msg.reply_to,
            "profile_name": msg.profile_name,
            "customer_msgs": 0,
            "window_warned": False,
        }
        store.set_thread(msg.user_id, state)
        if mode == "human":
            await _thread_post(msg.user_id, "✋ Still in human mode from a previous thread. "
                                            "Messages here go to the customer. Type `bot` to hand back.")
        return state
    except Exception:  # noqa: BLE001
        logger.exception("ensure_thread failed for %s", msg.user_id)
        return None


async def mirror_customer(msg: InboundMessage) -> None:
    if not enabled():
        return
    try:
        store.touch_customer(msg.user_id)
        body = escape(msg.text) if msg.text else f"({msg.msg_type})"
        if msg.media:
            name = msg.media.get("filename")
            body += f" 📎 {escape(name)}" if name else f" 📎 {escape(msg.media.get('mime') or msg.msg_type)}"
        await _thread_post(msg.user_id, f"👤 {body}")
    except Exception:  # noqa: BLE001
        logger.exception("mirror_customer failed for %s", msg.user_id)


async def mirror_bot(user_id: str, text: str) -> None:
    if not enabled():
        return
    try:
        await _thread_post(user_id, f"🤖 {escape(text)}")
    except Exception:  # noqa: BLE001
        logger.exception("mirror_bot failed for %s", user_id)


async def mirror_human(user_id: str, name: str, text: str) -> None:
    if not enabled():
        return
    try:
        await _thread_post(user_id, f"🧑 {escape(name)}: {escape(text)}")
    except Exception:  # noqa: BLE001
        logger.exception("mirror_human failed for %s", user_id)


async def escalate_in_thread(msg: InboundMessage, reason: str) -> bool:
    """Owner-facing escalation: mode=human + broadcast mention in the thread.
    Returns False if no thread could be used (caller falls back to webhook)."""
    try:
        t = store.get_thread(msg.user_id) or await ensure_thread(msg)
        if not t:
            return False
        store.start_handoff(msg.user_id, "slack")
        store.set_mode(msg.user_id, "human")
        text = f"🚨 {_owner_mention()} escalated: {escape(reason)}"
        if t.get("customer_msgs", 0) <= 1:  # thread just opened -> add context
            lines = [f"{role}: {txt}" for role, txt in store.transcript(msg.user_id)][-10:]
            if len(lines) > 1:
                text += "\n*Recent transcript:*\n```" + escape("\n".join(lines))[-1500:] + "```"
        text += "\nReply in this thread to answer the customer. Type `bot` to hand back."
        return await _thread_post(msg.user_id, text, reply_broadcast=True) is not None
    except Exception:  # noqa: BLE001
        logger.exception("escalate_in_thread failed for %s", msg.user_id)
        return False


async def check_windows() -> None:
    """Warn once per human-mode thread when the 24h WhatsApp window is about to close."""
    if not enabled():
        return
    for user_id, t in list(store.all_threads().items()):
        try:
            if t.get("mode") != "human" or t.get("window_warned"):
                continue
            if store.now() - t["last_customer_at"] > WINDOW_WARN_AFTER:
                t["window_warned"] = True
                _persist(user_id)
                await _thread_post(user_id, f"⏰ {_owner_mention()} the WhatsApp 24h window closes "
                                            "in <1h; after that only templates can be sent.")
        except Exception:  # noqa: BLE001
            logger.exception("window check failed for %s", user_id)


async def window_watcher() -> None:  # started from the app lifespan
    while True:
        await asyncio.sleep(WINDOW_CHECK_INTERVAL)
        await check_windows()


# --------------------------------------------------------------------------- #
# Slack -> WhatsApp
# --------------------------------------------------------------------------- #
async def take_control(user_id: str, by: str) -> None:
    if store.get_mode(user_id) == "human" and store.handoff_target(user_id) == "slack":
        return  # already human; no duplicate confirmation
    store.start_handoff(user_id, "slack")
    store.set_mode(user_id, "human")
    await _thread_post(user_id, f"✋ <@{by}> took control. Messages in this thread go to the "
                                "customer. Type `bot` to hand back.")


async def release_control(user_id: str, note: bool = True) -> None:
    was_human = store.get_mode(user_id) == "human" or store.in_handoff(user_id)
    store.end_handoff(user_id)
    store.set_mode(user_id, "bot")
    # The agent keeps its own history untouched; the Claude message format has
    # no system turns, so no context note is injected here.
    if note and was_human:
        await _thread_post(user_id, "🤖 Bot resumed.")


# Set by main.py: sends the customer a closing message and resets the session.
close_hook = None
# Set by main.py (app/leads/alerts.handle_command): lead commands typed in a thread.
lead_command_hook = None


async def close_thread(user_id: str, note: str) -> None:
    """Post a final note and unlink the thread (the next message opens a new one)."""
    if not store.get_thread(user_id):
        return
    await _thread_post(user_id, note)
    store.close_thread(user_id)


async def close_conversation(user_id: str, by: str) -> None:
    """End the conversation: tell the customer, hand back to the bot, wipe the
    agent's memory and the transcript, and unlink the thread so the next
    message starts fresh."""
    if not store.get_thread(user_id):
        return
    if close_hook is not None:
        try:
            await close_hook(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("close hook failed for %s", user_id)
    await _thread_post(user_id, f"🔒 Conversation closed by <@{by}>. Agent memory cleared; "
                                "the next customer message opens a new thread.")
    store.end_handoff(user_id)
    store.wipe_conversation(user_id)  # agent (Claude) history + quiz progress
    store.clear_transcript(user_id)   # plain-text transcript used for escalations
    store.close_thread(user_id)


async def send_human_reply(user_id: str, text: str, slack_ts: str) -> None:
    t = store.get_thread(user_id)
    if not t:
        return
    await check_windows()
    channel = get_channel(t["channel_name"])
    if channel is None:
        await add_reaction(t["channel_id"], slack_ts, "warning")
        await _thread_post(user_id, f"⚠️ Channel `{t['channel_name']}` is not configured; not sent.")
        return
    body = unescape(text)
    try:
        await with_retry(
            lambda: channel.send_text(t["reply_to"], body),
            attempts=3,
            is_retryable=lambda e: isinstance(e, SendError) and e.retryable,
            label=f"{channel.name}.send(human)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Human reply to %s failed: %s", user_id, exc)
        await add_reaction(t["channel_id"], slack_ts, "warning")
        if "131047" in str(exc):
            note = ("⚠️ Not delivered: the WhatsApp 24h window is closed (Meta error 131047). "
                    "Only template messages can be sent until the customer writes again.")
        else:
            note = f"⚠️ Not delivered to WhatsApp: {escape(str(exc))[:300]}"
        await _thread_post(user_id, note)
        return
    store.log_turn(user_id, "human", body)
    await add_reaction(t["channel_id"], slack_ts, "white_check_mark")


def _is_own(event: dict, payload: dict) -> bool:
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return True
    me = {_bot_user_id}
    for a in payload.get("authorizations") or []:
        if a.get("is_bot") is not False:
            me.add(a.get("user_id"))
    me.discard(None)
    return event.get("user") in me


async def handle_slack_event(payload: dict) -> None:
    """Process one Events API callback (already signature-checked and deduped)."""
    if not enabled():
        return
    event = payload.get("event") or {}
    etype = event.get("type")
    try:
        if _is_own(event, payload):
            return
        settings = get_settings()

        if etype == "message":
            if event.get("subtype") in IGNORED_SUBTYPES:
                return
            thread_ts, ts = event.get("thread_ts"), event.get("ts")
            if not thread_ts or thread_ts == ts:
                return  # top-level channel message, not a conversation reply
            # Lead commands (contacted, meeting, note ..., followup) in a conversation thread or
            # a hot-alert thread of the leads channel; never forwarded to the customer.
            if lead_command_hook is not None and await lead_command_hook(
                    event.get("channel"), thread_ts, (event.get("text") or "").strip(), event.get("user", "")):
                return
            if event.get("channel") and event["channel"] != settings.slack_channel_id:
                return
            user_id = store.user_for_thread(thread_ts)
            text = (event.get("text") or "").strip()
            cmd = text.lower()
            if not user_id:
                if cmd in ("take", "bot", "close"):
                    await post_message(settings.slack_channel_id,
                                       "⚠️ This thread is no longer linked to a conversation. "
                                       "Use the newest 💬 thread for this customer.",
                                       thread_ts=thread_ts)
                return
            if cmd == "take":
                await take_control(user_id, event.get("user", ""))
            elif cmd == "bot":
                await release_control(user_id)
            elif cmd == "close":
                await close_conversation(user_id, event.get("user", ""))
            elif store.get_mode(user_id) == "human" and text:
                await send_human_reply(user_id, text, ts)
            # bot mode: other replies are internal notes -> ignored

        elif etype == "reaction_added":
            item = event.get("item") or {}
            if item.get("type") != "message":
                return
            user_id = store.user_for_thread(item.get("ts", ""))
            if not user_id:
                return  # only reactions on the parent message count
            if event.get("reaction") == TAKE_REACTION:
                await take_control(user_id, event.get("user", ""))
            elif event.get("reaction") == RELEASE_REACTION:
                await release_control(user_id)
            elif event.get("reaction") == CLOSE_REACTION:
                await close_conversation(user_id, event.get("user", ""))
    except Exception:  # noqa: BLE001
        logger.exception("Slack event handling failed (%s)", etype)


def reset() -> None:  # used by tests
    global _bot_user_id
    _bot_user_id = None
