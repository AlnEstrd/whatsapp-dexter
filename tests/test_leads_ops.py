"""Leads, operations side: Slack speed-to-lead, templates, Zoho sync, admin read access,
the owner dashboard and the web contact form."""
import asyncio
import json
import time
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.flows import quiz
from tests.test_channels import META_SEND, SLACK
from tests.test_slack_bridge import BOT_ENV, SlackMock, slack_event, thread_reply

USER = "523312345678"
LEADS = {"LEADS_ENABLED": "true", "FLOWS_ENABLED": "true", "USER_MSG_PER_MINUTE": "100"}
ZOHO_ENV = {"ZOHO_ENABLED": "true", "ZOHO_CLIENT_ID": "cid", "ZOHO_CLIENT_SECRET": "csecret",
            "ZOHO_REFRESH_TOKEN": "rtoken"}
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ZAPI = "https://www.zohoapis.com/crm/v8"
PASSWORD = "correct horse battery staple"
DASH = {"DASHBOARD_ENABLED": "true", "DASHBOARD_PASSWORD": PASSWORD}


def hot_lead(uid=USER, lang="es", consent=True, name="Ana López"):
    from app.leads import core
    lid = core.engage(uid, "x", profile_name=name, lang=lang)["lead_id"]
    core.set_profile(lid, name=name, company="Acme", role_level="owner_exec", company_size="11-50")
    core.record_quiz(lid, quiz.score([1] * 9))
    if consent:
        core.record_consent(lid, True, "2026-10-v1", "test")
    core.add_intent(lid, "handoff_business")
    core.recompute(lid)
    return lid


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
@respx.mock
def test_hot_alert_once_with_content(make_client):
    slack = SlackMock()
    make_client(**LEADS, **BOT_ENV, SLACK_LEADS_CHANNEL_ID="CLEADS")
    from app import leads
    from app.leads import core
    lid = hot_lead()
    run(leads.effects(USER))
    run(leads.effects(USER))
    run(leads.tick())
    posts = [p for p in slack.posts if p["channel"] == "CLEADS" and "Lead caliente" in p["text"]]
    assert len(posts) == 1
    text = posts[0]["text"]
    for s in ("<@UOWNER>", "Ana López", "Acme", "Dueño(a)/Dir. general", "11–50", "Score 80", "L1",
              "Adopción, Estrategia y valor", "ventana abierta", "`contacted`"):
        assert s in text, s
    assert core.get(lid)["slack_alert_ts"] == "1700000000.000001"


@respx.mock
def test_slack_commands_in_alert_thread(make_client):
    slack = SlackMock()
    c = make_client(**LEADS, **BOT_ENV, SLACK_LEADS_CHANNEL_ID="CLEADS")
    from app import leads
    from app.leads import core
    lid = hot_lead()
    run(leads.effects(USER))
    ts = core.get(lid)["slack_alert_ts"]
    slack_event(c, thread_reply("contacted", ts, channel="CLEADS", ts="1700000100.000001"))
    row = core.get(lid)
    assert row["stage"] == "contacted" and row["stage_source"] == "slack" and row["zoho_dirty"]
    slack_event(c, thread_reply("note llamar el lunes", ts, channel="CLEADS", ts="1700000100.000002"))
    assert [a["data"]["text"] for a in core.activity_rows(lid) if a["type"] == "note"] == ["llamar el lunes"]
    slack_event(c, thread_reply("meeting", ts, channel="CLEADS", ts="1700000100.000003"))
    assert core.get(lid)["stage"] == "meeting"
    assert any("Etapa → *meeting*" in p["text"] for p in slack.replies())


@respx.mock
def test_command_in_conversation_thread_never_reaches_customer(make_client):
    slack = SlackMock()
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    c = make_client(**LEADS, **BOT_ENV)
    from tests.test_leads import say
    say(c, "humano")                                       # thread + human mode + lead
    parent = slack.parents()[0]
    thread_ts = "1700000000.000001"
    assert parent["channel"] == "C123"
    n = len(meta.calls)
    slack_event(c, thread_reply("note quiere cotización", thread_ts))
    assert len(meta.calls) == n                            # not forwarded to WhatsApp
    from app.leads import core
    assert core.activity_rows(core.by_user(USER)["lead_id"])[-1]["type"] == "note"


