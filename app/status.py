"""Dexter's live status, for the on-request "making of" entry ({{status_line}} / {{status}}).

Built from facts the process can check, never from hand-written claims:

- version: ``app/BUILD_INFO.json`` written by deploy/remote-deploy.sh before the image
  build (commit, build time, passing tests from CI, eval cases, ci|manual);
- uptime: when this process started;
- features: on/off from the actual configuration;
- Meta: the last account/verification webhook events (stored by ``record_meta_event``),
  with META_VERIFICATION_STATUS as a fallback;
- counters: conversations, assessments and curious people since the counters began.

Every field is optional and fail-soft: an error drops that field; a total failure
renders a one-line "unavailable". Never includes IPs, phone numbers, Meta/WABA ids,
user data, secrets or raw errors.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import db
from .config import get_settings

logger = logging.getLogger("status")

STARTED_AT = time.time()
BUILD_INFO = Path(__file__).parent / "BUILD_INFO.json"
MX = dt.timezone(dt.timedelta(hours=-6))  # Mexico City, no DST since 2022

UNAVAILABLE = {"es": "Estado no disponible en este momento.", "en": "Status unavailable right now."}

# Meta webhook fields worth remembering (latest value of each).
META_KINDS = {"account_review_update", "account_update", "business_capability_update",
              "phone_number_name_update"}
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_ .-]{1,40}$")

_memory_kv: dict[str, Any] = {}  # fallback when the DB is off


# --------------------------------------------------------------------------- #
# Small key/value store (SQLite ``kv`` table, memory fallback)
# --------------------------------------------------------------------------- #
def _kv_get(key: str) -> Any:
    if db.enabled():
        rows = db.safe("kv get", lambda: db.query("SELECT value_json FROM kv WHERE key = ?", (key,)), default=None)
        if rows:
            return json.loads(rows[0]["value_json"])
        if rows is not None:
            return None
    return _memory_kv.get(key)


def _kv_set(key: str, value: Any) -> None:
    _memory_kv[key] = value
    if db.enabled():
        db.safe("kv set", lambda: db.execute(
            "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, db.dumps(value), db.now())))


def bump(counter: str) -> None:
    """Add one to a public counter (conversations, assessments, curious). Counts only."""
    try:
        key = f"count:{counter}"
        if db.enabled():
            ok = db.safe("kv bump", lambda: db.execute(
                "INSERT INTO kv (key, value_json, updated_at) VALUES (?, '1', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = CAST(CAST(value_json AS INTEGER) + 1 AS TEXT), "
                "updated_at = excluded.updated_at", (key, db.now())))
            if ok is not None:
                return
        _memory_kv[key] = int(_memory_kv.get(key) or 0) + 1
    except Exception:  # noqa: BLE001
        logger.exception("counter %s failed", counter)


def count(counter: str) -> Optional[int]:
    v = _kv_get(f"count:{counter}")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def reset() -> None:  # tests
    _memory_kv.clear()


# --------------------------------------------------------------------------- #
# Meta events
# --------------------------------------------------------------------------- #
def _safe(value: Any) -> Optional[str]:
    """Only short enum-like words survive (APPROVED, TIER_1K, 250...). Names/ids never do."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if 0 <= value < 10_000_000 else None
    if isinstance(value, str) and _SAFE_WORD.match(value.strip()):
        return value.strip()
    return None


def record_meta_event(kind: str, data: dict) -> None:
    """Keep the latest verification-related value of each Meta account event."""
    if kind not in META_KINDS or not isinstance(data, dict):
        return
    keep: dict[str, str] = {}
    if kind in ("account_review_update", "phone_number_name_update"):
        if (d := _safe(data.get("decision"))):
            keep["decision"] = d.upper()
    elif kind == "account_update":
        if (e := _safe(data.get("event"))):
            keep["event"] = e.upper()
    elif kind == "business_capability_update":
        for k in ("max_daily_conversations_per_business", "max_daily_conversation_per_phone",
                  "max_phone_numbers_per_business"):
            if (v := _safe(data.get(k))):
                keep[k] = v
    if keep:
        keep["at"] = str(int(time.time()))
        _kv_set(f"meta:{kind}", keep)


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def build_info() -> dict:
    try:
        data = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  (missing in local runs and tests)
        return {}


