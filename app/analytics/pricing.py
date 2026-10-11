"""Model prices in USD per million tokens, used at query time (never stored with a call).

Defaults checked against https://platform.claude.com/docs/en/about-claude/pricing
as of 2026-10-11 (5-minute cache writes; 1-hour writes are not used by Dexter):

    claude-sonnet-4-5   input $3     output $15   cache write $3.75   cache read $0.30
    claude-haiku-4-5    input $1     output $5    cache write $1.25   cache read $0.10

Override or add models with MODEL_PRICES_JSON, e.g.
    {"claude-sonnet-4-5": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3}}
The table is copied into SQLite (``model_prices``) on startup so the read-only SQL views
can price history too; a fix here re-prices every past call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .. import db
from ..config import get_settings

logger = logging.getLogger("pricing")

FIELDS = ("input", "output", "cache_write", "cache_read")

DEFAULTS: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize(model: Optional[str]) -> str:
    """'claude-sonnet-4-5-20250929' -> 'claude-sonnet-4-5' (responses carry the dated id)."""
    return _DATE_SUFFIX.sub("", (model or "").strip())


def prices() -> dict[str, dict[str, float]]:
    out = {m: dict(p) for m, p in DEFAULTS.items()}
    raw = get_settings().model_prices_json
    if raw:
        try:
            for model, p in json.loads(raw).items():
                row = {**out.get(normalize(model), {f: 0.0 for f in FIELDS})}
                row.update({f: float(p[f]) for f in FIELDS if f in p})
                out[normalize(model)] = row
        except (ValueError, TypeError, AttributeError):
            logger.error("MODEL_PRICES_JSON is not valid JSON {model: {input, output, ...}}; using defaults")
    return out


def price_for(model: Optional[str]) -> Optional[dict[str, float]]:
    return prices().get(normalize(model))


def cost(model: Optional[str], input_tokens: int = 0, output_tokens: int = 0,
         cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """USD for one call. Unknown model -> 0.0 (and listed as unpriced by the metrics)."""
    p = price_for(model)
    if not p:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_write_tokens * p["cache_write"] + cache_read_tokens * p["cache_read"]) / 1_000_000


def sync_table() -> None:
    """Rewrite ``model_prices`` from the current table (startup)."""
    rows = [(m, *(p[f] for f in FIELDS)) for m, p in prices().items()]

    def _write():
        with db.transaction() as c:
            c.execute("DELETE FROM model_prices")
            c.executemany("INSERT INTO model_prices (model, input, output, cache_write, cache_read) "
                          "VALUES (?, ?, ?, ?, ?)", rows)
    db.safe("model prices", _write)