@respx.mock
def test_sla_and_window_reminders_fire_once(make_client, monkeypatch):
    slack = SlackMock()
    make_client(**LEADS, **BOT_ENV, SLACK_LEADS_CHANNEL_ID="CLEADS", **DASH)
    from app import leads
    from app.leads import alerts, core
    lid = hot_lead()
    run(leads.effects(USER))
    core.update(lid, hot_alerted_at=core.now() - 25 * 3600, last_inbound_at=core.now() - 22.5 * 3600)
    run(alerts.check_reminders())
    run(alerts.check_reminders())
    texts = [p["text"] for p in slack.replies()]
    assert sum("sigue sin contacto" in t for t in texts) == 1
    assert sum("ventana de WhatsApp" in t for t in texts) == 1
    assert any(f"/dashboard/leads/{lid}" in t for t in texts)


@respx.mock
def test_leads_digest_once_and_skips_empty_days(make_client, monkeypatch):
    slack = SlackMock()
    make_client(**LEADS, **BOT_ENV, SLACK_LEADS_CHANNEL_ID="CLEADS")
    from app.leads import alerts, core
    assert alerts.digest_text(core.now() - 86400) is None       # empty day
    hot_lead()
    core.opt_out("5215550009999")
    assert run(alerts.maybe_digest()) in (True, False)          # depends on the hour; force it below
    import datetime as dt
    nine = dt.datetime.now(core.MX).replace(hour=9).timestamp()
    monkeypatch.setattr(core, "now", lambda: nine)
    from app import db
    db.execute("DELETE FROM flags")
    assert run(alerts.maybe_digest()) is True
    assert run(alerts.maybe_digest()) is False
    text = [p["text"] for p in slack.parents() if "Leads, últimas 24 h" in p["text"]][-1]
    assert "Nuevos: 1 (🔥 1" in text and "Consentimiento: 1 sí · 0 preguntas" in text


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
@respx.mock
def test_template_rules_and_confirmation(make_client):
    slack = SlackMock()
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    c = make_client(**LEADS, **BOT_ENV, SLACK_LEADS_CHANNEL_ID="CLEADS")
    from app import leads
    from app.leads import core, templates
    cold = core.engage("5215550001111", "x")
    assert "not contactable" in templates.check(core.get(cold["lead_id"]))
    lid = hot_lead()
    assert "not configured" in templates.check(core.get(lid))
    run(leads.effects(USER))
    ts = core.get(lid)["slack_alert_ts"]
    from app.config import get_settings
    import os
    os.environ["TEMPLATE_FOLLOWUP_ES"] = "dexter_seguimiento_es"
    get_settings.cache_clear()
    slack_event(c, thread_reply("followup", ts, channel="CLEADS", ts="1700000200.000001"))
    assert not [m for m in meta.calls if json.loads(m.request.content).get("type") == "template"]
    assert "confirm" in slack.replies()[-1]["text"]
    slack_event(c, thread_reply("✅ confirm", ts, channel="CLEADS", ts="1700000200.000002"))
    tpl = [json.loads(m.request.content) for m in meta.calls if json.loads(m.request.content).get("type") == "template"]
    assert len(tpl) == 1 and tpl[0]["template"]["name"] == "dexter_seguimiento_es"
    assert tpl[0]["template"]["language"]["code"] == "es_MX"
    assert tpl[0]["template"]["components"][0]["parameters"][0]["text"] == "Ana"
    assert "7 days" in templates.check(core.get(lid))
    assert [a["type"] for a in core.activity_rows(lid)].count("followup_sent") == 1


def test_template_language_and_neutral_name(make_client):
    make_client(**LEADS, TEMPLATE_FOLLOWUP_EN="dexter_followup_en")
    from app.leads import core, templates
    lid = core.engage(USER, "x", lang="en")["lead_id"]
    p = templates.payload(core.get(lid))
    assert p["template"]["name"] == "dexter_followup_en" and p["template"]["language"]["code"] == "en"
    assert p["template"]["components"][0]["parameters"][0]["text"] == "there"


