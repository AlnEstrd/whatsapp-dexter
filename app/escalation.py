"""Human escalation + operational alerts, delivered to Slack.

Uses a Slack Incoming Webhook (no bot app needed). If SLACK_WEBHOOK_URL is
not set, alerts are only logged — the bot keeps working either way.
"""
from __future__ import annotations

import json
import logging

import httpx

from .channels import InboundMessage, ProviderEvent
from .config import get_settings

logger = logging.getLogger("escalation")

HANDOFF_REPLY = (
    "Te paso con una persona del equipo — te responderán por aquí en breve. "
    "/ I'm handing you to a human, they'll reply here shortly."
)
HANDOFF_KEYWORDS = {"humano", "human", "agente", "persona", "asesor"}


def wants_human(text: str) -> bool:
    return text.strip().lower().strip("!.?¡¿") in HANDOFF_KEYWORDS


async def notify_slack(text: str, blocks: list | None = None) -> bool:
    s = get_settings()
    url = s.slack_webhook_url
    if not url:
        if s.slack_threads_enabled:  # no webhook: post top-level with the bot token
            from . import slack_bridge  # local import avoids a cycle
            return await slack_bridge.post_top_level(text)
        logger.warning("[slack disabled] %s", text)
        return False
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.error("Slack webhook failed (%s): %s", resp.status_code, resp.text)
            return False
        return True
    except httpx.HTTPError:
        logger.exception("Slack webhook error")
        return False


async def escalate(msg: InboundMessage, reason: str, transcript: str = "") -> None:
    who = f"{msg.profile_name or 'unknown'} ({msg.user_id})"
    history = f"*Recent transcript:*\n```{transcript[-2500:]}```\n" if transcript else ""
    text = (
        f":rotating_light: *Escalation* via `{msg.channel}` from {who}\n"
        f"*Reason:* {reason}\n*Message:* {msg.text or '(non-text)'}\n{history}"
        f"Reply with: `POST /admin/reply` "
        f"{{\"channel\": \"{msg.channel}\", \"to\": \"{msg.reply_to}\", \"text\": \"...\"}}"
    )
    await notify_slack(text)


async def alert_provider_event(event: ProviderEvent) -> None:
    emoji = {
        "account_review_update": ":memo:",
        "account_update": ":office:",
        "business_capability_update": ":chart_with_upwards_trend:",
        "message_failed": ":x:",
    }.get(event.kind, ":bell:")
    body = json.dumps(event.data, ensure_ascii=False, indent=2)[:2500]
    await notify_slack(f"{emoji} *{event.channel} event: {event.kind}*\n```{body}```")
