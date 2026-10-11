"""Lead scoring: explicit weights, explainable results.

    score = fit (0-40, from the profile) + intent (0-60, from lead_activity rows)
    tier  = hot >= 60 | warm 30-59 | cold < 30   (explicit meeting / implementation ask -> hot)

Every intent point is a ``lead_activity`` row (type = a key of ``WEIGHTS["intent"]``), so
"why is this lead hot?" is always answered by :func:`breakdown`. Intent decays: rows older
than 30 days count 50%, older than 60 days 0. Override the weights with ``LEAD_SCORING_JSON``
(same shape, deep-merged), e.g. ``{"intent": {"quiz_completed": {"points": 15}}}``.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger("leads.scoring")

WEIGHTS: dict[str, Any] = {
    "fit": {
        "role_level": {"owner_exec": 20, "director": 15, "manager": 8},
        "company_size": {"11-50": 15, "51-250": 15, "251+": 10, "1-10": 5},
        "identity_known": 5,           # company or name known
        "max": 40,
    },
    "intent": {
        # type: points per row, max rows that earn points (later rows are logged with 0)
        "handoff_business": {"points": 25, "max": 1},   # asked to talk to Alan about their own company
        "buying_signal": {"points": 20, "max": 2},      # flag_buying_signal
        "quiz_completed": {"points": 10, "max": 1},
        "quiz_need": {"points": 5, "max": 1},           # quiz level L0 or L1
        "report_requested": {"points": 5, "max": 1},
        "returning_visit": {"points": 10, "max": 3},    # new session on another day
        "pdf_requested": {"points": 3, "max": 2},       # topic PDF or slides
        "making_of": {"points": 3, "max": 1},           # on-request "making of" disclosed
        "contact_request": {"points": 0, "max": 1},     # tier override only (form confirmed)
        "max": 60,
    },
    "decay": {"half_after_days": 30, "zero_after_days": 60},
    "tiers": {"hot": 60, "warm": 30},
    # buying-signal kinds that make a lead hot whatever the score
    "hot_kinds": ["asked_meeting", "wants_help_implementing"],
}

BUYING_SIGNAL_KINDS = ["wants_help_implementing", "asked_services_or_price", "asked_meeting",
                       "timeline_or_budget_mentioned", "team_or_process_pain"]

LABELS = {
    "role_level": "rol", "company_size": "tamaño", "identity_known": "nombre/empresa",
    "handoff_business": "pidió hablar con Alan sobre su empresa", "buying_signal": "señal de compra",
    "quiz_completed": "terminó el diagnóstico", "quiz_need": "nivel L0/L1 (necesidad clara)",
    "report_requested": "pidió su reporte", "returning_visit": "regresó otro día",
    "pdf_requested": "pidió PDF/láminas", "making_of": "leyó el making-of",
    "contact_request": "pidió que lo contacten",
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


_cache: tuple[str, dict] | None = None


def weights() -> dict:
    """WEIGHTS with LEAD_SCORING_JSON applied (bad JSON is logged and ignored)."""
    global _cache
    from ..config import get_settings
    raw = get_settings().lead_scoring_json or ""
    if _cache and _cache[0] == raw:
        return _cache[1]
    w = copy.deepcopy(WEIGHTS)
    if raw.strip():
        try:
            _merge(w, json.loads(raw))
        except (ValueError, TypeError):
            logger.error("LEAD_SCORING_JSON is not valid JSON; using the defaults")
    _cache = (raw, w)
    return w


def fit_parts(lead: dict) -> list[tuple[str, int]]:
    w = weights()["fit"]
    parts = []
    if (p := w["role_level"].get(lead.get("role_level") or "")):
        parts.append(("role_level", p))
    if (p := w["company_size"].get(lead.get("company_size") or "")):
        parts.append(("company_size", p))
    if lead.get("company") or lead.get("name"):
        parts.append(("identity_known", w["identity_known"]))
    return parts


def fit_score(lead: dict) -> int:
    return min(weights()["fit"]["max"], sum(p for _, p in fit_parts(lead)))


def points_for(kind: str, earned_so_far: int) -> int:
    """Points a new activity of ``kind`` earns, given how many earning rows already exist."""
    spec = weights()["intent"].get(kind)
    if not isinstance(spec, dict):
        return 0
    return int(spec.get("points", 0)) if earned_so_far < int(spec.get("max", 1)) else 0


def decay_factor(age_seconds: float) -> float:
    d = weights()["decay"]
    days = age_seconds / 86400
    if days > d["zero_after_days"]:
        return 0.0
    if days > d["half_after_days"]:
        return 0.5
    return 1.0


def intent_parts(rows: Iterable[dict], now: Optional[float] = None) -> list[tuple[str, float]]:
    """(type, decayed points) per activity row that carries points."""
    t = time.time() if now is None else now
    kinds = {k for k, v in weights()["intent"].items() if isinstance(v, dict)}
    out = []
    for r in rows:
        if r.get("points") and r.get("type") in kinds:  # fit_change rows are audit only
            out.append((r["type"], r["points"] * decay_factor(t - r["ts"])))
    return out


def intent_score(rows: Iterable[dict], now: Optional[float] = None) -> int:
    total = sum(p for _, p in intent_parts(rows, now))
    return int(round(min(weights()["intent"]["max"], total)))


def tier_for(score: int, override: Optional[str] = None) -> str:
    if override == "hot":
        return "hot"
    t = weights()["tiers"]
    return "hot" if score >= t["hot"] else "warm" if score >= t["warm"] else "cold"


def breakdown(lead: dict, rows: list[dict], now: Optional[float] = None) -> list[tuple[str, int]]:
    """Reasons, biggest first: [(label, points)] (fit + decayed intent, grouped by type)."""
    agg: dict[str, float] = {}
    for k, p in fit_parts(lead):
        agg[k] = agg.get(k, 0) + p
    for k, p in intent_parts(rows, now):
        agg[k] = agg.get(k, 0) + p
    items = [(LABELS.get(k, k), int(round(v))) for k, v in agg.items() if v]
    if lead.get("tier_override") == "hot":
        items.append(("override: pidió reunión/ayuda o contacto", 0))
    return sorted(items, key=lambda x: -x[1])


def top_reasons(lead: dict, rows: list[dict], n: int = 3) -> str:
    parts = breakdown(lead, rows)[:n]
    return ", ".join(f"{label} (+{p})" if p else label for label, p in parts) or "sin señales"