# --------------------------------------------------------------------------- #
# Zoho
# --------------------------------------------------------------------------- #
def test_zoho_mapping_with_and_without_custom_fields(make_client):
    make_client(**LEADS)
    from app.leads import core, zoho
    lid = hot_lead()
    rec = zoho.record(core.get(lid), set())
    assert (rec["First_Name"], rec["Last_Name"], rec["Company"], rec["Mobile"]) == ("Ana", "López", "Acme", "+523312345678")
    assert rec["Lead_Status"] == "Pre-Qualified" and rec["Lead_Source"] == "Chat" and rec["No_of_Employees"] == 30
    assert [t["name"] for t in rec["Tag"]] == ["dexter", "hot"] and rec["Email_Opt_Out"] is False
    assert "Razones" in rec["Description"] and "Consentimiento" in rec["Description"]
    assert "Dexter_Score" not in rec
    rec = zoho.record(core.get(lid), {"Dexter_Score", "Dexter_Tier", "Dexter_Lead_ID"})
    assert rec["Dexter_Score"] == 80 and rec["Dexter_Tier"] == "hot" and rec["Dexter_Lead_ID"] == lid
    anon = core.engage("5215550001234", "x")["lead_id"]
    assert zoho.record(core.get(anon), set())["Last_Name"] == "WhatsApp ••1234"
    core.opt_out(USER)
    rec = zoho.record(core.get(lid), set())
    assert rec["Lead_Status"] == "Not Qualified" and rec["Email_Opt_Out"] is True
    assert "opted_out" in [t["name"] for t in rec["Tag"]]


def test_zoho_disabled_makes_no_calls(make_client):
    make_client(**LEADS)
    with respx.mock(assert_all_mocked=True) as router:
        from app import db
        from app.leads import zoho
        hot_lead()
        assert db.count("zoho_outbox") == 0
        assert run(zoho.process_due()) == 0
        assert not router.calls


def test_zoho_debounce_one_row_per_lead(make_client):
    make_client(**LEADS, **ZOHO_ENV)
    from app import db
    lid = hot_lead()
    rows = db.query("SELECT * FROM zoho_outbox WHERE lead_id = ? AND op = 'upsert'", (lid,))
    assert len(rows) == 1 and rows[0]["next_attempt_at"] > time.time() + 50


def _due():
    from app import db
    db.execute("UPDATE zoho_outbox SET next_attempt_at = 0")


@respx.mock
def test_zoho_insert_then_token_refresh_on_401(make_client):
    make_client(**LEADS, **ZOHO_ENV)
    tokens = iter(["t1", "t2"])
    token = respx.post(TOKEN_URL).mock(side_effect=lambda r: httpx.Response(200, json={"access_token": next(tokens)}))
    coql = respx.post(f"{ZAPI}/coql").mock(return_value=httpx.Response(204))
    insert = respx.post(f"{ZAPI}/Leads").mock(return_value=httpx.Response(
        201, json={"data": [{"code": "SUCCESS", "details": {"id": "9001"}}]}))
    from app.leads import core, zoho
    lid = hot_lead()
    _due()
    run(zoho.process_due())
    assert core.get(lid)["zoho_id"] == "9001" and not core.get(lid)["zoho_dirty"]
    assert coql.called and "Mobile = '+523312345678'" in json.loads(coql.calls[0].request.content)["select_query"]
    body = json.loads(insert.calls[0].request.content)["data"][0]
    assert body["Last_Name"] == "López" and insert.calls[0].request.headers["Authorization"] == "Zoho-oauthtoken t1"
    # later changes update by id; a 401 refreshes the token once
    put = respx.put(f"{ZAPI}/Leads").mock(side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={"data": []})])
    core.set_stage(lid, "contacted", "slack")
    _due()
    run(zoho.process_due())
    assert token.call_count == 2 and put.call_count == 2
    assert put.calls[1].request.headers["Authorization"] == "Zoho-oauthtoken t2"
    assert json.loads(put.calls[1].request.content)["data"][0]["Lead_Status"] == "Contacted"


