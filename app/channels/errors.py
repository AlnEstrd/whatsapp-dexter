from __future__ import annotations


class SendError(Exception):
    """Provider rejected or failed a send. `retryable` drives the retry policy."""

    def __init__(self, provider: str, status: int, detail: str) -> None:
        super().__init__(f"{provider} send failed ({status}): {detail[:500]}")
        self.provider = provider
        self.status = status
        self.retryable = status == 429 or status >= 500 or status == 0


class MediaError(Exception):
    """Could not fetch an inbound media file. ``reason``: too_large | fetch_failed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"media {reason}: {detail[:300]}")
        self.reason = reason