def _try(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("status field failed")
        return default


def features() -> list[tuple[str, bool]]:
    """(feature key, live?) from the actual configuration."""
    s = get_settings()
    info = build_info()
    evals = info.get("eval_cases")
    return [
        ("talk", s.flows_enabled),
        ("quiz", s.flows_enabled),
        ("slides", s.flows_enabled),
        ("files", s.meta_enabled),
        ("slack", s.slack_threads_enabled),
        ("close", s.close_on_intent),
        ("guard", bool(s.guard_enabled)),
        ("persistence", db.enabled()),
        ("dedupe", True),
        ("limits", s.limits_enabled),
        ("evals", isinstance(evals, int) and evals > 0),
        ("cicd", info.get("source") == "ci"),
        ("on_request", s.on_request_enabled),
    ]


def snapshot() -> dict:
    info = build_info()
    snap: dict[str, Any] = {"features": _try(features, [])}
    sha = info.get("sha")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,40}", sha):
        snap["sha"] = sha[:7]
    built = info.get("built_at")
    if isinstance(built, str):
        snap["built_at"] = _try(lambda: dt.datetime.fromisoformat(built.replace("Z", "+00:00")))
    for k in ("tests_passed", "eval_cases"):
        if isinstance(info.get(k), int) and info[k] > 0:
            snap[k] = info[k]
    snap["uptime_s"] = max(0, int(time.time() - STARTED_AT))
    snap["meta"] = _try(_meta_state, {})
    snap["counts"] = {c: n for c in ("conversations", "assessments", "curious")
                      if (n := _try(lambda c=c: count(c))) is not None}
    return snap


def _meta_state() -> dict:
    out: dict[str, str] = {}
    acct = _kv_get("meta:account_update") or {}
    review = _kv_get("meta:account_review_update") or {}
    if acct.get("event") == "VERIFIED_ACCOUNT":
        out["verification"] = "verified"
    elif review.get("decision"):
        out["verification"] = {"APPROVED": "approved", "REJECTED": "rejected"}.get(review["decision"], "review")
    name = (_kv_get("meta:phone_number_name_update") or {}).get("decision")
    if name:
        out["display_name"] = {"APPROVED": "approved", "REJECTED": "rejected"}.get(name, "review")
    cap = _kv_get("meta:business_capability_update") or {}
    tier = cap.get("max_daily_conversations_per_business") or cap.get("max_daily_conversation_per_phone")
    if tier:
        out["tier"] = tier
    if "verification" not in out:
        fallback = (get_settings().meta_verification_status or "").strip()
        if fallback and len(fallback) <= 60 and not re.search(r"\d{6,}", fallback):
            out["verification_text"] = fallback
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
FEATURE_LABELS = {
    "talk": ("explorar la plática", "explore the talk"),
    "quiz": ("diagnóstico + reporte PDF", "assessment + PDF report"),
    "slides": ("láminas", "slides"),
    "files": ("leer fotos y PDFs", "read photos and PDFs"),
    "slack": ("hilos en Slack con Alan", "Slack threads with Alan"),
    "close": ("cierre por intención", "close on intent"),
    "guard": ("filtro de temas", "off-topic guard"),
    "persistence": ("memoria persistente (SQLite)", "persistent memory (SQLite)"),
    "dedupe": ("webhooks sin duplicados", "deduplicated webhooks"),
    "limits": ("topes de uso", "usage limits"),
    "evals": ("evals", "evals"),
    "cicd": ("CI/CD", "CI/CD"),
    "on_request": ("info para curiosos", "info for the curious"),
}

MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _date(d: dt.datetime, lang: str) -> str:
    d = d.astimezone(MX) if d.tzinfo else d
    return f"{d.day} de {MONTHS_ES[d.month - 1]}" if lang == "es" else f"{MONTHS_EN[d.month - 1]} {d.day}"