@respx.mock
def test_zoho_backoff_on_server_error(make_client):
    make_client(**LEADS, **ZOHO_ENV)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{ZAPI}/coql").mock(return_value=httpx.Response(500))
    from app import db
    from app.leads import zoho
    hot_lead()
    _due()
    run(zoho.process_due())
    row = db.query("SELECT * FROM zoho_outbox WHERE op = 'upsert'")[0]
    assert row["attempts"] == 1 and row["last_error_kind"] == "server"
    assert 50 < row["next_attempt_at"] - time.time() < 70


@respx.mock
def test_zoho_daily_credit_stop_alerts_once(make_client):
    make_client(**LEADS, **ZOHO_ENV)
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    from app import db
    from app.leads import zoho
    hot_lead()
    db.execute("INSERT INTO kv (key, value_json, updated_at) VALUES (?, '4000', 0)", (f"zoho_credits:{zoho._day()}",))
    _due()
    run(zoho.process_due())
    _due()
    run(zoho.process_due())
    alerts = [json.loads(c.request.content)["text"] for c in slack.calls if "Zoho sync paused" in json.loads(c.request.content)["text"]]
    assert len(alerts) == 1
    assert db.count("zoho_outbox") >= 1                     # kept for tomorrow


@respx.mock
def test_zoho_pull_maps_statuses(make_client):
    make_client(**LEADS, **ZOHO_ENV)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    coql = respx.post(f"{ZAPI}/coql").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "9001", "Lead_Status": "Lost Lead", "Modified_Time": "2026-10-11T10:00:00-06:00"}]}))
    from app.leads import core, zoho
    lid = hot_lead()
    core.update(lid, zoho_id="9001")
    assert run(zoho.pull()) == 1
    assert core.get(lid)["stage"] == "lost" and core.get(lid)["stage_source"] == "zoho"
    assert "Lead_Source in ('Chat')" in json.loads(coql.calls[0].request.content)["select_query"]


@respx.mock
def test_admin_erase_deletes_zoho_record(make_client):
    c = make_client(**LEADS, **ZOHO_ENV)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    delete = respx.delete(f"{ZAPI}/Leads/9001").mock(return_value=httpx.Response(200, json={}))
    from app.leads import core, zoho
    lid = hot_lead()
    core.update(lid, zoho_id="9001")
    r = c.post(f"/admin/leads/{lid}/erase", headers={"X-Admin-Token": "admin"})
    assert r.json() == {"erased": True, "suppression_kept": False, "zoho_delete_queued": True}
    run(zoho.process_due())
    assert delete.called and core.get(lid) is None


def test_backfill_dry_run_masks(make_client, capsys):
    make_client(**LEADS)
    from app.leads import zoho
    hot_lead()
    assert run(zoho.backfill(dry_run=True)) == 1
    out = capsys.readouterr().out
    assert "••5678" in out and "523312345678" not in out


# --------------------------------------------------------------------------- #
# Admin read access
# --------------------------------------------------------------------------- #
def test_admin_leads_masked_csv_and_funnel(make_client):
    c = make_client(**LEADS)
    from app.leads import core
    hot_lead()
    other = core.engage("5215550001111", "x")["lead_id"]
    core.opt_out("5215550001111")
    assert c.get("/admin/leads").status_code == 403
    rows = c.get("/admin/leads", headers={"X-Admin-Token": "admin"}).json()["leads"]
    assert {r["phone"] for r in rows} == {"••5678", "••1111"}
    csv_masked = c.get("/admin/leads.csv", headers={"X-Admin-Token": "admin"}).text
    assert "523312345678" not in csv_masked
    assert "523312345678" in c.get("/admin/leads.csv?unmask=1", headers={"X-Admin-Token": "admin"}).text
    core.set_stage(core.by_user(USER)["lead_id"], "contacted", "dashboard")
    f = c.get("/admin/leads/funnel", headers={"X-Admin-Token": "admin"}).json()
    assert (f["engaged"], f["consented"], f["hot"], f["contacted"], f["meeting"]) == (2, 1, 1, 1, 0)
    assert f["consent_rate"] == 0.5 and f["opt_out_rate"] == 0.5
    assert f["median_hours_to_contact_hot"] is not None and other


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def dash(make_client, **env):
    make_client(**LEADS, **env)
    from app.main import app
    return TestClient(app, base_url="https://testserver")


