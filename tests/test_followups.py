"""Pendientes de seguimiento (app/leads/followups.py, the dashboard view): who is on the list, marking
someone contacted, the dashboard's login and CSRF protection, the wa.me draft, the Slack digest line, and the
migration that backfills the three-moment answers."""
import sqlite3
from urllib.parse import unquote

from app import db
from app.analytics import events
from app.flows import welcome
from app.leads import consent as lead_consent
from app.leads import core, followups
from fastapi.testclient import TestClient

from tests.test_consent_welcome import CONSENT
from tests.test_leads_ops import csrf, form, login

PASSWORD = "correct-horse-battery-staple"  # 16+ characters, as the dashboard requires
DASH = {**CONSENT, "DASHBOARD_ENABLED": "true", "DASHBOARD_PASSWORD": PASSWORD}
DAY = 86400


def _dash(make_client):
    """The dashboard sets a Secure cookie: the client must be https, as in test_leads_ops."""
    from app.main import app
    make_client(**DASH)
    return TestClient(app, base_url="https://testserver")


def _lead(uid, ctype, *, name=None, lang="es", first_seen=None, quiz=None, email=None):
    lead, _ = core.ensure(uid, profile_name=name, lang=lang, reason="test")
    lid = lead["lead_id"]
    if ctype in ("tacit_notice", "explicit", "declined"):
        core.set_consent(lid, ctype, version=welcome.CONSENT_TEXT_VERSION)
    if quiz is not None:
        core.update(lid, quiz_level=quiz)
    if email:
        core.update(lid, email=email)
    if first_seen is not None:
        core.update(lid, first_seen=first_seen)
    return core.by_user(uid)


def test_the_pending_list_has_explicit_and_tacit_leads_only_oldest_first(make_client):
    make_client(**CONSENT)
    now = core.now()
    _lead("521000000001", "explicit", name="Ana", first_seen=now - 5 * DAY, quiz=2, email="a@x.com")
    _lead("521000000002", "tacit_notice", lang="en", first_seen=now - 3 * DAY)
    _lead("521000000003", "declined", first_seen=now - 4 * DAY)
    _lead("521000000004", None, first_seen=now - 1 * DAY)        # never shown the welcome
    _lead("521000000005", "explicit", first_seen=now - 6 * DAY)
    core.opt_out("521000000005")                                 # ...then opted out
    contacted = _lead("521000000006", "explicit", first_seen=now - 7 * DAY)
    followups.mark_contacted(contacted["lead_id"], "call")
    rows = followups.pending()
    assert [r["user_id"] for r in rows] == ["521000000001", "521000000002"]
    assert followups.digest_line() == "Pendientes de seguimiento: 2 (más antiguo: 5 días)"


def test_an_opt_out_is_not_on_the_list_and_an_empty_list_has_no_digest_line(make_client):
    make_client(**CONSENT)
    _lead("521000000007", "explicit")
    core.opt_out("521000000007")
    assert followups.pending() == []
    assert followups.digest_line() is None                       # N = 0: no line in the digest


def test_marking_contacted_sets_status_channel_time_and_a_scrubbed_note(make_client):
    make_client(**CONSENT)
    lead = _lead("521000000008", "explicit", name="Ana")
    before = core.now()
    out = followups.mark_contacted(lead["lead_id"], "call", "Le llamé  el lunes a@x.com")
    assert out["followup_status"] == "contacted" and out["contacted_channel"] == "call"
    assert out["contacted_at"] >= before
    assert out["followup_note"] == "Le llamé el lunes [email]"   # the address is scrubbed out of the note
    assert out["stage"] == "contacted"
    assert followups.pending() == []
    events.drain()
    assert db.query("SELECT COUNT(*) AS n FROM events WHERE type = 'followup_marked'")[0]["n"] == 1


def test_an_unknown_channel_is_recorded_as_other_and_a_contacted_lead_is_refused(make_client):
    make_client(**CONSENT)
    lead = _lead("521000000009", "tacit_notice")
    assert followups.mark_contacted(lead["lead_id"], "carrier pigeon")["contacted_channel"] == "other"
    assert followups.mark_contacted(lead["lead_id"], "call") is None   # already contacted


def test_the_dashboard_lists_the_follow_ups_first_and_marks_one_contacted(make_client):
    c = _dash(make_client)
    lead = _lead("521000000010", "tacit_notice", name="Ana", first_seen=core.now() - 2 * DAY)
    assert login(c, PASSWORD).status_code == 303
    page = c.get("/dashboard").text
    assert "Pendientes de seguimiento (1)" in page and "wa.me/521000000010" in page
    assert page.index("Pendientes de seguimiento") < page.index("Solicitudes de contacto")
    assert "Plantilla Dexter" not in page                        # tacit notice: no Dexter template action
    r = form(c, f"/dashboard/followups/{lead['lead_id']}/contacted", csrf=csrf(c),
             channel="personal_whatsapp", note="hola")
    assert r.status_code == 303
    row = core.get(lead["lead_id"])
    assert row["followup_status"] == "contacted" and row["contacted_channel"] == "personal_whatsapp"
    assert followups.pending() == []


