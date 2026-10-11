"""Human handoff routing: Twilio Flex (agent desktop) or Slack.

Flex flow (Twilio-channel users only):
  1. Create a Twilio Conversation and add the WhatsApp user as a participant
     (address = user, proxy = our Twilio WhatsApp number). While that
     Conversation is *active*, Twilio routes the user's inbound messages into
     it -> to the Flex agent, instead of to /webhooks/twilio.
  2. Create a Flex Interaction pointing at that Conversation, so TaskRouter
     creates a task for an agent. Reason + bot transcript go in task attributes.
  3. When the agent wraps up (or the Closed timer fires) the Conversation
     closes and messages flow to the bot webhook again -> we auto-release.

Any Flex failure falls back to Slack, so a human is always notified.
"""
from __future__ import annotations

import json
import logging
import re

from . import slack_bridge, store
from .analytics import events
from .channels import InboundMessage
from .config import get_settings
from .escalation import escalate, notify_slack
from .twilio_api import CONVERSATIONS_API, FLEX_API, TwilioAPIError, twilio_post

logger = logging.getLogger("handoff")

BINDING_EXISTS = 50416  # participant already bound to this proxy in another active conversation
_CH_SID = re.compile(r"CH[0-9a-fA-F]{32}")


def _transcript_text(user_id: str) -> str:
    return "\n".join(f"{role}: {text}" for role, text in store.transcript(user_id))


async def start_flex_handoff(msg: InboundMessage, reason: str) -> str:
    """Returns the Conversation SID the Flex task is attached to."""
    s = get_settings()
    proxy = s.twilio_whatsapp_from
    if not proxy.startswith("whatsapp:"):
        proxy = f"whatsapp:{proxy}"

    conv = await twilio_post(
        f"{CONVERSATIONS_API}/Conversations",
        {
            "FriendlyName": f"WhatsApp {msg.profile_name or msg.user_id}",
            "Attributes": json.dumps({"source": "whatsapp-agent-poc", "userId": msg.user_id}),
            "Timers.Closed": s.flex_conversation_close_timer,
        },
    )
    conv_sid = conv["sid"]

    try:
        await twilio_post(
            f"{CONVERSATIONS_API}/Conversations/{conv_sid}/Participants",
            {"MessagingBinding.Address": msg.reply_to, "MessagingBinding.ProxyAddress": proxy},
        )
    except TwilioAPIError as exc:
        existing = _CH_SID.search(exc.message or "")
        if exc.code != BINDING_EXISTS or not existing:
            raise
        # User is already in an active conversation (e.g. a stale handoff):
        # reuse it and close the one we just created.
        logger.info("Reusing existing conversation %s for %s", existing.group(0), msg.user_id)
        try:
            await twilio_post(f"{CONVERSATIONS_API}/Conversations/{conv_sid}", {"State": "closed"})
        except TwilioAPIError:
            logger.warning("Could not close orphan conversation %s", conv_sid)
        conv_sid = existing.group(0)

    routing: dict = {
        "workspace_sid": s.flex_workspace_sid,
        "task_channel_unique_name": s.flex_task_channel,
        "attributes": {
            "name": msg.profile_name or msg.user_id,
            "customerName": msg.profile_name or msg.user_id,
            "customerAddress": msg.reply_to,
            "from": msg.reply_to,
            "channelType": "whatsapp",
            "userId": msg.user_id,
            "handoffReason": reason,
            "botTranscript": _transcript_text(msg.user_id),
        },
    }
    if s.flex_workflow_sid:
        routing["workflow_sid"] = s.flex_workflow_sid

    await twilio_post(
        f"{FLEX_API}/Interactions",
        {
            "Channel": json.dumps({
                "type": "whatsapp",
                "initiated_by": "customer",
                "properties": {"media_channel_sid": conv_sid},
            }),
            "Routing": json.dumps({"properties": routing}),
        },
    )
    return conv_sid


# Handoff reasons for the metrics (the free-text ``reason`` still goes to Alan).
REASONS = ("user_asked", "agent_error", "offtopic", "delivery_failure", "limit", "other")


async def hand_off(msg: InboundMessage, reason: str, kind: str = "other") -> str:
    """Route the user to a human. Returns the target used: "flex" or "slack".
    ``kind``: one of REASONS, recorded as the ``handoff`` event's reason."""
    events.record("handoff", msg.user_id, step="handoff", reason=kind if kind in REASONS else "other")
    from . import on_request
    reason = on_request.lead_note(msg.user_id) + reason  # Alan sees what a lead read first
    s = get_settings()
    if s.flex_enabled and msg.channel == "twilio":
        try:
            conv_sid = await start_flex_handoff(msg, reason)
            store.start_handoff(msg.user_id, "flex")
            await notify_slack(
                f":headphones: Routed {msg.profile_name or msg.user_id} to *Flex* "
                f"(conversation `{conv_sid}`). Reason: {reason}"
            )
            return "flex"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flex handoff failed for %s; falling back to Slack", msg.user_id)
            reason = f"{reason} (Flex handoff failed: {exc})"

    store.start_handoff(msg.user_id, "slack")
    # Slack app configured -> threaded escalation with an owner mention;
    # otherwise (or if Slack fails) the legacy Incoming Webhook.
    if not (slack_bridge.enabled() and await slack_bridge.escalate_in_thread(msg, reason)):
        await escalate(msg, reason, transcript=_transcript_text(msg.user_id))
    return "slack"