def login(d, password=PASSWORD):
    return d.post("/dashboard/login", content=f"password={password}",
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)


def csrf(d):
    from app import db
    return db.query("SELECT csrf FROM dashboard_sessions")[0]["csrf"]


def form(d, path, **fields):
    from urllib.parse import urlencode
    return d.post(path, content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"},
                  follow_redirects=False)


def test_dashboard_disabled_by_default_and_short_password_refused(make_client):
    assert dash(make_client).get("/dashboard").status_code == 404
    assert dash(make_client, DASHBOARD_ENABLED="true", DASHBOARD_PASSWORD="short").get("/dashboard").status_code == 404


def test_dashboard_login_cookie_lockout(make_client):
    d = dash(make_client, **DASH)
    page = d.get("/dashboard")
    assert page.status_code == 200 and "Contraseña" in page.text
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert login(d, "wrong").status_code == 401
    r = login(d)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/dashboard"):
        assert flag in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    from app import db
    assert db.count("dashboard_sessions") == 1
    assert not db.query("SELECT 1 FROM dashboard_sessions WHERE token_hash = ?", (token,))   # stored hashed
    assert "Calientes sin contactar" in d.get("/dashboard").text
    d2 = dash(make_client, **DASH)
    for _ in range(5):
        login(d2, "wrong")
    assert login(d2).status_code == 429                   # locked out, even with the right password


def test_dashboard_csrf_and_contactable_rules(make_client):
    d = dash(make_client, **DASH)
    lid = hot_lead()
    noc = hot_lead("5215550002222", consent=False, name="Beto Ruiz")
    login(d)
    assert form(d, f"/dashboard/leads/{lid}/contacted", channel="call").status_code == 403    # no CSRF
    page = d.get(f"/dashboard/leads/{noc}").text
    assert "wa.me" not in page and "tel:" not in page and "••2222" in page
    assert "sin consentimiento — no contactar" in page or "pídeselo en su hilo" in page
    assert form(d, f"/dashboard/leads/{noc}/contacted", channel="call", csrf=csrf(d)).status_code == 403
    page = d.get(f"/dashboard/leads/{lid}").text
    link = page.split("href='https://wa.me/")[1].split("'")[0]
    phone, text = link.split("?text=")
    assert phone == USER
    draft = unquote(text.replace("&amp;", "&"))
    assert draft.startswith("Hola Ana, soy Alan Estrada.") and "Adopción" in draft
    assert "%20" in text and " " not in text


def test_dashboard_mark_contacted_closes_requests(make_client):
    d = dash(make_client, **DASH)
    from app.leads import core
    lid = hot_lead()
    core.add_contact_request(lid, "call", "afternoon", None, None)
    login(d)
    assert "Solicitudes de contacto (1)" in d.get("/dashboard").text
    r = form(d, f"/dashboard/leads/{lid}/contacted", channel="personal_whatsapp", note="le escribí", csrf=csrf(d))
    assert r.status_code == 303
    row = core.get(lid)
    assert row["stage"] == "contacted" and row["stage_source"] == "dashboard" and row["zoho_dirty"]
    last = [a for a in core.activity_rows(lid) if a["type"] == "stage"][-1]["data"]
    assert last["channel"] == "personal_whatsapp" and last["note"] == "le escribí"
    assert not core.open_requests(lid)
    assert "Solicitudes de contacto (0)" in d.get("/dashboard").text


def test_dashboard_english_draft(make_client):
    make_client(**LEADS)
    from app.leads import core, drafts
    lid = hot_lead(lang="en")
    link = drafts.wa_link(core.get(lid))
    assert link.startswith(f"https://wa.me/{USER}?text=Hi%20Ana%2C%20this%20is%20Alan")


