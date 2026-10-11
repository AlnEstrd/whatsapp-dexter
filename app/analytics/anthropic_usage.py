"""Optional reconciliation with Anthropic's own numbers (Usage & Cost Admin API).

Only when ANTHROPIC_ADMIN_KEY (an Admin API key, ``sk-ant-admin...``) is set. Server-side
calls to (docs: platform.claude.com/docs/en/manage-claude/usage-cost-api, checked 2026-10-11):

    GET /v1/organizations/usage_report/messages   starting_at, ending_at (RFC 3339 UTC),
        bucket_width=1d, group_by[]=model, limit (≤31 daily buckets), page / has_more / next_page
        -> results[]: uncached_input_tokens, cache_read_input_tokens,
           cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}, output_tokens, model
    GET /v1/organizations/cost_report              starting_at, ending_at, group_by[]=description
        -> results[]: amount (USD, decimal string in cents), currency, model, ...

Headers: ``x-api-key`` + ``anthropic-version: 2023-06-01``. The key is sent only to
api.anthropic.com and is never logged. Results are cached for 15 minutes.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Optional

import httpx

from ..config import get_settings
from . import pricing

logger = logging.getLogger("anthropic_usage")

API = "https://api.anthropic.com/v1/organizations"
CACHE_SECONDS = 15 * 60
_cache: dict[tuple, tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(get_settings().anthropic_admin_key)


def _get(client: httpx.Client, path: str, params: dict) -> list[dict]:
    """All pages of one report (``data`` buckets)."""
    out: list[dict] = []
    page: Optional[str] = None
    for _ in range(20):
        q = dict(params, **({"page": page} if page else {}))
        resp = client.get(f"{API}/{path}", params=q)
        if resp.status_code >= 400:
            # Never echo the request (it carries the key in a header; the body is safe to summarize).
            raise RuntimeError(f"{path} returned {resp.status_code}")
        body = resp.json()
        out += body.get("data") or []
        if not body.get("has_more"):
            break
        page = body.get("next_page")
    return out


def report(first: date, last: date) -> dict[str, Any]:
    """Anthropic-reported usage and cost for the UTC days first..last (inclusive), by model."""
    key = (first, last)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    s = get_settings()
    start = f"{first.isoformat()}T00:00:00Z"
    end = f"{(last + timedelta(days=1)).isoformat()}T00:00:00Z"
    headers = {"x-api-key": s.anthropic_admin_key or "", "anthropic-version": "2023-06-01",
               "User-Agent": "dexter-whatsapp-bot/1.0"}
    with httpx.Client(timeout=20, headers=headers) as client:
        usage = _get(client, "usage_report/messages",
                     {"starting_at": start, "ending_at": end, "bucket_width": "1d", "group_by[]": "model",
                      "limit": 31})
        try:
            cost_buckets = _get(client, "cost_report", {"starting_at": start, "ending_at": end,
                                                        "group_by[]": "description", "limit": 31})
        except Exception as exc:  # noqa: BLE001  (usage alone still reconciles via our prices)
            logger.warning("cost_report unavailable: %s", exc.__class__.__name__)
            cost_buckets = None

    by_model: dict[str, dict[str, int]] = {}
    for bucket in usage:
        for r in bucket.get("results") or []:
            m = pricing.normalize(r.get("model") or "?")
            row = by_model.setdefault(m, {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                          "cache_write_tokens": 0})
            cc = r.get("cache_creation") or {}
            row["input_tokens"] += int(r.get("uncached_input_tokens") or 0)
            row["output_tokens"] += int(r.get("output_tokens") or 0)
            row["cache_read_tokens"] += int(r.get("cache_read_input_tokens") or 0)
            row["cache_write_tokens"] += int(cc.get("ephemeral_5m_input_tokens") or 0) + \
                int(cc.get("ephemeral_1h_input_tokens") or 0)
    from_tokens = sum(pricing.cost(m, **t) for m, t in by_model.items())
    out: dict[str, Any] = {"by_model": by_model, "cost_from_tokens_usd": round(from_tokens, 6),
                           "cost_usd": None, "cost_by_model_usd": {}}
    if cost_buckets is not None:
        total_cents = 0.0
        for bucket in cost_buckets:
            for r in bucket.get("results") or []:
                cents = float(r.get("amount") or 0)
                total_cents += cents
                m = pricing.normalize(r.get("model") or r.get("description") or "?")
                out["cost_by_model_usd"][m] = round(out["cost_by_model_usd"].get(m, 0) + cents / 100, 6)
        out["cost_usd"] = round(total_cents / 100, 6)
    _cache[key] = (time.time(), out)
    return out


def reset() -> None:  # tests
    _cache.clear()
