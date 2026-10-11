"""Zoho CRM sync (Free edition): one-way push from Dexter + a daily status pull.

Dexter's SQLite is the system of record; Zoho is Alan's sales workspace. Designed for the
Free edition: no workflow rules (all logic stays here), 5,000 API credits/day, 5 concurrent
calls per org (we use at most 2).

- Auth: OAuth Self Client refresh token -> access token, cached in memory, refreshed on 401.
  Tokens are never logged.
- Outbox (``zoho_outbox``): changes enqueue an ``upsert`` (debounced 60 s per lead), a
  ``note`` or a ``delete``; the worker sends them with exponential backoff and stops for the
  day at 80% of ZOHO_DAILY_CREDIT_BUDGET (one Slack alert).
- Upsert: by ``zoho_id`` once known; the first time, a COQL lookup on ``Mobile`` (or
  ``Email``) avoids duplicates (``Mobile`` isn't a unique field in a stock Leads layout,
  so the upsert API's duplicate check can't use it), then an insert.
- Pull: once a day, a COQL query for Dexter-sourced leads modified since the last pull maps
  ``Lead_Status`` back to the stage (Zoho wins for stages >= contacted).
- ZOHO_ENABLED=false (default): nothing here makes a call.

Backfill: ``python -m app.leads.zoho backfill [--dry-run]``.
REST API v8 (ZOHO_API_VERSION): /Leads, /Leads/{id}, /Leads/{id}/Notes, /coql,
/settings/fields?module=Leads, /Leads/actions/remove_tags.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
from typing import Any, Optional

import httpx

from .. import db
from ..config import get_settings
from . import core, scoring

logger = logging.getLogger("leads.zoho")

DEBOUNCE_SECONDS = 60
MAX_CONCURRENT = 2
MAX_ATTEMPTS = 8
BUDGET_STOP = 0.8
CREDITS = {"GET": 1, "POST": 1, "PUT": 1, "DELETE": 1, "COQL": 2, "TOKEN": 0}
TIERS = ("hot", "warm", "cold")

STATUS_FOR_STAGE = {
    "new": "Not Contacted", "engaged": "Not Contacted",
    "mql": "Pre-Qualified", "sql": "Pre-Qualified",
    "contacted": "Contacted", "meeting": "Contacted", "proposal": "Contacted", "won": "Contacted",
    "nurture": "Contact in Future", "lost": "Lost Lead", "opted_out": "Not Qualified",
}
STAGE_FOR_STATUS = {"Contacted": "contacted", "Lost Lead": "lost", "Contact in Future": "nurture",
                    "Junk Lead": "lost", "Not Qualified": "lost"}
CUSTOM_FIELDS = ("Dexter_Score", "Dexter_Tier", "Dexter_Quiz_Level", "Dexter_Consent_At", "Dexter_Lead_ID")
SIZE_MIDPOINT = {"1-10": 5, "11-50": 30, "51-250": 150, "251+": 300}
ROLE_DESIGNATION = {"owner_exec": "Dueño(a) / Dirección general", "director": "Dirección de área",
                    "manager": "Gerencia"}


class ZohoError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail[:300]}")
        self.kind = kind


class BudgetStop(Exception):
    pass


_token: Optional[str] = None
_fields: Optional[set[str]] = None
_sem: Optional[asyncio.Semaphore] = None


def enabled() -> bool:
    return get_settings().zoho_active


def reset() -> None:  # tests
    global _token, _fields, _sem
    _token, _fields, _sem = None, None, None


def _base() -> str:
    s = get_settings()
    return f"{s.zoho_api_domain.rstrip('/')}/crm/{s.zoho_api_version}"


# --------------------------------------------------------------------------- #
# Credits
# --------------------------------------------------------------------------- #
def _day() -> str:
    return core.mx_day(core.now())


def credits_used() -> int:
    rows = db.query("SELECT value_json FROM kv WHERE key = ?", (f"zoho_credits:{_day()}",))
    return int(json.loads(rows[0]["value_json"])) if rows else 0


def _spend(n: int) -> None:
    key = f"zoho_credits:{_day()}"
    db.execute("""INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)
                  ON CONFLICT(key) DO UPDATE SET value_json = CAST(CAST(value_json AS INTEGER) + ? AS TEXT),
                  updated_at = excluded.updated_at""", (key, str(n), core.now(), n))


def budget_left() -> bool:
    return credits_used() < BUDGET_STOP * get_settings().zoho_daily_credit_budget


async def _budget_alert() -> None:
    if db.safe("flag", lambda: db.set_flag_once(f"zoho_budget:{_day()}"), default=False):
        from . import alerts
        await alerts.post(f"⚠️ {alerts._owner()} Zoho sync paused until tomorrow: {credits_used()} of "
                          f"{get_settings().zoho_daily_credit_budget} API credits used (stop at 80%). "
                          "Leads keep their changes in Dexter and sync tomorrow.")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def _refresh_token() -> str:
    global _token
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{s.zoho_accounts_url.rstrip('/')}/oauth/v2/token", params={
                "refresh_token": s.zoho_refresh_token, "client_id": s.zoho_client_id,
                "client_secret": s.zoho_client_secret, "grant_type": "refresh_token"})
    except httpx.HTTPError as exc:
        raise ZohoError("network", repr(exc)) from exc
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("access_token"):
        raise ZohoError("auth", f"token refresh failed ({resp.status_code}: {data.get('error', '')})")
    _token = data["access_token"]
    return _token


async def request(method: str, path: str, *, body: Optional[dict] = None, params: Optional[dict] = None,
                  cost: Optional[int] = None) -> dict:
    """One API call, with the credit budget and one token refresh on 401."""
    cost = CREDITS.get(method, 1) if cost is None else cost
    if not budget_left():
        await _budget_alert()
        raise BudgetStop()
    token = _token or await _refresh_token()
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(method, f"{_base()}{path}", json=body, params=params,
                                            headers={"Authorization": f"Zoho-oauthtoken {token}"})
        except httpx.HTTPError as exc:
            raise ZohoError("network", repr(exc)) from exc
        db.safe("zoho credits", lambda: _spend(cost))
        if resp.status_code == 401 and attempt == 1:
            token = await _refresh_token()
            continue
        break
    if resp.status_code == 204:
        return {}
    data = resp.json() if resp.content else {}
    if resp.status_code == 401:
        raise ZohoError("auth", "unauthorized after refresh")
    if resp.status_code == 429:
        raise ZohoError("rate_limit", str(data)[:200])
    if resp.status_code >= 500:
        raise ZohoError("server", f"{resp.status_code}")
    if resp.status_code >= 400:
        raise ZohoError("client", f"{resp.status_code}: {str(data)[:300]}")
    return data


async def check_fields() -> set[str]:
    """Field API names of Leads; logs the Dexter custom fields that are missing."""
    global _fields
    data = await request("GET", "/settings/fields", params={"module": "Leads"})
    _fields = {f.get("api_name") for f in data.get("fields", []) if f.get("api_name")}
    missing = [f for f in CUSTOM_FIELDS if f not in _fields]
    for std in ("Last_Name", "Mobile", "Lead_Status", "Description", "Email_Opt_Out"):
        if std not in _fields:
            logger.warning("Zoho Leads has no %s field", std)
    if missing:
        logger.info("Zoho custom fields not found (the data goes in Description): %s", ", ".join(missing))
    return _fields


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def lead_source(lead: dict) -> str:
    s = get_settings()
    return s.zoho_lead_source_web if lead.get("source") == "web_form" else s.zoho_lead_source_whatsapp


def tags(lead: dict) -> list[str]:
    out = ["dexter", lead.get("tier") or "cold"]
    if lead.get("stage") in ("meeting", "proposal", "won"):
        out.append(lead["stage"])
    if lead.get("stage") == "opted_out" or lead.get("opted_out_at"):
        out.append("opted_out")
    return out


def description(lead: dict) -> str:
    from .alerts import domain_names
    rows = core.activity_rows(lead["lead_id"])
    lines = [f"Dexter lead {lead['lead_id']} · tier {lead['tier']} · score {lead['score']} "
             f"(fit {lead['fit_score']} + intent {lead['intent_score']})",
             "Razones: " + scoring.top_reasons(lead, rows)]
    if lead.get("quiz_level") is not None:
        lines.append(f"Diagnóstico: L{lead['quiz_level']} · débiles: {domain_names(lead.get('weakest_domains') or [])}")
    if lead.get("industry"):
        lines.append(f"Industria: {lead['industry']}")
    if lead.get("interest_note"):
        lines.append(f"Interés: {lead['interest_note']}")
    if lead.get("consent_ts"):
        ts = dt.datetime.fromtimestamp(lead["consent_ts"], core.MX).isoformat(timespec="minutes")
        lines.append(f"Consentimiento: {ts} (v {lead.get('consent_text_version')})")
    if lead.get("opted_out_at"):
        lines.append("BAJA: no contactar.")
    return "\n".join(lines)


def record(lead: dict, fields: Optional[set[str]] = None) -> dict:
    fields = _fields if fields is None else fields
    full = (lead.get("name") or lead.get("wa_profile_name") or "").strip()
    first, last = None, full
    if " " in full:
        first, last = full.split(" ", 1)
    if not last:
        last = f"WhatsApp ••{(lead.get('user_id') or '')[-4:]}" if lead.get("user_id") else "Dexter lead"
    rec: dict[str, Any] = {
        "Last_Name": last[:80], "First_Name": first, "Company": lead.get("company") or "—",
        "Mobile": f"+{lead['user_id']}" if lead.get("user_id") else None,
        "Email": lead.get("email"),
        "Designation": lead.get("role") or ROLE_DESIGNATION.get(lead.get("role_level") or ""),
        "No_of_Employees": SIZE_MIDPOINT.get(lead.get("company_size") or ""),
        "Lead_Source": lead_source(lead),
        "Lead_Status": STATUS_FOR_STAGE.get(lead.get("stage") or "new", "Not Contacted"),
        "Description": description(lead),
        "Email_Opt_Out": bool(lead.get("opted_out_at")),
        "Tag": [{"name": t} for t in tags(lead)],
    }
    if fields:
        custom = {"Dexter_Score": lead.get("score"), "Dexter_Tier": lead.get("tier"),
                  "Dexter_Quiz_Level": lead.get("quiz_level"), "Dexter_Lead_ID": lead["lead_id"],
                  "Dexter_Consent_At": dt.datetime.fromtimestamp(lead["consent_ts"], core.MX).isoformat(timespec="seconds")
                  if lead.get("consent_ts") else None}
        rec.update({k: v for k, v in custom.items() if k in fields})
    return {k: v for k, v in rec.items() if v is not None}


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #
def enqueue(lead_id: str, op: str, payload: Optional[dict] = None) -> None:
    """Queue a change. ``upsert`` is debounced: one pending row per lead, pushed 60 s out."""
    t = core.now()
    if op == "upsert":
        rows = db.query("SELECT id FROM zoho_outbox WHERE lead_id = ? AND op = 'upsert' AND attempts = 0",
                        (lead_id,))
        if rows:
            db.execute("UPDATE zoho_outbox SET next_attempt_at = ? WHERE id = ?", (t + DEBOUNCE_SECONDS, rows[0]["id"]))
            return
        db.execute("INSERT INTO zoho_outbox (lead_id, op, payload_json, next_attempt_at) VALUES (?, 'upsert', '{}', ?)",
                   (lead_id, t + DEBOUNCE_SECONDS))
        return
    db.execute("INSERT INTO zoho_outbox (lead_id, op, payload_json, next_attempt_at) VALUES (?, ?, ?, ?)",
               (lead_id, op, db.dumps(payload or {}), t if op == "delete" else t + DEBOUNCE_SECONDS + 5))


def _backoff(attempts: int) -> float:
    return min(6 * 3600, 30 * 2 ** attempts)


async def _find_existing(lead: dict) -> Optional[str]:
    if lead.get("user_id"):
        where = f"Mobile = '+{lead['user_id']}'"
    elif lead.get("email"):
        where = "Email = '" + lead["email"].replace("'", "") + "'"
    else:
        return None
    data = await request("POST", "/coql", body={"select_query": f"select id from Leads where {where} limit 1"},
                         cost=CREDITS["COQL"])
    rows = data.get("data") or []
    return str(rows[0]["id"]) if rows else None


def _kv(key: str) -> Optional[str]:
    rows = db.query("SELECT value_json FROM kv WHERE key = ?", (key,))
    return json.loads(rows[0]["value_json"]) if rows else None


def _kv_set(key: str, value: Any) -> None:
    db.execute("INSERT OR REPLACE INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)",
               (key, db.dumps(value), core.now()))


async def upsert(lead: dict) -> str:
    rec = record(lead)
    zid = lead.get("zoho_id") or await _find_existing(lead)
    if zid:
        await request("PUT", "/Leads", body={"data": [{**rec, "id": zid}]})
        old_tier = _kv(f"zoho_tier:{lead['lead_id']}")
        if old_tier in TIERS and old_tier != lead.get("tier"):  # tier tags: keep only the current one
            await request("POST", "/Leads/actions/remove_tags",
                          body={"tags": [{"name": old_tier}], "ids": [zid]})
    else:
        data = await request("POST", "/Leads", body={"data": [rec]})
        item = (data.get("data") or [{}])[0]
        zid = str((item.get("details") or {}).get("id") or "")
        if not zid:
            raise ZohoError("client", f"insert returned no id: {str(item)[:200]}")
    core.update(lead["lead_id"], zoho_id=zid, zoho_synced_at=core.now(), zoho_dirty=0)
    _kv_set(f"zoho_tier:{lead['lead_id']}", lead.get("tier"))
    return zid


async def _process(row: dict) -> None:
    op, lead_id = row["op"], row["lead_id"]
    payload = json.loads(row["payload_json"] or "{}")
    try:
        if op == "delete":
            if payload.get("zoho_id"):
                await request("DELETE", f"/Leads/{payload['zoho_id']}")
        else:
            lead = core.get(lead_id)
            if lead is None:
                pass  # erased meanwhile
            elif op == "upsert":
                if core.in_sync_scope(lead) or lead.get("opted_out_at"):
                    await upsert(lead)
            elif op == "note":
                zid = lead.get("zoho_id")
                if not zid:
                    if not core.in_sync_scope(lead):
                        pass  # never synced (out of scope): nothing to attach the note to
                    else:
                        zid = await upsert(lead)
                if zid:
                    await request("POST", f"/Leads/{zid}/Notes", body={"data": [{
                        "Note_Title": str(payload.get("title") or "Dexter")[:120],
                        "Note_Content": str(payload.get("text") or "")[:3000]}]})
        db.execute("DELETE FROM zoho_outbox WHERE id = ?", (row["id"],))
    except BudgetStop:
        db.execute("UPDATE zoho_outbox SET next_attempt_at = ? WHERE id = ?", (core.now() + 3600, row["id"]))
        raise
    except Exception as exc:  # noqa: BLE001
        kind = exc.kind if isinstance(exc, ZohoError) else "error"
        attempts = row["attempts"] + 1
        logger.warning("Zoho %s for %s failed (%s), attempt %d", op, lead_id, kind, attempts)
        if attempts >= MAX_ATTEMPTS:
            db.execute("DELETE FROM zoho_outbox WHERE id = ?", (row["id"],))
            if lead_id != "-":
                core.activity(lead_id, "zoho_failed", 0, {"op": op, "kind": kind})
        else:
            db.execute("UPDATE zoho_outbox SET attempts = ?, next_attempt_at = ?, last_error_kind = ? WHERE id = ?",
                       (attempts, core.now() + _backoff(attempts), kind, row["id"]))


async def process_due(limit: int = 20) -> int:
    """Send the due outbox rows (max 2 at a time). Returns rows attempted."""
    global _sem
    if not enabled():
        return 0
    rows = [dict(r) for r in db.query("SELECT * FROM zoho_outbox WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
                                      (core.now(), limit))]
    if not rows:
        return 0
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENT)
    stop = False

    async def run(r: dict) -> None:
        nonlocal stop
        if stop:
            return
        async with _sem:
            if stop:
                return
            try:
                await _process(r)
            except BudgetStop:
                stop = True

    await asyncio.gather(*(run(r) for r in rows))
    return len(rows)


# --------------------------------------------------------------------------- #
# Daily pull
# --------------------------------------------------------------------------- #
def apply_status(lead: dict, status: str) -> Optional[str]:
    """Zoho Lead_Status -> Dexter stage (Zoho wins for stages >= contacted). Returns the new stage."""
    stage = STAGE_FOR_STATUS.get(status)
    cur = lead.get("stage") or "new"
    if stage is None or cur == "opted_out" or stage == cur:
        return None
    if stage == "contacted" and cur in core.HUMAN_STAGES:
        return None  # Dexter already has contacted or later
    core.set_stage(lead["lead_id"], stage, "zoho", sync=False)
    return stage


async def pull() -> int:
    """Map Lead_Status changes made in Zoho back to Dexter stages. Returns leads changed."""
    if not enabled():
        return 0
    s = get_settings()
    since = _kv("zoho_last_pull") or (core.now() - 7 * 86400)
    iso = dt.datetime.fromtimestamp(since, core.MX).isoformat(timespec="seconds")
    sources = sorted({s.zoho_lead_source_whatsapp, s.zoho_lead_source_web})
    src = ", ".join("'" + x.replace("'", "") + "'" for x in sources)
    query = (f"select id, Lead_Status, Modified_Time from Leads where Lead_Source in ({src}) "
             f"and Modified_Time >= '{iso}' limit 200")
    started = core.now()
    data = await request("POST", "/coql", body={"select_query": query}, cost=CREDITS["COQL"])
    changed = 0
    for row in data.get("data") or []:
        found = db.query("SELECT lead_id FROM leads WHERE zoho_id = ?", (str(row.get("id")),))
        if found and apply_status(core.get(found[0]["lead_id"]), row.get("Lead_Status") or ""):
            changed += 1
    _kv_set("zoho_last_pull", started)
    logger.info("Zoho pull: %d lead(s) changed", changed)
    return changed


async def maybe_pull() -> None:
    if enabled() and db.safe("flag", lambda: db.set_flag_once(f"zoho_pull:{_day()}"), default=False):
        try:
            await pull()
        except Exception:  # noqa: BLE001
            logger.exception("Zoho pull failed")


# --------------------------------------------------------------------------- #
# Backfill CLI
# --------------------------------------------------------------------------- #
def backfill_rows() -> list[dict]:
    return [lead for lead in core.list_leads(limit=100000) if core.in_sync_scope(lead)]


async def backfill(dry_run: bool) -> int:
    leads = backfill_rows()
    if dry_run:
        for lead in leads:
            rec = record(lead, set())
            rec["Mobile"] = core.mask(lead.get("user_id"))
            rec.pop("Email", None)
            print(json.dumps({"lead_id": lead["lead_id"], "record": rec}, ensure_ascii=False))
        print(f"{len(leads)} lead(s) would be synced (scope: {get_settings().zoho_sync_scope}).")
        return len(leads)
    if not get_settings().zoho_active:
        print("Zoho credentials missing (ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN).")
        return 0
    await check_fields()
    done = 0
    for lead in leads:
        try:
            await upsert(core.get(lead["lead_id"]))
            done += 1
        except BudgetStop:
            print("Stopped: 80% of the daily credit budget used. Run again tomorrow.")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"{lead['lead_id']}: {exc}")
    print(f"{done}/{len(leads)} lead(s) synced.")
    return done


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "backfill":
        print("usage: python -m app.leads.zoho backfill [--dry-run]")
        return 2
    logging.basicConfig(level=logging.INFO)
    # Backfill runs without ZOHO_ENABLED so Alan can sync before switching the live push on.
    asyncio.run(backfill("--dry-run" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
