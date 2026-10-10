"""Conversation state keyed by WhatsApp user id: agent history, language, handoff,
transcript, webhook dedupe and the Slack thread map.

The dicts below are the working copy; every change is written through to
SQLite (app/db.py) and :func:`load_all` reads it back on startup, so a restart
keeps conversations. If a DB write fails the change stays in memory for the
turn (``db.safe`` logs it and alerts Slack once). Single worker on purpose.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any

from . import db

logger = logging.getLogger("store")

HISTORY_MAX_MESSAGES = 40  # default; HISTORY_MAX_MESSAGES in .env overrides it

_conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)


def _max_history() -> int:
    from .config import get_settings
    return get_settings().history_max_messages or HISTORY_MAX_MESSAGES


# --------------------------------------------------------------------------- #
# Agent history (Anthropic message format, text only)
# --------------------------------------------------------------------------- #
def _block(b: Any) -> dict:
    if isinstance(b, dict):
        d = dict(b)
    elif hasattr(b, "model_dump"):
        d = b.model_dump(exclude_none=True)
    else:  # SimpleNamespace & co.
        d = {k: v for k, v in vars(b).items() if v is not None}
    if d.get("type") in ("image", "document"):  # never keep file bytes, same rule as before
        return {"type": "text", "text": "[file omitted]"}
    return d


def _jsonable(content: Any) -> Any:
    """Plain dicts/strings: what the API accepts and what we can store as JSON."""
    if isinstance(content, str):
        return content
    return [_block(b) for b in content]


def _plain_user(m: dict) -> bool:
    return m["role"] == "user" and isinstance(m["content"], str)


def trim_history(history: list[dict], max_messages: int) -> list[dict]:
    """Keep the newest ``max_messages``, starting at a plain user message, so a
    tool_use is never separated from its tool_result."""
    if len(history) <= max_messages:
        return history
    cut = len(history) - max_messages
    i = next((j for j in range(cut, len(history)) if _plain_user(history[j])), None)
    if i is None:  # no clean boundary inside the window: start at the last one before it
        i = next((j for j in range(cut - 1, -1, -1) if _plain_user(history[j])), cut)
    return history[i:]


def get_history(wa_id: str) -> list[dict[str, Any]]:
    return _conversations[wa_id]


def _save_history(wa_id: str) -> None:
    history = _conversations.get(wa_id) or []
    t = db.now()

    def _write():
        with db.transaction() as c:
            c.execute("DELETE FROM messages WHERE user_id = ?", (wa_id,))
            c.executemany(
                "INSERT INTO messages (user_id, role, content_json, created_at) VALUES (?, ?, ?, ?)",
                [(wa_id, m["role"], db.dumps(m["content"]), t) for m in history])
    db.safe("save history", _write)


def append_message(wa_id: str, role: str, content: Any) -> None:
    history = _conversations[wa_id]
    history.append({"role": role, "content": _jsonable(content)})
    trimmed = trim_history(history, _max_history())
    if trimmed is not history:
        history[:] = trimmed
    _save_history(wa_id)


def reset(wa_id: str) -> None:
    """Drop the agent history (e.g. after a failed, half-written tool turn)."""
    _conversations.pop(wa_id, None)
    db.safe("reset history", lambda: db.execute("DELETE FROM messages WHERE user_id = ?", (wa_id,)))


def wipe_conversation(user_id: str) -> None:
    """Conversation closed: history and quiz progress go, in one DB transaction."""
    from .flows import quiz
    _conversations.pop(user_id, None)
    quiz._states.pop(user_id, None)
    db.safe("wipe conversation", lambda: db.wipe_conversation(user_id))


# --------------------------------------------------------------------------- #
# Conversation row: language, handoff mode, transcript, contact
# --------------------------------------------------------------------------- #
_lang: dict[str, str] = {}
_handoff: dict[str, str] = {}  # user_id -> target ("slack" | "flex"); bot stays quiet meanwhile
# Plain-text transcript per user (what the human sees on handoff). Kept
# separate from the agent's Claude-format history on purpose.
_transcripts: dict[str, list[tuple[str, str]]] = defaultdict(list)
_contact: dict[str, dict[str, Any]] = {}  # channel, reply_to, profile_name, last_customer_at
MAX_TRANSCRIPT = 20


def _save_conv(user_id: str) -> None:
    c = _contact.get(user_id, {})
    t = db.now()
    row = (user_id, c.get("channel"), c.get("reply_to"), c.get("profile_name"),
           _handoff.get(user_id, "bot"), _lang.get(user_id),
           db.dumps(list(_transcripts.get(user_id, []))), c.get("last_customer_at"), t, t)
    db.safe("save conversation", lambda: db.execute(
        """INSERT INTO conversations (user_id, channel, reply_to, profile_name, mode, lang,
               transcript_json, last_customer_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET channel = excluded.channel,
               reply_to = excluded.reply_to, profile_name = excluded.profile_name,
               mode = excluded.mode, lang = excluded.lang, transcript_json = excluded.transcript_json,
               last_customer_at = excluded.last_customer_at, updated_at = excluded.updated_at""", row))


def touch_contact(user_id: str, channel: str, reply_to: str, profile_name: str | None = None) -> None:
    """Inbound message: remember where to reply (written with the next conversation save)."""
    c = _contact.setdefault(user_id, {})
    c.update(channel=channel, reply_to=reply_to, last_customer_at=db.now())
    if profile_name:
        c["profile_name"] = profile_name


def get_lang(user_id: str) -> str | None:
    return _lang.get(user_id)


def set_lang(user_id: str, lang: str) -> None:
    if _lang.get(user_id) != lang:
        _lang[user_id] = lang
        _save_conv(user_id)


def log_turn(user_id: str, role: str, text: str) -> None:
    t = _transcripts[user_id]
    t.append((role, text))
    if len(t) > MAX_TRANSCRIPT:
        del t[: len(t) - MAX_TRANSCRIPT]
    _save_conv(user_id)


def transcript(user_id: str) -> list[tuple[str, str]]:
    return list(_transcripts.get(user_id, []))


def clear_transcript(user_id: str) -> None:
    _transcripts.pop(user_id, None)
    _save_conv(user_id)


def start_handoff(user_id: str, target: str = "slack") -> None:
    _handoff[user_id] = target
    _save_conv(user_id)


def end_handoff(user_id: str) -> None:
    if _handoff.pop(user_id, None) is not None:
        _save_conv(user_id)


def in_handoff(user_id: str) -> bool:
    return user_id in _handoff


def handoff_target(user_id: str) -> str | None:
    return _handoff.get(user_id)


# --------------------------------------------------------------------------- #
# Webhook dedupe (providers retry). Backed by processed_messages; the dict is
# only the fallback when the DB is off or failing.
# --------------------------------------------------------------------------- #
_seen_ids: dict[str, None] = {}
_MAX_SEEN = 2000


def _seen_in_memory(key: str) -> bool:
    if key in _seen_ids:
        return True
    _seen_ids[key] = None
    if len(_seen_ids) > _MAX_SEEN:
        for k in list(_seen_ids)[: len(_seen_ids) - _MAX_SEEN]:
            del _seen_ids[k]
    return False


def seen_before(message_id: str, prefix: str = "") -> bool:
    """True if we already processed this message id; records it otherwise.
    Safe to call from a worker thread (``asyncio.to_thread``)."""
    if not message_id:
        return False
    key = f"{prefix}:{message_id}" if prefix else message_id
    is_new = db.safe("dedupe", lambda: db.mark_processed(key))
    if is_new is None:  # DB off or failed
        return _seen_in_memory(key)
    return not is_new


def slack_event_seen(event_id: str) -> bool:
    """True if this Slack event_id was already processed; records it otherwise."""
    return seen_before(event_id, prefix="slack")


# --------------------------------------------------------------------------- #
# Slack conversation threads (one thread per WhatsApp conversation, see
# app/slack_bridge.py), persisted in slack_threads.
# --------------------------------------------------------------------------- #
_threads: dict[str, dict[str, Any]] = {}   # user_id -> thread state
_thread_index: dict[str, str] = {}         # thread_ts -> user_id


def now() -> float:
    """Clock used by the Slack bridge (patched in tests)."""
    return time.time()


def save_thread(user_id: str) -> None:
    """Persist one thread's state (call after mutating the dict from get_thread)."""
    t = _threads.get(user_id)
    if not t:
        return

    def _write():
        with db.transaction() as c:
            # thread_ts is unique: a stale row for another user must go first
            c.execute("DELETE FROM slack_threads WHERE thread_ts = ? AND user_id != ?",
                      (t["thread_ts"], user_id))
            c.execute("""INSERT INTO slack_threads (user_id, thread_ts, channel_id, last_customer_at, mode, state_json)
                         VALUES (?, ?, ?, ?, ?, ?)
                         ON CONFLICT(user_id) DO UPDATE SET thread_ts = excluded.thread_ts,
                             channel_id = excluded.channel_id, last_customer_at = excluded.last_customer_at,
                             mode = excluded.mode, state_json = excluded.state_json""",
                      (user_id, t["thread_ts"], t.get("channel_id"), t.get("last_customer_at"),
                       t.get("mode"), db.dumps(t)))
    db.safe("save thread", _write)


def get_thread(user_id: str) -> dict[str, Any] | None:
    return _threads.get(user_id)


def _index_thread(user_id: str, state: dict[str, Any]) -> None:
    old = _threads.get(user_id)
    if old and old.get("thread_ts") in _thread_index:
        _thread_index.pop(old["thread_ts"], None)
    _threads[user_id] = state
    _thread_index[state["thread_ts"]] = user_id


def set_thread(user_id: str, state: dict[str, Any]) -> None:
    """state: thread_ts, channel_id, last_customer_at, mode ("bot" | "human"),
    plus channel_name / reply_to / profile_name to route replies back."""
    _index_thread(user_id, state)
    save_thread(user_id)


def user_for_thread(thread_ts: str) -> str | None:
    return _thread_index.get(thread_ts)


def set_mode(user_id: str, mode: str) -> None:
    if user_id in _threads:
        _threads[user_id]["mode"] = mode
        save_thread(user_id)


def get_mode(user_id: str) -> str:
    t = _threads.get(user_id)
    return t["mode"] if t else ("human" if handoff_target(user_id) == "slack" else "bot")


def touch_customer(user_id: str, at: float | None = None) -> None:
    t = _threads.get(user_id)
    if t:
        t["last_customer_at"] = now() if at is None else at
        t["window_warned"] = False
        t["customer_msgs"] = t.get("customer_msgs", 0) + 1
        save_thread(user_id)


def close_thread(user_id: str) -> None:
    """Forget the Slack thread for this user; their next message opens a new one."""
    t = _threads.pop(user_id, None)
    if t:
        _thread_index.pop(t.get("thread_ts"), None)
        db.safe("close thread", lambda: db.execute("DELETE FROM slack_threads WHERE user_id = ?", (user_id,)))


def all_threads() -> dict[str, dict[str, Any]]:
    return _threads


def reset_threads() -> None:  # used by tests (memory only)
    _threads.clear()
    _thread_index.clear()


# --------------------------------------------------------------------------- #
# Startup: restore from SQLite, one-time import of the old JSON snapshot
# --------------------------------------------------------------------------- #
def import_legacy_threads(path: str) -> int:
    """Import ``.slack_threads.json`` (written by older versions) once, then rename it
    to ``*.imported``. Skipped if the table already has rows. Returns threads imported."""
    if not path or not os.path.exists(path) or not db.enabled():
        return 0
    if db.count("slack_threads"):
        logger.info("Slack thread snapshot %s ignored: table already populated", path)
        return 0
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    threads = data.get("threads") or {}
    for user_id, state in threads.items():
        if state.get("thread_ts"):
            set_thread(user_id, state)
    for user_id, target in (data.get("handoff") or {}).items():
        start_handoff(user_id, target)
    os.replace(path, f"{path}.imported")
    logger.info("Imported %d Slack thread(s) and %d handoff(s) from the JSON snapshot",
                len(threads), len(data.get("handoff") or {}))
    return len(threads)


def load_all() -> dict[str, int]:
    """Read conversations, history and thread links back into memory. Raises on DB errors
    (the caller then falls back to the restart apology)."""
    counts = {"conversations": 0, "histories": 0, "threads": 0}
    if not db.enabled():
        return counts
    for row in db.query("SELECT * FROM conversations"):
        uid = row["user_id"]
        if row["lang"]:
            _lang[uid] = row["lang"]
        if row["mode"] in ("slack", "flex"):
            _handoff[uid] = row["mode"]
        tr = [tuple(x) for x in json.loads(row["transcript_json"] or "[]")]
        if tr:
            _transcripts[uid] = tr
        _contact[uid] = {k: row[k] for k in ("channel", "reply_to", "profile_name", "last_customer_at")}
        counts["conversations"] += 1
    loaded: dict[str, list] = defaultdict(list)
    for row in db.query("SELECT user_id, role, content_json FROM messages ORDER BY id"):
        loaded[row["user_id"]].append({"role": row["role"], "content": json.loads(row["content_json"])})
    for uid, history in loaded.items():
        _conversations[uid] = history
    counts["histories"] = len(loaded)
    for row in db.query("SELECT user_id, state_json FROM slack_threads"):
        _index_thread(row["user_id"], json.loads(row["state_json"]))
    counts["threads"] = len(_threads)
    return counts


def forget(user_id: str) -> None:
    """Drop a purged user from memory (rows already deleted by db.purge)."""
    from .flows import quiz
    for d in (_conversations, _lang, _handoff, _transcripts, _contact, quiz._states, quiz._results):
        d.pop(user_id, None)
    t = _threads.pop(user_id, None)
    if t:
        _thread_index.pop(t.get("thread_ts"), None)


def reset_memory() -> None:  # tests: simulate a fresh process
    for d in (_conversations, _lang, _handoff, _transcripts, _contact, _seen_ids):
        d.clear()
    reset_threads()


def has_history(user_id: str) -> bool:
    return bool(_conversations.get(user_id))