def test_hot_not_reached_out_view(make_client):
    d = dash(make_client, **DASH)
    from app.leads import core
    a = hot_lead("5215550000001", name="Ana Uno")
    b = hot_lead("5215550000002", name="Beto Dos")
    cc = hot_lead("5215550000003", name="Caro Tres")
    w = core.engage("5215550000004", "x", profile_name="Dani Cuatro")["lead_id"]
    core.update(a, last_inbound_at=core.now() - 3 * 86400)
    core.update(b, last_inbound_at=core.now() - 3600)
    core.update(cc, last_inbound_at=core.now() - 3 * 86400)
    core.set_stage(cc, "contacted", "slack")
    assert [x["lead_id"] for x in core.hot_not_reached()] == [a]
    login(d)
    section = d.get("/dashboard").text.split("Calientes sin contactar")[1].split("<h2>Todos")[0]
    assert "Ana Uno" in section and "Beto Dos" not in section and "Caro Tres" not in section and w


def test_dashboard_csv_needs_session_and_is_unmasked(make_client):
    d = dash(make_client, **DASH)
    hot_lead()
    hot_lead("5215550002222", consent=False, name="Beto Ruiz")
    assert d.get("/dashboard/leads.csv", follow_redirects=False).status_code == 303
    login(d)
    body = d.get("/dashboard/leads.csv").text
    assert USER in body and "5215550002222" not in body


# --------------------------------------------------------------------------- #
# Web contact form
# --------------------------------------------------------------------------- #
def web(c, ip="1.2.3.4", **over):
    data = {"name": "Ana López", "company": "Acme", "email": "ana@acme.mx", "whatsapp": "", "channel": "email",
            "message": "Queremos implementar IA en ventas", "consent": True, "website": "",
            "started_at": (time.time() - 10) * 1000, "role_level": "owner_exec", "company_size": "11-50"}
    data.update(over)
    return c.post("/contact", json=data, headers={"X-Forwarded-For": ip})


@respx.mock
def test_web_form_email_only_lead(make_client):
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(**LEADS)
    r = web(c)
    assert r.status_code == 200 and r.json() == {"ok": True}
    from app.leads import core
    row = core.by_email("ana@acme.mx")
    assert row["user_id"] is None and row["source"] == "web_form" and row["tier"] == "hot"
    assert row["consent_followup"] and row["consent_text_version"] == "web-form-2026-10-v1"
    assert core.open_requests(row["lead_id"])[0]["note"] == "Queremos implementar IA en ventas"
    assert sum("Solicitud de contacto" in json.loads(x.request.content)["text"] for x in slack.calls) == 1
    from app.leads import drafts
    assert drafts.wa_link(row) is None                                 # mailto only


@respx.mock
def test_web_form_phone_merges_with_whatsapp_lead(make_client):
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(**LEADS)
    from app.leads import core
    wa = core.engage(USER, "quiz_start")["lead_id"]
    assert web(c, whatsapp="33 1234 5678", email="", channel="call").json() == {"ok": True}
    row = core.by_user(USER)
    assert row["lead_id"] == wa and row["source"] == "organic" and row["company"] == "Acme"
    assert core.open_requests(wa)[0]["channel"] == "call"


@pytest.mark.parametrize("over", [{"consent": False}, {"consent": None}, {"website": "http://spam"},
                                  {"started_at": "now"}, {"name": ""},
                                  {"email": "", "whatsapp": ""}, {"email": "nope"}, {"message": "x" * 1001}])
def test_web_form_rejections_are_generic(make_client, over):
    c = make_client(**LEADS)
    if over.get("started_at") == "now":               # submitted 0 s after the page loaded
        over = {"started_at": time.time() * 1000}
    r = web(c, **over)
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "invalid"}
    from app import db
    assert db.count("contact_requests") == 0


@respx.mock
def test_web_form_rate_limit(make_client):
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(**LEADS)
    codes = [web(c, ip="9.9.9.9", email=f"a{i}@acme.mx").status_code for i in range(6)]
    assert codes == [200] * 5 + [429]
    assert web(c, ip="8.8.8.8", email="z@acme.mx").status_code == 200


def test_web_form_disabled_with_leads_off(make_client):
    c = make_client(LEADS_ENABLED="false")
    assert web(c).status_code == 404
