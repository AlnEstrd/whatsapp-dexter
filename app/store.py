"""Very small in-memory conversation store, keyed by WhatsApp sender id.

This is a POC: history lives only in this process's memory and is lost on
restart. Swap for Redis / a database before this goes anywhere near
production or runs on more than one worker process.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

MAX_TURNS = 20  # keep the last N user+assistant messages per sender

_conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)


def get_history(wa_id: str) -> list[dict[str, Any]]:
    return _conversations[wa_id]


def append_message(wa_id: str, role: str, content: Any) -> None:
    history = _conversations[wa_id]
    history.append({"role": role, "content": content})
    if len(history) > MAX_TURNS:
        del history[: len(history) - MAX_TURNS]


def reset(wa_id: str) -> None:
    _conversations.pop(wa_id, None)


# --- Per-user language ("es" | "en") ----------------------------------------
_lang: dict[str, str] = {}


def get_lang(user_id: str) -> str | None:
    return _lang.get(user_id)


def set_lang(user_id: str, lang: str) -> None:
    _lang[user_id] = lang


# --- Human handoff state ---------------------------------------------------
# Users currently being handled by a human: the bot stays quiet for them
# until /admin/release is called.
_handoff: dict[str, str] = {}  # user_id -> target ("slack" | "flex")

# Plain-text transcript per user (what the human sees on handoff). Kept
# separate from the agent's Claude-format history on purpose.
_transcripts: dict[str, list[tuple[str, str]]] = defaultdict(list)
MAX_TRANSCRIPT = 20


def log_turn(user_id: str, role: str, text: str) -> None:
    t = _transcripts[user_id]
    t.append((role, text))
    if len(t) > MAX_TRANSCRIPT:
        del t[: len(t) - MAX_TRANSCRIPT]


def transcript(user_id: str) -> list[tuple[str, str]]:
    return list(_transcripts.get(user_id, []))

# Recently seen provider message ids (providers retry webhooks).
_seen_ids: dict[str, None] = {}
_MAX_SEEN = 2000


def start_handoff(user_id: str, target: str = "slack") -> None:
    _handoff[user_id] = target


def end_handoff(user_id: str) -> None:
    _handoff.pop(user_id, None)


def in_handoff(user_id: str) -> bool:
    return user_id in _handoff


def handoff_target(user_id: str) -> str | None:
    return _handoff.get(user_id)


def seen_before(message_id: str) -> bool:
    """True if we already processed this message id; records it otherwise."""
    if not message_id:
        return False
    if message_id in _seen_ids:
        return True
    _seen_ids[message_id] = None
    if len(_seen_ids) > _MAX_SEEN:
        for k in list(_seen_ids)[: len(_seen_ids) - _MAX_SEEN]:
            del _seen_ids[k]
    return False


# --- Slack conversation threads ---------------------------------------------
# One Slack thread per WhatsApp conversation (see app/slack_bridge.py).
#
# Kept in memory, plus a small JSON snapshot (SLACK_STATE_FILE) so a restart or
# `uvicorn --reload` doesn't orphan open threads. Agent history is NOT saved.
# Kept behind these small functions so it can move to Redis later.
import json as _json
import logging as _logging
import os as _os
import time as _time

_threads: dict[str, dict[str, Any]] = {}   # user_id -> thread state
_thread_index: dict[str, str] = {}         # thread_ts -> user_id
_slack_event_ids: dict[str, None] = {}     # Slack event_id dedupe (Slack retries)


def now() -> float:
    """Clock used by the Slack bridge (patched in tests)."""
    return _time.time()


def get_thread(user_id: str) -> dict[str, Any] | None:
    return _threads.get(user_id)


def set_thread(user_id: str, state: dict[str, Any]) -> None:
    """state: thread_ts, channel_id, last_customer_at, mode ("bot" | "human"),
    plus channel_name / reply_to / profile_name to route replies back."""
    old = _threads.get(user_id)
    if old and old.get("thread_ts") in _thread_index:
        _thread_index.pop(old["thread_ts"], None)
    _threads[user_id] = state
    _thread_index[state["thread_ts"]] = user_id


def user_for_thread(thread_ts: str) -> str | None:
    return _thread_index.get(thread_ts)


def set_mode(user_id: str, mode: str) -> None:
    if user_id in _threads:
        _threads[user_id]["mode"] = mode


def get_mode(user_id: str) -> str:
    t = _threads.get(user_id)
    return t["mode"] if t else ("human" if handoff_target(user_id) == "slack" else "bot")


def touch_customer(user_id: str, at: float | None = None) -> None:
    t = _threads.get(user_id)
    if t:
        t["last_customer_at"] = now() if at is None else at
        t["window_warned"] = False
        t["customer_msgs"] = t.get("customer_msgs", 0) + 1


def close_thread(user_id: str) -> None:
    """Forget the Slack thread for this user; their next message opens a new one."""
    t = _threads.pop(user_id, None)
    if t:
        _thread_index.pop(t.get("thread_ts"), None)


def clear_transcript(user_id: str) -> None:
    _transcripts.pop(user_id, None)


def all_threads() -> dict[str, dict[str, Any]]:
    return _threads


def slack_event_seen(event_id: str) -> bool:
    """True if this Slack event_id was already processed; records it otherwise."""
    if not event_id:
        return False
    if event_id in _slack_event_ids:
        return True
    _slack_event_ids[event_id] = None
    if len(_slack_event_ids) > _MAX_SEEN:
        for k in list(_slack_event_ids)[: len(_slack_event_ids) - _MAX_SEEN]:
            del _slack_event_ids[k]
    return False


def save_threads(path: str) -> None:
    """Snapshot thread map + Slack handoffs to disk (atomic replace)."""
    if not path:
        return
    try:
        data = {"threads": _threads,
                "handoff": {u: t for u, t in _handoff.items() if t == "slack"}}
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(data, fh)
        _os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        _logging.getLogger("store").exception("Could not save Slack thread state")


def load_threads(path: str) -> int:
    """Restore a snapshot written by save_threads. Returns threads loaded."""
    if not path or not _os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
        for user_id, state in (data.get("threads") or {}).items():
            set_thread(user_id, state)
        for user_id, target in (data.get("handoff") or {}).items():
            _handoff.setdefault(user_id, target)
        return len(_threads)
    except Exception:  # noqa: BLE001
        _logging.getLogger("store").exception("Could not load Slack thread state")
        return 0


def reset_threads() -> None:  # used by tests
    _threads.clear()
    _thread_index.clear()
    _slack_event_ids.clear()
