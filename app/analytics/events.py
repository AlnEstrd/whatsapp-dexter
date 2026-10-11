"""Event log: funnel steps, outcomes and disconnect reasons, written to SQLite (``events``).

    record("quiz_answer", user_id, step="quiz_q5", q=4)

Fire-and-forget: nothing here may raise into the WhatsApp path. Each write is handed to
one dedicated worker thread (``run_in_executor``, i.e. ``asyncio.to_thread`` on a
single-thread pool, so writes stay in order) and goes through ``db.safe``. Outside an
event loop (startup, plain tests) it writes inline.

Privacy: ``events`` keeps the full user id internally but never free message text; text
lives only in ``question_samples``, scrubbed (:func:`scrub`). Anything that leaves the DB
(metrics, digest, MCP) masks ids with :func:`mask`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .. import db, limits, sessions
from ..config import get_settings
from . import pricing

logger = logging.getLogger("analytics")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics")
SAMPLE_MAX = 200


def enabled() -> bool:
    return bool(get_settings().analytics_enabled and db.enabled())


def iso(t: Optional[float] = None) -> str:
    """UTC ISO-8601 with a fixed width, so strings sort like the times they hold."""
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mask(user_id: Optional[str]) -> str:
    return limits.mask(user_id or "")


# --------------------------------------------------------------------------- #
# Scrubbing (question samples, gap notes)
# --------------------------------------------------------------------------- #
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Digit groups joined by spaces, dots, dashes or parentheses: phones, cards, ids.
_NUMBERISH = re.compile(r"\+?\(?\d[\d\s().-]*\d")


_DIGIT_RUN = re.compile(r"\d{6,}")


def _digits(m: re.Match) -> str:
    # 8+ digits with separators: a phone or card number ("10-20-70" stays)
    return "[…]" if sum(c.isdigit() for c in m.group(0)) >= 8 else m.group(0)


def scrub(text: Optional[str]) -> str:
    """Replace URLs, emails, phone and card numbers (8+ digits with separators) and long digit
    runs (6+ digits in a row) with
    ``[…]``; truncate to 200 characters."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = _URL.sub("[…]", t)
    t = _EMAIL.sub("[…]", t)
    t = _NUMBERISH.sub(_digits, t)
    t = _DIGIT_RUN.sub("[…]", t)
    return t if len(t) <= SAMPLE_MAX else t[: SAMPLE_MAX - 1] + "…"


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
def _submit(label: str, fn: Callable[[], Any]) -> None:
    def run() -> None:
        try:
            db.safe(label, fn)
        except Exception:  # noqa: BLE001
            logger.exception("analytics write %s failed", label)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run()
        return
    loop.run_in_executor(_executor, run)


def drain(timeout: float = 5.0) -> None:
    """Block until every queued write is done (tests, shutdown, the eval harness)."""
    try:
        _executor.submit(lambda: None).result(timeout=timeout)
    except Exception:  # noqa: BLE001
        logger.warning("analytics drain timed out")


def record(type_: str, user_id: Optional[str], step: Optional[str] = None,
           conversation_id: Optional[str] = None, **data: Any) -> None:
    """Log one event. ``step`` also becomes the session's ``last_step``. Never raises."""
    try:
        if step and user_id:
            sessions.note_step(user_id, step)
        if not enabled():
            return
        conv = conversation_id or (sessions.conversation_id(user_id) if user_id else None)
        row = (iso(), user_id, conv, type_, step, db.dumps(data))
        _submit("event", lambda: db.execute(
            "INSERT INTO events (ts, user_id, conversation_id, type, step, data_json) VALUES (?, ?, ?, ?, ?, ?)",
            row))
    except Exception:  # noqa: BLE001
        logger.exception("record(%s) failed", type_)


def sample(user_id: str, text: str, topic: str, lang: Optional[str], kind: str = "question") -> Optional[str]:
    """Store one scrubbed text sample; returns its id (to link a gap to it)."""
    try:
        if not enabled() or not (text or "").strip():
            return None
        sid = uuid.uuid4().hex[:16]
        row = (sid, iso(), sessions.conversation_id(user_id), topic, lang, kind, scrub(text))
        _submit("sample", lambda: db.execute(
            "INSERT INTO question_samples (id, ts, conversation_id, topic, lang, kind, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", row))
        return sid
    except Exception:  # noqa: BLE001
        logger.exception("sample failed")
        return None


def llm_call(user_id: Optional[str], purpose: str, model: Optional[str], response: Any = None,
             latency_ms: Optional[int] = None, error: Optional[BaseException] = None) -> None:
    """One Anthropic call (agent | guard | other), from the response's ``usage`` object.
    Failed calls are logged too (error = exception class name, zero tokens)."""
    try:
        if not enabled():
            return
        u = limits.usage_of(response) if response is not None else {}
        row = (iso(), user_id, sessions.conversation_id(user_id) if user_id else None, purpose,
               pricing.normalize(model), u.get("input_tokens", 0), u.get("output_tokens", 0),
               u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0), latency_ms,
               getattr(response, "stop_reason", None), error.__class__.__name__ if error else None)
        _submit("llm_call", lambda: db.execute(
            """INSERT INTO llm_calls (ts, user_id, conversation_id, purpose, model, input_tokens,
                   output_tokens, cache_read_tokens, cache_write_tokens, latency_ms, stop_reason, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row))
    except Exception:  # noqa: BLE001
        logger.exception("llm_call record failed")


def outbound(user_id: str, wamid: Optional[str]) -> None:
    """A bot message went out with Meta id ``wamid``: remember it for status callbacks."""
    if not wamid:
        return
    try:
        sessions.note_bot_msg(user_id, wamid)
        if not enabled():
            return
        row = (wamid, iso(), user_id, sessions.conversation_id(user_id))
        _submit("outbound", lambda: db.execute(
            "INSERT OR IGNORE INTO outbound_messages (wamid, ts, user_id, conversation_id) VALUES (?, ?, ?, ?)",
            row))
    except Exception:  # noqa: BLE001
        logger.exception("outbound record failed")


def status(status_obj: dict) -> None:
    """A Meta ``statuses`` callback (sent / delivered / read / failed) -> ``msg_status``.
    Called from a background task; never reaches handle_inbound."""
    try:
        from ..channels import normalize_user_id
        wamid = str(status_obj.get("id") or "")
        st = str(status_obj.get("status") or "")
        user_id = normalize_user_id(str(status_obj.get("recipient_id") or "")) or None
        errors = status_obj.get("errors") or []
        code = errors[0].get("code") if errors and isinstance(errors[0], dict) else None
        if user_id and st == "read":
            sessions.note_read(user_id, wamid)
        if not enabled():
            return
        conv = None
        rows = db.safe("wamid lookup", lambda: db.query(
            "SELECT user_id, conversation_id FROM outbound_messages WHERE wamid = ?", (wamid,)), default=[])
        if rows:
            user_id, conv = rows[0]["user_id"] or user_id, rows[0]["conversation_id"]
        record("msg_status", user_id, conversation_id=conv, status=st, error_code=code, wamid=wamid)
    except Exception:  # noqa: BLE001
        logger.exception("status record failed")
