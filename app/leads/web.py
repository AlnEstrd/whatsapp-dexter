"""HTTP side of leads: admin read access, the web contact form and the owner dashboard.

    GET  /admin/leads               JSON (X-Admin-Token), phones masked
    GET  /admin/leads.csv           CSV, masked unless ?unmask=1
    GET  /admin/leads/funnel        engaged -> consented -> warm -> hot -> contacted -> meeting -> won
    POST /admin/leads/{id}/erase    ARCO erase (lead, activity, outbox, Zoho record)
    POST /admin/leads/{id}/followup follow-up template (one lead, rules in templates.py)
    POST /contact                   web form "Que Alan me contacte" (via the Vercel rewrite)
    GET  /dashboard ...             owner dashboard (DASHBOARD_ENABLED, password >= 16 chars)

No new dependencies: HTML is rendered here, form posts are parsed with the stdlib.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db
from ..channels import normalize_user_id
from ..config import get_settings
from ..flows import quiz
from . import alerts, core, drafts, flows, scoring, templates

logger = logging.getLogger("leads.web")
router = APIRouter()


def _leads_on() -> bool:
    from . import enabled
    return enabled()


def _check_admin(token: Optional[str]) -> None:
    expected = get_settings().admin_token
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="admin disabled or bad token")
    if not _leads_on():
        raise HTTPException(status_code=404, detail="leads disabled")


def _iso(ts: Optional[float]) -> Optional[str]:
    return dt.datetime.fromtimestamp(ts, core.MX).isoformat(timespec="minutes") if ts else None


def public_row(lead: dict, unmask: bool = False) -> dict:
    is_open, left = core.window(lead)
    return {
        "lead_id": lead["lead_id"], "name": lead.get("name") or lead.get("wa_profile_name"),
        "company": lead.get("company"), "role_level": lead.get("role_level"),
        "company_size": lead.get("company_size"), "tier": lead["tier"], "score": lead["score"],
        "stage": lead["stage"], "contactable": lead["contactable"], "source": lead.get("source"),
        "last_seen": _iso(lead.get("last_seen")),
        "window": {"open": is_open, "minutes_left": int(left // 60)},
        "phone": lead.get("user_id") if unmask else core.mask(lead.get("user_id")),
        "email": lead.get("email") if unmask else (("•••@" + lead["email"].split("@")[-1]) if lead.get("email") else None),
    }


@router.get("/admin/leads")
async def admin_leads(tier: Optional[str] = None, stage: Optional[str] = None, contactable_only: bool = False,
                      limit: int = 200, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    rows = core.list_leads(tier=tier or None, stage=stage or None,
                           contactable=True if contactable_only else None, limit=min(limit, 1000))
    return {"leads": [public_row(r) for r in rows]}


CSV_COLS = ["lead_id", "name", "company", "role_level", "company_size", "phone", "email", "tier", "score",
            "stage", "contactable", "source", "last_seen", "consent_at"]


def _csv(rows: list[dict], unmask: bool) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for lead in rows:
        r = public_row(lead, unmask)
        w.writerow([r["lead_id"], r["name"], r["company"], r["role_level"], r["company_size"], r["phone"],
                    r["email"], r["tier"], r["score"], r["stage"], int(r["contactable"]), r["source"],
                    r["last_seen"], _iso(lead.get("consent_ts"))])
    return buf.getvalue()


@router.get("/admin/leads.csv")
async def admin_leads_csv(unmask: int = 0, x_admin_token: Optional[str] = Header(None)) -> Response:
    _check_admin(x_admin_token)
    body = _csv(core.list_leads(limit=100000), bool(unmask))
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


@router.get("/admin/leads/funnel")
async def admin_funnel(days: int = 0, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    return core.funnel(core.now() - days * 86400 if days else None)


@router.post("/admin/leads/{lead_id}/erase")
async def admin_erase(lead_id: str, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    lead = core.erase(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    if lead.get("zoho_id") and get_settings().zoho_enabled:
        from . import zoho
        zoho.enqueue("-", "delete", {"zoho_id": lead["zoho_id"]})
    logger.info("lead %s erased (admin)", lead_id)
    return {"erased": True, "suppression_kept": bool(lead.get("opted_out_at")),
            "zoho_delete_queued": bool(lead.get("zoho_id") and get_settings().zoho_enabled)}


@router.post("/admin/leads/{lead_id}/followup")
async def admin_followup(lead_id: str, x_admin_token: Optional[str] = Header(None)) -> dict:
    _check_admin(x_admin_token)
    ok, note = await templates.send_followup(lead_id, "admin")
    return {"sent": ok, "note": note}


# --------------------------------------------------------------------------- #
# POST /contact (web form)
# --------------------------------------------------------------------------- #
MAX_BODY = 8 * 1024
MIN_FILL_SECONDS = 3
RATE_PER_HOUR = 5
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _rate_limited(bucket: dict[str, deque], key: str, limit: int, window: float) -> bool:
    q = bucket[key]
    t = time.time()
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def normalize_phone(raw: str) -> Optional[str]:
    digits = "".join(c for c in raw or "" if c.isdigit())
    if len(digits) == 10:          # Mexican number without country code
        digits = "52" + digits
    digits = normalize_user_id(digits)
    return digits if 11 <= len(digits) <= 15 else None


def _bad(status: int = 400, error: str = "invalid") -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


async def _read_payload(request: Request) -> Optional[dict]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return None
    ctype = request.headers.get("content-type", "")
    try:
        if "json" in ctype:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else None
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
    except (ValueError, UnicodeDecodeError):
        return None


@router.post("/contact")
async def contact_form(request: Request) -> JSONResponse:
    s = get_settings()
    if not (_leads_on() and s.contact_form_enabled):
        return _bad(404, "unavailable")
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return _bad(413)
    if _rate_limited(_hits, client_ip(request), RATE_PER_HOUR, 3600):
        return _bad(429, "rate_limited")
    data = await _read_payload(request)
    if data is None:
        return _bad()
    if str(data.get("website") or "").strip():            # honeypot
        logger.info("contact form: honeypot")
        return _bad()
    try:
        started = float(data.get("started_at") or 0) / 1000
    except (TypeError, ValueError):
        started = 0
    if not started or time.time() - started < MIN_FILL_SECONDS:
        logger.info("contact form: filled too fast")
        return _bad()
    consent = data.get("consent")
    if consent not in (True, "true", "on", "1", 1):
        return _bad()
    name = core.clean_text(data.get("name"), 80)
    email = (str(data.get("email") or "").strip().lower()) or None
    phone = normalize_phone(str(data.get("whatsapp") or ""))
    message = str(data.get("message") or "")
    if not name or len(message) > 1000 or (email and not core.valid_email(email)) or not (email or phone):
        return _bad()
    if str(data.get("whatsapp") or "").strip() and not phone:
        return _bad()
    channel = data.get("channel") if data.get("channel") in ("whatsapp", "call", "email") else \
        ("whatsapp" if phone else "email")
    if channel in ("whatsapp", "call") and not phone:
        channel = "email"
    if channel == "email" and not email:
        channel = "whatsapp"
    try:
        lead = (core.by_user(phone) if phone else None) or (core.by_email(email) if email else None)
        if lead is None:
            lead, _ = core.ensure(phone, email=email if not phone else None, source="web_form",
                                  lang="en" if data.get("lang") == "en" else "es", reason="web_form")
        elif phone and not lead.get("user_id"):
            core.update(lead["lead_id"], user_id=phone)
        fields = {"name": name, "company": data.get("company"), "email": email,
                  "role_level": data.get("role_level"), "company_size": data.get("company_size")}
        core.set_profile(lead["lead_id"], source="web_form", **fields)
        from . import contact_confirmed
        await contact_confirmed(lead["lead_id"], {"channel": channel, "note": core.scrub(message, 1000)},
                                flows.WEB_FORM_VERSION, "web_form")
    except Exception:  # noqa: BLE001
        logger.exception("contact form failed")
        return _bad(500, "unavailable")
    logger.info("contact form: request recorded for lead %s", lead["lead_id"])
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Dashboard: auth
# --------------------------------------------------------------------------- #
COOKIE = "dx_session"
SESSION_HOURS = 12
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60
LOCKOUT_SECONDS = 15 * 60
_fails: dict[str, deque] = defaultdict(deque)
_locked: dict[str, float] = {}

SEC_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "form-action 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
}


def _dash_on() -> None:
    s = get_settings()
    if not (_leads_on() and s.dashboard_active):
        if s.dashboard_enabled and len(s.dashboard_password or "") < 16:
            logger.error("DASHBOARD_ENABLED=true refused: DASHBOARD_PASSWORD must be at least 16 characters")
        raise HTTPException(status_code=404)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _session(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    rows = db.query("SELECT * FROM dashboard_sessions WHERE token_hash = ? AND expires_at > ?",
                    (_hash(token), core.now()))
    return dict(rows[0]) if rows else None


def password_ok(given: str) -> bool:
    expected = get_settings().dashboard_password or ""
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413)
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}


def _require(request: Request, form: Optional[dict] = None) -> dict:
    sess = _session(request)
    if sess is None:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    if form is not None and not hmac.compare_digest(form.get("csrf", ""), sess["csrf"]):
        logger.warning("dashboard: bad CSRF token")
        raise HTTPException(status_code=403, detail="bad csrf")
    return sess


def _html(body: str, status: int = 200, cookie: Optional[str] = None) -> HTMLResponse:
    resp = HTMLResponse(body, status_code=status, headers=SEC_HEADERS)
    if cookie is not None:
        resp.headers.append("Set-Cookie", cookie)
    return resp


def _cookie(token: str, max_age: int) -> str:
    return f"{COOKIE}={token}; Path=/dashboard; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"


@router.post("/dashboard/login")
async def dashboard_login(request: Request) -> Response:
    _dash_on()
    ip = client_ip(request)
    if _locked.get(ip, 0) > time.time():
        logger.warning("dashboard login locked out ip=%s", ip)
        return _html(page("Dexter", login_body("Demasiados intentos. Intenta en 15 minutos.")), 429)
    form = await _form(request)
    if not password_ok(form.get("password", "")):
        if _rate_limited(_fails, ip, LOGIN_MAX_FAILS - 1, LOGIN_WINDOW):
            _locked[ip] = time.time() + LOCKOUT_SECONDS
            _fails[ip].clear()
        logger.warning("dashboard login failed ip=%s", ip)
        return _html(page("Dexter", login_body("Contraseña incorrecta.")), 401)
    _fails.pop(ip, None)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    t = core.now()
    db.execute("DELETE FROM dashboard_sessions WHERE expires_at < ?", (t,))
    db.execute("INSERT INTO dashboard_sessions (token_hash, csrf, created_at, expires_at) VALUES (?, ?, ?, ?)",
               (_hash(token), csrf, t, t + SESSION_HOURS * 3600))
    logger.info("dashboard login ok ip=%s", ip)
    resp = RedirectResponse("/dashboard", status_code=303, headers=SEC_HEADERS)
    resp.headers.append("Set-Cookie", _cookie(token, SESSION_HOURS * 3600))
    return resp


@router.post("/dashboard/logout")
async def dashboard_logout(request: Request) -> Response:
    _dash_on()
    form = await _form(request)
    sess = _require(request, form)
    db.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (sess["token_hash"],))
    resp = RedirectResponse("/dashboard", status_code=303, headers=SEC_HEADERS)
    resp.headers.append("Set-Cookie", _cookie("", 0))
    return resp


# --------------------------------------------------------------------------- #
# Dashboard: pages
# --------------------------------------------------------------------------- #
e = html.escape

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#16181d;--muted:#5f6670;--line:#e3e3df;--accent:#1f5eff;--hot:#d9480f;
--warm:#e8a317;--cold:#4a6fa5;--ok:#2b8a3e;--danger:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--card:#1b1d22;--ink:#ecedf0;--muted:#9aa1ab;--line:#2c2f36;
--accent:#6d97ff;--hot:#ff7a45;--warm:#f5b83d;--cold:#8aa9d6;--ok:#51cf66;--danger:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}main{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:22px 0 8px}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.muted{color:var(--muted);font-size:13px}.pill{display:inline-block;border-radius:99px;padding:1px 8px;font-size:12px;
border:1px solid var(--line)}.hot{color:var(--hot);border-color:var(--hot)}.warm{color:var(--warm);border-color:var(--warm)}
.cold{color:var(--cold);border-color:var(--cold)}.btn{display:inline-block;border:0;border-radius:10px;padding:10px 14px;
background:var(--accent);color:#fff;text-decoration:none;font-size:15px;cursor:pointer}.btn.alt{background:transparent;
color:var(--accent);border:1px solid var(--accent)}.btn.ok{background:var(--ok)}.actions{display:flex;flex-wrap:wrap;gap:8px;
margin:8px 0}input,select,textarea{font:inherit;padding:9px;border-radius:9px;border:1px solid var(--line);
background:var(--card);color:var(--ink);max-width:100%}textarea{width:100%}table{width:100%;border-collapse:collapse}
td{padding:4px 0;vertical-align:top;border-bottom:1px solid var(--line)}td:first-child{color:var(--muted);width:38%}
form.inline{display:inline}.filters{display:flex;flex-wrap:wrap;gap:6px}.warn{color:var(--danger)}
"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='es'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='robots' content='noindex,nofollow'><meta name='apple-mobile-web-app-capable' content='yes'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def login_body(error: str = "") -> str:
    err = f"<p class='warn'>{e(error)}</p>" if error else ""
    return ("<h1>Dexter · Leads</h1><div class='card'><form method='post' action='/dashboard/login'>"
            f"{err}<p><input type='password' name='password' autocomplete='current-password' "
            "placeholder='Contraseña' required style='width:100%'></p>"
            "<button class='btn' type='submit'>Entrar</button></form></div>")


def _tier(lead: dict) -> str:
    return f"<span class='pill {e(lead['tier'])}'>{e(lead['tier'])} · {lead['score']}</span>"


def _name(lead: dict) -> str:
    parts = [lead.get("name") or lead.get("wa_profile_name") or "Sin nombre", lead.get("company")]
    return " · ".join(e(p) for p in parts if p)


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    d = core.now() - ts
    if d < 3600:
        return f"hace {int(d // 60)} min"
    if d < 86400:
        return f"hace {int(d // 3600)} h"
    return f"hace {int(d // 86400)} d"


def _lead_card(lead: dict, extra: str = "") -> str:
    return (f"<div class='card'><div class='row'><a href='/dashboard/leads/{e(lead['lead_id'])}'><b>{_name(lead)}</b></a>"
            f"{_tier(lead)}</div><div class='muted'>{e(lead['stage'])} · {e(alerts.ROLE_LABEL.get(lead.get('role_level') or '', ''))}"
            f" · visto {_ago(lead.get('last_seen'))}{' · 🌐 web' if lead.get('source') == 'web_form' else ''}"
            f"{'' if lead['contactable'] else ' · sin consentimiento'}</div>{extra}</div>")


@router.get("/dashboard")
async def dashboard_home(request: Request, tier: str = "", stage: str = "", contactable: str = "",
                         source: str = "", days: str = "", q: str = "") -> Response:
    _dash_on()
    sess = _session(request)
    if sess is None:
        return _html(page("Dexter", login_body()))
    reqs = core.open_requests()
    out = ["<div class='row'><h1>Dexter · Leads</h1>"
           f"<form method='post' action='/dashboard/logout' class='inline'><input type='hidden' name='csrf' "
           f"value='{e(sess['csrf'])}'><button class='btn alt' type='submit'>Salir</button></form></div>"]
    out.append(f"<h2>📇 Solicitudes de contacto ({len(reqs)})</h2>")
    for r in reqs:
        lead = core.get(r["lead_id"])
        if lead:
            detail = f"<div>{e(flows.contact_summary(r))} · {_ago(r['ts'])}</div>"
            if r.get("note"):
                detail += f"<div class='muted'>{e(r['note'])}</div>"
            out.append(_lead_card(lead, detail))
    if not reqs:
        out.append("<p class='muted'>Ninguna pendiente.</p>")
    hot = core.hot_not_reached()
    out.append(f"<h2>🔥 Calientes sin contactar ({len(hot)})</h2>")
    out += [_lead_card(lead) for lead in hot] or ["<p class='muted'>Nada pendiente.</p>"]
    since = core.now() - int(days) * 86400 if days.isdigit() else None
    rows = core.list_leads(tier=tier or None, stage=stage or None,
                           contactable={"1": True, "0": False}.get(contactable), source=source or None,
                           since=since, q=q or None, limit=200)

    def sel(name: str, value: str, opts: list[tuple[str, str]]) -> str:
        return f"<select name='{name}'>" + "".join(
            f"<option value='{e(v)}'{' selected' if v == value else ''}>{e(t)}</option>" for v, t in opts) + "</select>"

    out.append("<h2>Todos</h2><form class='filters' method='get' action='/dashboard'>"
               + sel("tier", tier, [("", "tier"), ("hot", "hot"), ("warm", "warm"), ("cold", "cold")])
               + sel("stage", stage, [("", "etapa")] + [(s, s) for s in core.STAGES])
               + sel("contactable", contactable, [("", "consent."), ("1", "contactable"), ("0", "no contactable")])
               + sel("source", source, [("", "origen"), ("whatsapp", "whatsapp"), ("web_form", "web")])
               + sel("days", days, [("", "fecha"), ("1", "24 h"), ("7", "7 días"), ("30", "30 días")])
               + f"<input name='q' value='{e(q)}' placeholder='nombre o empresa'>"
               "<button class='btn' type='submit'>Filtrar</button></form>")
    out += [_lead_card(lead) for lead in rows] or ["<p class='muted'>Sin resultados.</p>"]
    out.append("<p><a href='/dashboard/leads.csv'>Exportar CSV (contactables)</a></p>")
    return _html(page("Dexter · Leads", "".join(out)))


def zoho_link(lead: dict) -> Optional[str]:
    if not lead.get("zoho_id"):
        return None
    host = get_settings().zoho_api_domain.replace("www.zohoapis", "crm.zoho").replace("zohoapis", "crm.zoho")
    return f"{host.rstrip('/')}/crm/tab/Leads/{lead['zoho_id']}"


def _stage_history(rows: list[dict]) -> str:
    items = [r for r in rows if r["type"] in ("stage", "consent_yes", "consent_no", "opted_out", "note",
                                               "followup_sent", "contact_request", "hot_alert")]
    lines = []
    for r in items[-15:]:
        d = r["data"]
        label = {"stage": f"etapa {d.get('from')} → {d.get('to')} ({d.get('source')}"
                          + (f", {d.get('channel')}" if d.get("channel") else "") + ")",
                 "consent_yes": f"consentimiento sí (v {d.get('version')}, {d.get('via')})",
                 "consent_no": "consentimiento no", "opted_out": "BAJA",
                 "note": f"nota: {d.get('text', '')}", "followup_sent": f"plantilla {d.get('template')}",
                 "contact_request": f"solicitud de contacto ({d.get('channel')})",
                 "hot_alert": "alerta en Slack"}[r["type"]]
        if r["type"] == "stage" and d.get("note"):
            label += f" — {d['note']}"
        lines.append(f"<div class='muted'>{e(_iso(r['ts']) or '')} · {e(label)}</div>")
    return "".join(lines) or "<div class='muted'>—</div>"


@router.get("/dashboard/leads/{lead_id}")
async def dashboard_lead(request: Request, lead_id: str) -> Response:
    _dash_on()
    sess = _require(request)
    lead = core.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    rows = core.activity_rows(lead_id)
    csrf = f"<input type='hidden' name='csrf' value='{e(sess['csrf'])}'>"
    is_open, _ = core.window(lead)
    phone = lead.get("user_id")
    out = [f"<p><a href='/dashboard'>← Leads</a></p><div class='row'><h1>{_name(lead)}</h1>{_tier(lead)}</div>"]
    actions = []
    if lead["contactable"]:
        if (link := drafts.wa_link(lead)):
            actions.append(f"<a class='btn' href='{e(link)}'>Escribir desde mi WhatsApp</a>")
        if phone:
            actions.append(f"<a class='btn alt' href='tel:+{e(phone)}'>Llamar</a>")
        if lead.get("email"):
            actions.append(f"<a class='btn alt' href='mailto:{e(lead['email'])}'>Correo</a>")
        out.append("<div class='actions'>" + "".join(actions) + "</div>")
        out.append(f"<div class='card'><form method='post' action='/dashboard/leads/{e(lead_id)}/contacted'>{csrf}"
                   "<b>Marcar contactado</b><div class='actions'><select name='channel'>"
                   "<option value='personal_whatsapp'>Mi WhatsApp</option><option value='call'>Llamada</option>"
                   "<option value='email'>Correo</option><option value='dexter_template'>Plantilla Dexter</option>"
                   "</select><input name='note' placeholder='nota (opcional)'>"
                   "<button class='btn ok' type='submit'>Contactado</button></div></form>"
                   f"<form method='post' action='/dashboard/leads/{e(lead_id)}/stage' class='actions'>{csrf}"
                   + "".join(f"<button class='btn alt' name='stage' value='{s}' type='submit'>{s}</button>"
                             for s in ("meeting", "proposal", "won", "lost", "nurture"))
                   + "</form>"
                   f"<form method='post' action='/dashboard/leads/{e(lead_id)}/note'>{csrf}"
                   "<textarea name='text' rows='2' placeholder='Nota'></textarea>"
                   "<button class='btn alt' type='submit'>Guardar nota</button></form>")
        if not is_open and templates.template_for(lead)[0] and phone:
            out.append(f"<form method='post' action='/dashboard/leads/{e(lead_id)}/template' "
                       "onsubmit=\"return confirm('¿Enviar la plantilla de seguimiento de Dexter?')\">"
                       f"{csrf}<button class='btn alt' type='submit'>Enviar plantilla Dexter</button></form>")
        out.append("</div>")
    else:
        link = alerts.conversation_link(lead)
        if is_open and link:
            out.append(f"<div class='card warn'>Sin consentimiento. Su ventana está abierta: "
                       f"<a href='{e(link)}'>pídeselo en su hilo de Slack</a>.</div>")
        else:
            out.append("<div class='card warn'>sin consentimiento — no contactar</div>")
    weakest = alerts.domain_names(lead.get("weakest_domains") or [], "es")
    scores = lead.get("quiz_scores") or {}
    quiz_line = "—"
    if lead.get("quiz_level") is not None:
        quiz_line = f"L{lead['quiz_level']} · débiles: {weakest}<br><span class='muted'>" + " · ".join(
            f"{q['domain']['es']} {scores.get(q['key'], '—')}" for q in quiz.QUESTIONS) + "</span>"
    topics = ", ".join(f"{k} ({v})" for k, v in (lead.get("topics") or {}).items()) or "—"
    consent = "—"
    if lead.get("consent_ts"):
        consent = f"sí · {_iso(lead['consent_ts'])} · v {e(lead.get('consent_text_version') or '')}"
    if lead.get("opted_out_at"):
        consent = f"BAJA · {_iso(lead['opted_out_at'])}"
    elif lead.get("consent_declined_at") and not lead.get("consent_ts"):
        consent = f"no · {_iso(lead['consent_declined_at'])}"
    rows_html = [
        ("Teléfono", f"+{phone}" if (phone and lead["contactable"]) else core.mask(phone)),
        ("Correo", (lead.get("email") or "—") if lead["contactable"] else ("•••" if lead.get("email") else "—")),
        ("Rol", f"{lead.get('role') or ''} {alerts.ROLE_LABEL.get(lead.get('role_level') or '', '')}".strip() or "—"),
        ("Tamaño", alerts.SIZE_LABEL.get(lead.get("company_size") or "", "—")),
        ("Industria", lead.get("industry") or "—"), ("Origen", lead.get("source") or "—"),
        ("Etapa", f"{lead['stage']} ({lead.get('stage_source')})"),
        ("WhatsApp", core.window_text(lead) if phone else "sin número"),
        ("Interés", lead.get("interest_note") or "—"), ("Temas", topics),
        ("Consentimiento", consent), ("Sesiones", str(lead.get("sessions") or 1)),
        ("Primera / última vez", f"{_iso(lead.get('first_seen'))} / {_iso(lead.get('last_seen'))}"),
    ]
    table = "".join(f"<tr><td>{e(k)}</td><td>{v if k in ('Diagnóstico',) else e(str(v))}</td></tr>" for k, v in rows_html)
    table += f"<tr><td>Diagnóstico</td><td>{quiz_line}</td></tr>"
    out.append(f"<div class='card'><table>{table}</table></div>")
    reasons = "".join(f"<tr><td>{e(label)}</td><td>+{p}</td></tr>" for label, p in scoring.breakdown(lead, rows))
    out.append(f"<h2>Por qué {e(lead['tier'])} (score {lead['score']} = fit {lead['fit_score']} + intent "
               f"{lead['intent_score']})</h2><div class='card'><table>{reasons or '<tr><td>Sin señales</td><td></td></tr>'}</table></div>")
    links = []
    if (link := alerts.conversation_link(lead)):
        links.append(f"<a href='{e(link)}'>Hilo en Slack</a>")
    if (link := zoho_link(lead)):
        links.append(f"<a href='{e(link)}'>Zoho</a>")
    if links:
        out.append("<p>" + " · ".join(links) + "</p>")
    reqs = core.open_requests(lead_id)
    if reqs:
        out.append("<h2>Solicitud abierta</h2>" + "".join(
            f"<div class='card'>{e(flows.contact_summary(r))} · {_ago(r['ts'])}"
            + (f"<div class='muted'>{e(r['note'])}</div>" if r.get("note") else "") + "</div>" for r in reqs))
    out.append(f"<h2>Historial</h2><div class='card'>{_stage_history(rows)}</div>")
    return _html(page(f"{lead.get('name') or 'Lead'} · Dexter", "".join(out)))


def _log(sess: dict, action: str, lead_id: str) -> None:
    logger.info("dashboard action %s lead=%s session=%s", action, lead_id, sess["token_hash"][:8])


def _back(lead_id: str) -> RedirectResponse:
    return RedirectResponse(f"/dashboard/leads/{quote(lead_id)}", status_code=303, headers=SEC_HEADERS)


def _contactable_lead(lead_id: str) -> dict:
    lead = core.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    if not lead["contactable"]:
        raise HTTPException(status_code=403, detail="lead not contactable")
    return lead


@router.post("/dashboard/leads/{lead_id}/contacted")
async def dashboard_contacted(request: Request, lead_id: str) -> Response:
    _dash_on()
    form = await _form(request)
    sess = _require(request, form)
    _contactable_lead(lead_id)
    channel = form.get("channel") if form.get("channel") in core.CONTACT_CHANNELS else "personal_whatsapp"
    core.set_stage(lead_id, "contacted", "dashboard", channel=channel, note=form.get("note") or None)
    _log(sess, f"contacted/{channel}", lead_id)
    return _back(lead_id)


@router.post("/dashboard/leads/{lead_id}/stage")
async def dashboard_stage(request: Request, lead_id: str) -> Response:
    _dash_on()
    form = await _form(request)
    sess = _require(request, form)
    _contactable_lead(lead_id)
    stage = form.get("stage", "")
    if stage not in core.HUMAN_STAGES:
        raise HTTPException(status_code=400)
    core.set_stage(lead_id, stage, "dashboard")
    _log(sess, f"stage/{stage}", lead_id)
    return _back(lead_id)


@router.post("/dashboard/leads/{lead_id}/note")
async def dashboard_note(request: Request, lead_id: str) -> Response:
    _dash_on()
    form = await _form(request)
    sess = _require(request, form)
    _contactable_lead(lead_id)
    if form.get("text", "").strip():
        core.add_note(lead_id, form["text"], "dashboard")
        if get_settings().zoho_enabled:
            from . import zoho
            zoho.enqueue(lead_id, "note", {"title": "Nota de Alan (dashboard)", "text": form["text"][:3000]})
    _log(sess, "note", lead_id)
    return _back(lead_id)


@router.post("/dashboard/leads/{lead_id}/template")
async def dashboard_template(request: Request, lead_id: str) -> Response:
    _dash_on()
    form = await _form(request)
    sess = _require(request, form)
    _contactable_lead(lead_id)
    ok, note = await templates.send_followup(lead_id, "dashboard")
    _log(sess, f"template/{'sent' if ok else 'refused'}", lead_id)
    return _html(page("Plantilla", f"<p>{e(note)}</p><p><a href='/dashboard/leads/{e(lead_id)}'>← Volver</a></p>"))


@router.get("/dashboard/leads.csv")
async def dashboard_csv(request: Request) -> Response:
    _dash_on()
    sess = _require(request)
    _log(sess, "export_csv", "*")
    body = _csv(core.list_leads(contactable=True, limit=100000), unmask=True)
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={**SEC_HEADERS, "Content-Disposition": "attachment; filename=dexter-leads.csv"})


def reset() -> None:  # tests
    _hits.clear()
    _fails.clear()
    _locked.clear()