def test_an_explicit_lead_on_the_list_also_gets_the_dexter_template_action(make_client):
    c = _dash(make_client)
    _lead("521000000011", "explicit", name="Ana", email="ana@x.com")
    login(c, PASSWORD)
    page = c.get("/dashboard").text
    assert "Plantilla Dexter" in page and "mailto:ana@x.com" in page


def test_the_follow_up_endpoint_needs_the_login_and_the_csrf_token(make_client):
    c = _dash(make_client)
    lid = _lead("521000000012", "tacit_notice")["lead_id"]
    r = form(c, f"/dashboard/followups/{lid}/contacted", csrf="x", channel="call")   # not logged in
    assert r.status_code == 303 and core.get(lid)["followup_status"] == "pending"
    login(c, PASSWORD)
    r = form(c, f"/dashboard/followups/{lid}/contacted", csrf="wrong", channel="call")
    assert r.status_code == 403 and core.get(lid)["followup_status"] == "pending"
    r = form(c, f"/dashboard/followups/{lid}/contacted", csrf=csrf(c), channel="call")
    assert r.status_code == 303 and core.get(lid)["followup_status"] == "contacted"
    r = form(c, f"/dashboard/followups/{lid}/contacted", csrf=csrf(c), channel="call")
    assert r.status_code == 409                                  # no longer on the list


def test_the_wa_me_draft_is_in_the_leads_language_and_url_encoded():
    en = {"user_id": "521000000013", "lang": "en", "name": "Ana López", "quiz_level": None}
    link = followups.wa_link(en)
    assert link.startswith("https://wa.me/521000000013?text=")
    assert " " not in link and "¿" not in link                    # encoded, as a link must be
    assert unquote(link.split("?text=", 1)[1]) == (
        "Hi Ana, this is Alan, thanks for chatting with Dexter at the AI Governance talk. How did the talk go?")
    es = {"user_id": "521000000014", "lang": "es", "name": None, "wa_profile_name": "Juan", "quiz_level": 2}
    assert unquote(followups.wa_link(es).split("?text=", 1)[1]) == (
        "Hola Juan, soy Alan, gracias por escribirle a Dexter en la plática de Gobernanza de IA. "
        "¿Cómo te fue con el diagnóstico?")
    anon = {"user_id": "521000000015", "lang": "es", "name": None, "wa_profile_name": "🙂", "quiz_level": None}
    assert unquote(followups.wa_link(anon).split("?text=", 1)[1]).startswith("Hola, soy Alan, gracias")


def test_the_mailto_link_has_the_address_and_a_subject_in_the_leads_language():
    assert followups.mailto({"email": "ana@x.com", "lang": "en"}).startswith(
        "mailto:ana@x.com?subject=AI%20Governance%20talk")
    assert followups.mailto({"email": None}) is None


def test_migration_5_backfills_only_real_answers(tmp_path, monkeypatch):
    full = db.MIGRATIONS
    conn = sqlite3.connect(str(tmp_path / "old.sqlite3"), isolation_level=None)  # as app/db.py opens it
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db, "MIGRATIONS", full[:4])             # the database as it was before this change
    db.migrate(conn)
    t = 1_000.0
    legacy = [("L1", "5210000000a1", {"consent_followup": 1}),        # said yes to the three-moment ask
              ("L2", "5210000000a2", {"consent_declined_at": t}),     # said no
              ("L3", "5210000000a3", {"opted_out_at": t}),            # BAJA
              ("L4", "5210000000a4", {"consent_asked_count": 1})]     # asked, no answer
    for lid, uid, extra in legacy:
        cols = {"lead_id": lid, "user_id": uid, "created_at": t, "updated_at": t, **extra}
        conn.execute(f"INSERT INTO leads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(cols.values()))
    monkeypatch.setattr(db, "MIGRATIONS", full)                 # now apply migration 5
    db.migrate(conn)
    got = {r["lead_id"]: (r["consent_type"], r["followup_status"]) for r in
           conn.execute("SELECT lead_id, consent_type, followup_status FROM leads")}
    assert got["L1"] == ("explicit", "pending")
    assert got["L2"] == ("declined", "not_needed")
    assert got["L3"] == ("opted_out", "not_needed")
    assert got["L4"] == (None, None)


def test_a_tacit_notice_is_on_the_list_but_not_contactable_and_an_explicit_yes_is_both(make_client):
    make_client(**CONSENT)
    tacit = _lead("521000000016", "tacit_notice")
    assert core.contactable_personal(tacit) and not tacit["contactable"]
    explicit = _lead("521000000017", "explicit")
    assert core.contactable_personal(explicit) and explicit["contactable"]
    assert lead_consent.enabled() is True


def test_becoming_explicit_marks_the_lead_for_the_zoho_outbox(make_client):
    make_client(**CONSENT)
    lead = _lead("521000000018", "tacit_notice")
    core.update(lead["lead_id"], zoho_dirty=0)
    core.set_consent(lead["lead_id"], "explicit", version=welcome.CONSENT_TEXT_VERSION)
    row = core.get(lead["lead_id"])
    assert row["contactable"] and row["zoho_dirty"]
