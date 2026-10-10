"""Small async retry helper with exponential backoff + jitter."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("resilience")
T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    is_retryable: Callable[[Exception], bool] = lambda e: True,
    label: str = "op",
) -> T:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == attempts or not is_retryable(exc):
                break
            delay = min(max_delay, base_delay * 2 ** (i - 1)) * (0.5 + random.random())
            logger.warning("%s failed (attempt %d/%d): %s — retrying in %.1fs", label, i, attempts, exc, delay)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
