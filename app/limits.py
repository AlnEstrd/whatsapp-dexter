"""Usage accounting and cost caps.

Every Anthropic response (main agent, guard classifier) is recorded per user
per day (``America/Mexico_City``) in the ``usage`` table, including cache
tokens. With ``LIMITS_ENABLED=true`` (default):

    USER_MSG_PER_MINUTE   inbound messages per user per rolling minute
    USER_MSG_PER_DAY      inbound messages per user per day
    USER_FILES_PER_DAY    inbound images/PDFs per user per day
    USER_TOKENS_PER_DAY   input+output tokens per user per day, all model calls
    DAILY_TOKEN_BUDGET    input+output tokens for everyone; 80% -> one Slack alert,
                          100% -> no model calls for anyone until tomorrow

A blocked user gets one short static message per limit window (no model call)
and the Slack thread gets a silent note; main.py does the sending. Message
caps are checked after the deterministic "adiós" / "humano" handling, quiz
answers don't count against them, and human-handoff mode is never limited.
The token caps only stop model calls: menu, quiz and slides keep working.
Any DB error fails open (the message goes through).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db
from .config import get_settings

logger = logging.getLogger("limits")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Mexico_City")
except Exception:  # noqa: BLE001  (no tz database in the image: CDMX is UTC-6, no DST since 2022)
    TZ = timezone(timedelta(hours=-6))

TEXT = {
    "minute": {"es": "Recibí muchos mensajes seguidos; dame un minuto 🙂",
               "en": "I got a lot of messages in a row; give me a minute 🙂"},
    "day": {"es": "Por hoy llegamos al límite de mensajes con el asistente. Alan puede darte seguimiento: "
                  "escribe *humano* y le paso la conversación.",
            "en": "We've reached today's message limit with the assistant. Alan can follow up: "
                  "type *human* and I'll pass him the conversation."},
    "files": {"es": "Por hoy ya no puedo revisar más archivos. Si lo necesitas, escribe *humano* y Alan "
                    "le da seguimiento.",
              "en": "I can't review more files today. If you need it, type *human* and Alan will follow up."},
    "tokens": {"es": "Por hoy llegamos al límite de uso del asistente. Puedes seguir usando el *menú* y el "
                     "*diagnóstico*, o escribe *humano* y Alan te da seguimiento.",
               "en": "We've reached today's usage limit for the assistant. You can still use the *menu* and "
                     "the *assessment*, or type *human* and Alan will follow up."},
    "budget": {"es": "El asistente llegó a su límite de uso por hoy. Puedes seguir usando el *menú* y el "
                     "*diagnóstico*, o escribe *humano* y Alan te da seguimiento.",
               "en": "The assistant reached its usage limit for today. You can still use the *menu* and the "
                     "*assessment*, or type *human* and Alan will follow up."},
}

NOTE = {
    "minute": "⏳ Rate limit: more than {n} messages in a minute; the customer was asked to wait.",
    "day": "⏳ Daily message limit ({n}) reached; the customer was offered a handoff.",
    "files": "⏳ Daily file limit ({n}) reached; files are no longer read today.",
    "tokens": "⏳ Daily token limit ({n:,}) reached for this customer; model calls paused until tomorrow.",
    "budget": "⏳ Global daily token budget ({n:,}) exhausted; model calls paused for everyone.",
}

_minute: dict[str, deque] = defaultdict(deque)  # user -> timestamps of accepted messages
_minute_noticed: set[str] = set()               # notice sent for the current burst


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.fromtimestamp(now(), TZ).strftime("%Y-%m-%d")


def enabled() -> bool:
    return get_settings().limits_enabled


def text(kind: str, lang: Optional[str]) -> str:
    return TEXT[kind][lang if lang in ("es", "en") else "es"]


def note(kind: str) -> str:
    s = get_settings()
    n = {"minute": s.user_msg_per_minute, "day": s.user_msg_per_day, "files": s.user_files_per_day,
         "tokens": s.user_tokens_per_day, "budget": s.daily_token_budget}[kind]
    return NOTE[kind].format(n=n)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def _bump(user_id: str, **cols: int) -> None:
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = {c} + excluded.{c}" for c in cols)
    db.safe("usage", lambda: db.execute(
        f"INSERT INTO usage (user_id, day, {names}) VALUES (?, ?, {marks}) "
        f"ON CONFLICT(user_id, day) DO UPDATE SET {updates}",
        (user_id, today(), *cols.values())))


def usage_of(response: Any) -> dict[str, int]:
    u = getattr(response, "usage", None)
    if u is None:
        return {}

    def g(name: str) -> int:
        v = u.get(name) if isinstance(u, dict) else getattr(u, name, 0)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    return {"input_tokens": g("input_tokens"), "output_tokens": g("output_tokens"),
            "cache_read_tokens": g("cache_read_input_tokens"),
            "cache_write_tokens": g("cache_creation_input_tokens")}


async def record(user_id: str, response: Any, label: str = "") -> None:
    """Store the token usage of one Anthropic response; alert Slack near the budget."""
    try:
        cols = usage_of(response)
        if not cols or not any(cols.values()):
            return
        _bump(user_id, **cols)
        logger.info("usage %s %s in=%d out=%d cache_r=%d cache_w=%d", label, mask(user_id),
                    cols["input_tokens"], cols["output_tokens"], cols["cache_read_tokens"],
                    cols["cache_write_tokens"])
        await _budget_alerts()
    except Exception:  # noqa: BLE001
        logger.exception("usage recording failed")


async def _budget_alerts() -> None:
    budget = get_settings().daily_token_budget
    if budget <= 0:
        return
    used = total_tokens_today()
    day = today()
    for pct, key in ((1.0, "budget100"), (0.8, "budget80")):
        if used >= pct * budget:
            if db.safe("flag", lambda k=key: db.set_flag_once(f"{k}:{day}"), default=False):
                await _alert(f"{'🛑' if pct == 1 else '⚠️'} {_owner()} Daily token budget at "
                             f"{used / budget:.0%} ({used:,} / {budget:,})"
                             + (": model calls are paused until tomorrow (CDMX)." if pct == 1 else "."))
            break


def _owner() -> str:
    uid = get_settings().slack_alert_user_id
    return f"<@{uid}>" if uid else "<!here>"


async def _alert(text_: str) -> None:
    from .escalation import notify_slack  # top-level post (webhook, or the bot token)
    try:
        await notify_slack(text_)
    except Exception:  # noqa: BLE001
        logger.exception("budget alert failed")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _today_row(user_id: str) -> dict[str, int]:
    rows = db.safe("usage read", lambda: db.query(
        "SELECT * FROM usage WHERE user_id = ? AND day = ?", (user_id, today())), default=[])
    return dict(rows[0]) if rows else {}


def total_tokens_today() -> int:
    rows = db.safe("usage total", lambda: db.query(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage WHERE day = ?", (today(),)),
        default=[])
    return int(rows[0][0]) if rows else 0


def check_inbound(user_id: str, is_file: bool = False, exempt: bool = False) -> Optional[str]:
    """Count one inbound message. Returns the limit it hits ("minute" | "day" | "files")
    or None when it may go on. ``exempt`` (quiz answers): counted for the day, never blocked."""
    s = get_settings()
    row = _today_row(user_id)
    if enabled() and not exempt:
        t = now()
        q = _minute[user_id]
        while q and t - q[0] >= 60:
            q.popleft()
        if s.user_msg_per_minute > 0 and len(q) >= s.user_msg_per_minute:
            return "minute"
        if s.user_msg_per_day > 0 and row.get("requests", 0) >= s.user_msg_per_day:
            return "day"
        if is_file and s.user_files_per_day > 0 and row.get("files", 0) >= s.user_files_per_day:
            return "files"
    if not exempt:
        _minute[user_id].append(now())
        _minute_noticed.discard(user_id)
    _bump(user_id, requests=1, files=int(is_file))
    return None


def model_block(user_id: str) -> Optional[str]:
    """Before a model call: "budget" | "tokens" when it must not happen, else None."""
    if not enabled():
        return None
    s = get_settings()
    if s.daily_token_budget > 0 and total_tokens_today() >= s.daily_token_budget:
        return "budget"
    row = _today_row(user_id)
    if s.user_tokens_per_day > 0 and \
            row.get("input_tokens", 0) + row.get("output_tokens", 0) >= s.user_tokens_per_day:
        return "tokens"
    return None


def should_notify(user_id: str, kind: str) -> bool:
    """Once per window: per burst for the per-minute limit, per day for the others."""
    if kind == "minute":
        if user_id in _minute_noticed:
            return False
        _minute_noticed.add(user_id)
        return True
    return bool(db.safe("flag", lambda: db.set_flag_once(f"notice:{today()}:{kind}:{user_id}"),
                        default=True))


def reset() -> None:  # tests
    _minute.clear()
    _minute_noticed.clear()


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def mask(user_id: str) -> str:
    return "****" + (user_id or "")[-4:]


def summary() -> dict:
    s = get_settings()
    day = today()
    cols = ["requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "files"]
    totals_row = db.safe("usage summary", lambda: db.query(
        "SELECT " + ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for c in cols) +
        ", COUNT(*) AS users FROM usage WHERE day = ?", (day,)), default=[])
    totals = dict(totals_row[0]) if totals_row else {c: 0 for c in cols + ["users"]}
    used = totals["input_tokens"] + totals["output_tokens"]
    top = db.safe("usage top", lambda: db.query(
        "SELECT * FROM usage WHERE day = ? ORDER BY input_tokens + output_tokens DESC, requests DESC "
        "LIMIT 10", (day,)), default=[])
    return {
        "day": day,
        "timezone": "America/Mexico_City",
        "limits_enabled": s.limits_enabled,
        "totals": {**totals, "tokens": used},
        "budget": {"daily_token_budget": s.daily_token_budget,
                   "used_pct": round(100 * used / s.daily_token_budget, 1) if s.daily_token_budget else None},
        "top_users": [{"user": mask(r["user_id"]), "tokens": r["input_tokens"] + r["output_tokens"],
                       **{c: r[c] for c in cols}} for r in top],
    }
