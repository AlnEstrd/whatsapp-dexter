"""Channel registry. A channel is available only if its credentials are set."""
from __future__ import annotations

from ..config import get_settings
from .base import Channel, InboundMessage, ParsedWebhook, ProviderEvent, normalize_user_id
from .errors import MediaError, SendError

_instances: dict[str, Channel] = {}


def get_channel(name: str) -> Channel | None:
    s = get_settings()
    if name == "twilio" and not s.twilio_enabled:
        return None
    if name == "meta" and not s.meta_enabled:
        return None
    if name not in _instances:
        if name == "twilio":
            from .twilio import TwilioChannel

            _instances[name] = TwilioChannel()
        elif name == "meta":
            from .meta import MetaChannel

            _instances[name] = MetaChannel()
        else:
            return None
    return _instances[name]


def get_default_channel() -> Channel | None:
    """Channel for bot-initiated messages, chosen by WA_PROVIDER."""
    return get_channel(get_settings().wa_provider)


def enabled_channels() -> list[str]:
    return [n for n in ("twilio", "meta") if get_channel(n) is not None]


def reset_channels() -> None:  # used by tests
    _instances.clear()


__all__ = [
    "Channel", "InboundMessage", "ParsedWebhook", "ProviderEvent", "SendError", "MediaError",
    "normalize_user_id", "get_channel", "get_default_channel", "enabled_channels",
    "reset_channels",
]