def _ago(seconds: int, lang: str) -> str:
    m = seconds // 60
    if m < 60:
        n, es, en = max(1, m), "min", "min"
    elif m < 48 * 60:
        n, es, en = m // 60, "h", "h"
    else:
        n = m // (24 * 60)
        es, en = ("día" if n == 1 else "días"), ("day" if n == 1 else "days")
    return f"{n} {es}" if lang == "es" else f"{n} {en}"


def unavailable(lang: str) -> str:
    return UNAVAILABLE["en" if lang == "en" else "es"]


def _meta_line(meta: dict, lang: str) -> str:
    es = lang != "en"
    parts = []
    v = meta.get("verification")
    if v:
        parts.append({"verified": ("negocio verificado", "business verified"),
                      "approved": ("cuenta aprobada", "account approved"),
                      "rejected": ("verificación rechazada (en corrección)", "verification rejected (being fixed)"),
                      "review": ("verificación en revisión", "verification under review")}[v][0 if es else 1])
    elif meta.get("verification_text"):
        parts.append(("verificación: " if es else "verification: ") + meta["verification_text"])
    if meta.get("display_name"):
        parts.append(("nombre visible " if es else "display name ") + {
            "approved": ("aprobado", "approved"), "rejected": ("rechazado", "rejected"),
            "review": ("en revisión", "under review")}[meta["display_name"]][0 if es else 1])
    if meta.get("tier"):
        parts.append(("límite de mensajería " if es else "messaging limit ") + meta["tier"])
    return ", ".join(parts)


def render_line(lang: str = "es") -> str:
    """One sentence: version, deploy date, uptime."""
    try:
        s = snapshot()
        es = lang != "en"
        bits = []
        if s.get("sha"):
            bits.append(f"versión {s['sha']}" if es else f"version {s['sha']}")
            if s.get("built_at"):
                bits[-1] += (f", desplegada el {_date(s['built_at'], lang)}" if es
                             else f", deployed {_date(s['built_at'], lang)}")
        bits.append((f"en línea desde hace {_ago(s['uptime_s'], lang)}" if es
                     else f"online for {_ago(s['uptime_s'], lang)}"))
        if s.get("tests_passed"):
            bits.append(f"{s['tests_passed']} pruebas en verde" if es else f"{s['tests_passed']} tests passing")
        text = ", ".join(bits)
        return ("🟢 Estado ahora: " if es else "🟢 Status right now: ") + text + "."
    except Exception:  # noqa: BLE001
        logger.exception("status line failed")
        return unavailable(lang)


def render_block(lang: str = "es") -> str:
    """A short bullet list for the agent context and the PDF."""
    try:
        s = snapshot()
        es = lang != "en"
        lines = [f"- {render_line(lang).split(': ', 1)[-1]}"]
        if s.get("eval_cases"):
            lines.append(f"- {s['eval_cases']} casos de evaluación" if es else f"- {s['eval_cases']} eval cases")
        meta = _meta_line(s.get("meta") or {}, lang)
        if meta:
            lines.append(f"- Meta: {meta}")
        on = [FEATURE_LABELS[k][0 if es else 1] for k, live in s.get("features", []) if live]
        off = [FEATURE_LABELS[k][0 if es else 1] for k, live in s.get("features", []) if not live]
        if on:
            lines.append(("- ✅ Activo: " if es else "- ✅ Live: ") + ", ".join(on))
        if off:
            lines.append(("- ⏳ Apagado o pendiente: " if es else "- ⏳ Off or pending: ") + ", ".join(off))
        c = s.get("counts") or {}
        names = {"conversations": ("conversaciones", "conversations"),
                 "assessments": ("diagnósticos completados", "assessments completed"),
                 "curious": ("curiosos que encontraron esta puerta", "curious people who found this door")}
        nums = [f"{c[k]} {names[k][0 if es else 1]}" for k in ("conversations", "assessments", "curious") if c.get(k)]
        if nums:
            lines.append(("- Desde que empezamos a contar: " if es else "- Since we started counting: ")
                         + ", ".join(nums))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        logger.exception("status block failed")
        return unavailable(lang)
