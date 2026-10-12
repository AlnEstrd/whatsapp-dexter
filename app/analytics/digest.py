"""Daily metrics digest in Slack.

Every day at METRICS_DIGEST_HOUR (America/Mexico_City, default 8) the lifecycle loop posts
yesterday's summary, top-level, to SLACK_METRICS_CHANNEL_ID (falls back to SLACK_CHANNEL_ID)
with chat.postMessage. Short and in Spanish. No mentions unless ``anomalies()`` found
something; then SLACK_ALERT_USER_ID is mentioned once. A ``digest:<day>`` flag in SQLite
records the post, so a restart never double-posts. Slack failures are logged and retried
at most every 15 minutes; they never touch WhatsApp handling.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from .. import db, slack_bridge
from ..config import get_settings
from . import events, metrics, topics

logger = logging.getLogger("digest")

RETRY_SECONDS = 15 * 60
_last_attempt = 0.0

REASONS_ES = {"user_asked": "pidió hablar con Alan", "agent_error": "error del agente",
              "offtopic": "fuera de tema", "delivery_failure": "falla de envío", "limit": "límite",
              "other": "otro"}


def channel() -> Optional[str]:
    s = get_settings()
    return s.slack_metrics_channel_id or s.slack_channel_id


def enabled() -> bool:
    s = get_settings()
    return bool(s.metrics_digest_enabled and events.enabled() and s.slack_bot_token and channel())


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0%}"


def build(range_: str = "yesterday") -> str:
    r = metrics.parse_range(range_)
    day = r.first_day.isoformat() if r.first_day == r.last_day else f"{r.first_day}…{r.last_day}"
    found = metrics.anomalies(r)["anomalies"]
    alert = ""
    if found:
        uid = get_settings().slack_alert_user_id
        alert = (f"<@{uid}> " if uid else "") + "⚠️ Anomalías:\n" + "\n".join(f"   – {a['message']}" for a in found)
    conv = metrics.conversations(r)
    if not conv["conversations"]:
        return f"📊 *Dexter — {day}* (CDMX): sin conversaciones." + (f"\n{alert}" if alert else "")

    cont = metrics.containment(r)
    esc = metrics.escalation(r)
    comp = metrics.completion(r)
    top = metrics.topics_board(r, top_n=5)["topics"]
    drop = metrics.close_reasons(r)["idle_drop_off"]
    gaps = metrics.gaps_board(r, top_n=3, with_samples=False)["topics"]
    cost = metrics.costs(r)
    src = ", ".join(f"{k} {v}" for k, v in sorted(conv["by_source"].items(), key=lambda kv: -kv[1]))
    reasons = ", ".join(f"{REASONS_ES.get(k, k)} {n}" for k, n in
                        sorted(esc["handoffs_by_reason"].items(), key=lambda kv: -kv[1])) or "ninguno"
    q = comp["quiz"]
    lines = [
        f"📊 *Dexter — {day}* (días en hora CDMX)",
        f"• Conversaciones: *{conv['conversations']}*" + (f" ({src})" if src else ""),
        f"• Contención: {_pct(cont['containment_rate'])} ({cont['contained']}/{cont['closed']} cerradas sin Alan)",
        f"• Escalamientos: {sum(esc['handoffs_by_reason'].values())} — {reasons}",
        f"• Diagnóstico: {q['completed']}/{q['started']} completados ({_pct(q['rate'])})",
        "• Top temas: " + (" · ".join(f"{t['label']} {t['messages']}" for t in top) or "—"),
    ]
    if drop:
        d = drop[0]
        lines.append(f"• Mayor abandono: `{d['last_step']}` ({d['closes']} cierres por inactividad, "
                     f"{d['read']} leyeron el último mensaje)")
    lines.append("• Huecos de la KB: " + (", ".join(topics.label(g["topic"]) for g in gaps) or "ninguno"))
    per = cost["per_conversation"]["mean_usd"]
    lines.append(f"• Costo: ${cost['total_usd']:.2f} · ${per or 0:.3f} por conversación · "
                 f"cache hit {_pct(cost['cache_hit_ratio'])}")
    turns = metrics.agent_turns(r)
    if turns["reply_words"]["p90"] is not None:
        lines.append(f"• Respuestas: media {turns['reply_words']['mean']} palabras (p90 "
                     f"{turns['reply_words']['p90']}), {_pct(turns['trim_rate'])} recortadas")
    lines.append(alert or "• Anomalías: ninguna")
    return "\n".join(lines)


async def send(range_: str = "yesterday") -> bool:
    """Build and post one digest. True when Slack accepted it. Never raises (bad range: ValueError)."""
    metrics.parse_range(range_)  # validate before anything else (the admin endpoint maps it to 400)
    ch = channel()
    if not (get_settings().slack_bot_token and ch):
        logger.warning("Digest not sent: SLACK_BOT_TOKEN / SLACK_METRICS_CHANNEL_ID missing")
        return False
    try:
        text = await asyncio.to_thread(build, range_)
        return await slack_bridge.post_message(ch, text) is not None
    except Exception:  # noqa: BLE001
        logger.exception("Digest failed")
        return False


async def maybe_send() -> bool:
    """Called from the lifecycle loop: post yesterday's digest once, after the digest hour."""
    global _last_attempt
    if not enabled():
        return False
    local = datetime.fromtimestamp(metrics.now(), metrics.TZ)
    if local.hour < get_settings().metrics_digest_hour:
        return False
    day = (local.date() - timedelta(days=1)).isoformat()
    key = f"digest:{day}"
    if db.safe("digest flag", lambda: db.has_flag(key), default=True):
        return False
    if time.monotonic() - _last_attempt < RETRY_SECONDS and _last_attempt:
        return False
    _last_attempt = time.monotonic()
    if await send(day):
        db.safe("digest flag", lambda: db.set_flag_once(key))
        return True
    return False


def reset() -> None:  # tests
    global _last_attempt
    _last_attempt = 0.0
