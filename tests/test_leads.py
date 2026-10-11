"""Leads, WhatsApp side: lead creation, consent, opt-out, profiling, scoring, stages,
the "Que Alan me contacte" form and the LEADS_ENABLED flag."""
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.flows import quiz
from tests.test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from tests.test_flows import FakeMessages, _resp, _text, _tool, meta_payload

USER = "523312345678"
META_UPLOAD = "https://graph.facebook.com/v20.0/111/media"
LEADS = {"LEADS_ENABLED": "true", "FLOWS_ENABLED": "true", "USER_MSG_PER_MINUTE": "100"}
N = len(quiz.QUESTIONS)


def client(make_client, **kw):
    return make_client(**{**LEADS, **kw})


def mocks():
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(META_UPLOAD).mock(return_value=httpx.Response(200, json={"id": "M1"}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    return meta, slack


_n = 0


def say(c, text=None, reply_id=None, title=""):
    global _n
    _n += 1
    c.post("/webhooks/meta", json=meta_payload(f"wamid.t{_n}", text, reply_id=reply_id, title=title or text or "x"))


def sent(route):
    out = []
    for call in route.calls:
        m = json.loads(call.request.content)
        t = m.get("type")
        if t == "text":
            out.append(("text", m["text"]["body"], []))
        elif t == "interactive":
            i = m["interactive"]
            ids = [b["reply"]["id"] for b in i["action"]["buttons"]] if i["type"] == "button" \
                else [r["id"] for r in i["action"]["sections"][0]["rows"]]
            out.append((i["type"], i["body"]["text"], ids))
        elif t in ("document", "image"):
            out.append((t, m[t].get("filename", ""), []))
        elif t == "template":
            out.append(("template", m["template"]["name"], []))
    return out


def lead():
    from app.leads import core
    return core.by_user(USER)


def acts(type_=None):
    from app.leads import core
    rows = core.activity_rows(lead()["lead_id"])
    return [r for r in rows if type_ is None or r["type"] == type_]


def finish_quiz(c, answer=3):
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")
    for i in range(N):
        say(c, reply_id=f"quiz:{i}:{answer}")


def has_consent_buttons(msgs):
    return any("consent:yes" in ids for _, _, ids in msgs)


# --------------------------------------------------------------------------- #
# Lead creation
# --------------------------------------------------------------------------- #
@respx.mock
def test_no_lead_on_greeting_or_menu(make_client):
    mocks()
    c = client(make_client)
    say(c, "hola")
    say(c, "menu")
    assert lead() is None


@respx.mock
def test_lead_created_on_quiz_start(make_client):
    mocks()
    c = client(make_client)
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")
    row = lead()
    assert row and row["stage"] == "new" and row["wa_profile_name"] == "Ana López"
    assert acts("created")[0]["data"]["reason"] == "quiz_start"


@respx.mock
def test_lead_created_on_first_agent_question(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = client(make_client)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?"))
    assert lead() is not None and acts("created")[0]["data"]["reason"] == "agent_question"


@respx.mock
def test_lead_created_on_handoff_and_consent_asked_first(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "humano")
    msgs = sent(meta)
    assert has_consent_buttons(msgs)
    i = next(n for n, m in enumerate(msgs) if "consent:yes" in m[2])
    assert "Te paso con una persona" in msgs[i + 1][1]          # consent before the handoff reply
    assert lead() and acts("handoff_requested")                 # no business context -> 0 points
    assert lead()["intent_score"] == 0
    # the answer arrives in human mode: recorded silently
    n = len(sent(meta))
    say(c, reply_id="consent:yes", title="Sí, escríbeme")
    assert len(sent(meta)) == n
    assert lead()["consent_followup"] and lead()["contactable"]


@respx.mock
def test_lead_survives_close_and_counts_sessions(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")
    say(c, "salir")                      # quiz exit word -> stops the quiz
    say(c, "adiós")
    from app import sessions
    assert not sessions.is_open(USER)
    assert lead() is not None            # closing wipes history, never the lead


def test_returning_visit_on_another_day(make_client):
    client(make_client)
    from app.leads import core
    t0 = 1_790_000_000.0
    lead_row = core.engage(USER, "quiz_start", opened_at=t0)
    core.touch_session(core.by_user(USER), t0 + 3600)                 # same day: session, no points
    assert core.by_user(USER)["sessions"] == 2
    assert not core.activity_rows(lead_row["lead_id"])[-1]["points"]
    core.touch_session(core.by_user(USER), t0 + 2 * 86400)            # another day: +10
    row = core.by_user(USER)
    assert row["sessions"] == 3
    assert [r["points"] for r in core.activity_rows(row["lead_id"]) if r["type"] == "returning_visit"] == [10]


# --------------------------------------------------------------------------- #
# Quiz -> profiling -> consent
# --------------------------------------------------------------------------- #
@respx.mock
def test_quiz_profiling_name_and_consent(make_client, monkeypatch):
    meta, _ = mocks()
    c = client(make_client)
    from app import docs
    names = []
    real = docs.make_assessment
    monkeypatch.setattr(docs, "make_assessment", lambda r, name=None: names.append(name) or real(r, name))
    finish_quiz(c)
    assert quiz.last_result(USER).level == 2                     # scoring unchanged
    msgs = sent(meta)
    assert msgs[-1][0] == "list" and "profile:role:owner_exec" in msgs[-1][2]
    assert not has_consent_buttons(msgs)                         # never before the profiling ends
    say(c, reply_id="profile:role:owner_exec", title="Dueño(a) / Dir. general")
    assert "profile:size:11-50" in sent(meta)[-1][2]
    say(c, reply_id="profile:size:11-50", title="11–50")
    assert sent(meta)[-1][2] == ["profile:name:skip"]
    say(c, "Ana López, Acme")
    msgs = sent(meta)
    assert any(t == "document" for t, _, _ in msgs[-3:])          # report regenerated
    assert names == ["Ana López", "Ana López, Acme"]              # "Preparado para: Ana López, Acme"
    assert has_consent_buttons(msgs[-1:])
    say(c, reply_id="consent:yes", title="Sí, escríbeme")
    row = lead()
    assert (row["role_level"], row["company_size"], row["name"], row["company"]) == \
        ("owner_exec", "11-50", "Ana López", "Acme")
    assert row["consent_followup"] and row["consent_text_version"] == "2026-10-v1" and row["consent_ts"]
    assert row["fit_score"] == 40 and row["intent_score"] == 10 and row["tier"] == "warm"
    assert row["stage"] == "mql" and row["quiz_level"] == 2 and row["quiz_scores"]["adoption"] == 2
    assert "BAJA" in sent(meta)[-1][1]


@respx.mock
def test_profiling_skips_keep_report_as_today(make_client, monkeypatch):
    meta, _ = mocks()
    c = client(make_client)
    from app import docs
    names = []
    real = docs.make_assessment
    monkeypatch.setattr(docs, "make_assessment", lambda r, name=None: names.append(name) or real(r, name))
    finish_quiz(c, answer=2)
    say(c, reply_id="profile:role:skip", title="Omitir")
    say(c, "omitir")
    say(c, reply_id="profile:name:skip", title="Omitir")
    assert names == ["Ana López"]                                   # as today: WhatsApp name only
    assert has_consent_buttons(sent(meta)[-1:])
    row = lead()
    assert row["role_level"] == "unknown" and row["company_size"] == "unknown" and row["name"] is None
    assert row["quiz_level"] == 1 and row["intent_score"] == 15     # completed + L1 need


@respx.mock
def test_consent_never_mid_quiz_and_free_text_yes(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")
    for i in range(N - 1):
        say(c, reply_id=f"quiz:{i}:3")
        assert not has_consent_buttons(sent(meta))
    say(c, reply_id=f"quiz:{N - 1}:3")
    for rid in ("profile:role:skip", "profile:size:skip", "profile:name:skip"):
        say(c, reply_id=rid, title="Omitir")
    say(c, "sí")
    assert lead()["consent_followup"]
    assert [a["data"]["via"] for a in acts("consent_yes")] == ["text"]


@respx.mock
def test_unrecognised_answer_is_no_answer_and_not_reasked(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "humano")                                   # asked at handoff
    from app import store
    store.end_handoff(USER)                            # Alan hands back to the bot
    say(c, "¿y cuánto dura la plática?")              # not an answer: handled normally
    assert not lead()["consent_followup"] and not lead()["consent_declined_at"]
    say(c, "humano")
    assert sum("consent:yes" in ids for _, _, ids in sent(meta)) == 1   # once per conversation


@respx.mock
def test_no_pauses_30_days_and_max_two_asks(make_client, monkeypatch):
    meta, _ = mocks()
    c = client(make_client)
    from app import sessions, store
    from app.leads import core
    say(c, "humano")
    store.end_handoff(USER)
    say(c, reply_id="consent:no", title="No, gracias")
    assert lead()["consent_declined_at"]

    def new_conversation(days):
        t = core.now() + days * 86400
        monkeypatch.setattr(core, "now", lambda: t)
        sessions.mark_closed(USER, "bot")
        store.end_handoff(USER)
        say(c, "humano")

    new_conversation(10)                                # within 30 days: not asked
    assert sum("consent:yes" in ids for _, _, ids in sent(meta)) == 1
    new_conversation(40)                                # after 30 days: asked again (2nd ask)
    assert sum("consent:yes" in ids for _, _, ids in sent(meta)) == 2
    store.end_handoff(USER)
    say(c, reply_id="consent:no", title="No, gracias")
    new_conversation(80)                                # max 2 asks without a yes
    assert sum("consent:yes" in ids for _, _, ids in sent(meta)) == 2
    assert lead()["consent_asked_count"] == 2


def test_never_asked_in_human_mode_or_mid_form(make_client):
    client(make_client)
    from app import sessions, store
    from app.leads import core, flows
    sessions.touch_user(USER, "meta", USER)
    core.engage(USER, "agent_question")
    store.start_handoff(USER, "slack")
    assert flows.ask_consent(USER, "es", "buying_signal") is None
    store.end_handoff(USER)
    flows.start_contact(USER, "es")
    assert flows.ask_consent(USER, "es", "buying_signal") is None
    flows.clear(USER)
    assert flows.ask_consent(USER, "es", "buying_signal") is not None


# --------------------------------------------------------------------------- #
# Opt-out
# --------------------------------------------------------------------------- #
class NoModel:
    calls = 0

    async def create(self, **kw):
        NoModel.calls += 1
        raise AssertionError("no model call expected")


@pytest.mark.parametrize("word", ["baja", "BAJA", "Stop", "unsubscribe", "no me escriban", "No me contacten.",
                                  "darme de baja"])
@respx.mock
def test_opt_out_keywords_zero_tokens(make_client, word):
    meta, _ = mocks()
    c = client(make_client, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test", GUARD_ENABLED="false")
    from app import agent
    agent.get_agent()._client = SimpleNamespace(messages=NoModel())
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")      # lead exists, quiz active
    say(c, word)
    assert NoModel.calls == 0
    assert "ya no te escribiremos" in sent(meta)[-1][1]
    assert not quiz.active(USER)
    row = lead()
    assert row["opted_out_at"] and row["stage"] == "opted_out" and not row["contactable"]
    from app.leads import core
    assert core.is_suppressed(USER)


@respx.mock
def test_opt_out_in_human_mode_records_confirms_and_warns(make_client):
    meta, slack = mocks()
    c = client(make_client, ZOHO_ENABLED="true")
    say(c, "humano")
    say(c, reply_id="consent:yes", title="Sí, escríbeme")
    say(c, "BAJA")
    assert "ya no te escribiremos" in sent(meta)[-1][1]
    assert lead()["opted_out_at"]
    notes = [json.loads(x.request.content)["text"] for x in slack.calls]
    assert any("modo humano" in n and "BAJA" in n for n in notes)
    from app import db
    assert db.query("SELECT op FROM zoho_outbox WHERE lead_id = ?", (lead()["lead_id"],))[0]["op"] == "upsert"


@respx.mock
def test_opt_out_without_lead_keeps_suppression_and_blocks_consent(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "baja")
    assert lead() is None
    from app.leads import core
    assert core.is_suppressed(USER)
    say(c, reply_id="menu:quiz", title="Diagnóstico de IA")       # a later lead starts opted out
    assert lead()["opted_out_at"] and lead()["stage"] == "opted_out"


def test_erase_keeps_suppression_only_for_opt_outs(make_client):
    client(make_client)
    from app.leads import core
    a = core.engage(USER, "quiz_start")
    core.opt_out(USER)
    core.erase(a["lead_id"])
    assert core.by_user(USER) is None and core.is_suppressed(USER)
    b = core.engage("5215550001111", "quiz_start")
    core.erase(b["lead_id"])
    assert not core.is_suppressed("5215550001111")


# --------------------------------------------------------------------------- #
# Agent tools: invisible, don't end the turn
# --------------------------------------------------------------------------- #
def fake_agent(*responses):
    from app import agent
    fake = FakeMessages(list(responses))
    agent.get_agent()._client = SimpleNamespace(messages=fake)
    return fake


@respx.mock
def test_profile_and_signal_tools_are_invisible(make_client):
    meta, _ = mocks()
    c = client(make_client, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test", GUARD_ENABLED="false")
    fake = fake_agent(
        _resp("tool_use", _tool("update_lead_profile", {
            "name": "Luis Pérez", "company": "Grupo Norte", "role_level": "director", "company_size": "51-250",
            "interest_note": "Quiere un bot de ventas; escríbele a luis@norte.mx o al 55 1234 5678"}, "t1")),
        _resp("tool_use", _tool("flag_buying_signal", {"kind": "wants_help_implementing",
                                                        "note": "implementar IA en ventas"}, "t2")),
        _resp("end_turn", _text("Va, eso se resuelve rediseñando el flujo de ventas.")),
    )
    say(c, "Soy Luis de Grupo Norte, ¿Alan nos ayuda a implementar IA en ventas?")
    assert len(fake.calls) == 3                           # the tools never end the turn
    texts = [t for _, t, _ in sent(meta)]
    assert "Va, eso se resuelve rediseñando el flujo de ventas." in texts
    assert not any("Saved" in t or "Noted" in t for t in texts)
    assert has_consent_buttons(sent(meta)[-1:])          # moment 2: after a buying signal
    row = lead()
    assert row["name"] == "Luis Pérez" and row["role_level"] == "director"
    assert "luis@norte.mx" not in row["interest_note"] and "1234" not in row["interest_note"]
    assert row["tier"] == "hot"                           # wants help implementing -> override
    assert "update_lead_profile" in [t["name"] for t in fake.calls[0]["tools"]]
    assert "never mention them" in fake.calls[0]["system"][0]["text"]


@respx.mock
def test_request_contact_tool_starts_form(make_client):
    meta, _ = mocks()
    c = client(make_client, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test", GUARD_ENABLED="false")
    fake_agent(_resp("tool_use", _tool("request_contact", {})), _resp("end_turn", _text("Va, te pregunto 3 cosas.")))
    say(c, "Me interesa pero ahorita no puedo, que Alan me busque la próxima semana")
    assert sent(meta)[-1][2] == ["contact:ch:whatsapp", "contact:ch:call", "contact:ch:email"]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("profile,fit", [
    ({"role_level": "owner_exec"}, 20), ({"role_level": "director"}, 15), ({"role_level": "manager"}, 8),
    ({"role_level": "individual"}, 0), ({"company_size": "11-50"}, 15), ({"company_size": "51-250"}, 15),
    ({"company_size": "251+"}, 10), ({"company_size": "1-10"}, 5), ({"company": "Acme"}, 5), ({"name": "Ana"}, 5),
    ({"role_level": "owner_exec", "company_size": "11-50", "company": "Acme", "name": "Ana"}, 40),
])
def test_fit_weights(make_client, profile, fit):
    client(make_client)
    from app.leads import scoring
    assert scoring.fit_score(profile) == fit


@pytest.mark.parametrize("kind,points,maxn", [
    ("handoff_business", 25, 1), ("buying_signal", 20, 2), ("quiz_completed", 10, 1), ("quiz_need", 5, 1),
    ("report_requested", 5, 1), ("returning_visit", 10, 3), ("pdf_requested", 3, 2), ("making_of", 3, 1),
])
def test_intent_weights_and_caps(make_client, kind, points, maxn):
    client(make_client)
    from app.leads import core
    lid = core.engage(USER, "x")["lead_id"]
    got = [core.add_intent(lid, kind) for _ in range(maxn + 1)]
    assert got == [points] * maxn + [0]


def test_decay_tiers_override_and_explanations(make_client):
    client(make_client)
    from app.leads import core, scoring
    t = 1_790_000_000.0
    rows = [{"type": "buying_signal", "points": 20, "ts": t}, {"type": "quiz_completed", "points": 10, "ts": t},
            {"type": "fit_change", "points": 20, "ts": t}]
    assert scoring.intent_score(rows, now=t + 10 * 86400) == 30
    assert scoring.intent_score(rows, now=t + 31 * 86400) == 15          # > 30 days: 50%
    assert scoring.intent_score(rows, now=t + 61 * 86400) == 0           # > 60 days: 0
    assert scoring.intent_score([{"type": "buying_signal", "points": 20, "ts": t}] * 5, now=t) == 60   # capped
    assert [scoring.tier_for(s) for s in (59, 60, 29, 30)] == ["warm", "hot", "cold", "warm"]
    assert scoring.tier_for(0, "hot") == "hot"
    lid = core.engage(USER, "x")["lead_id"]
    core.set_profile(lid, role_level="owner_exec", company_size="11-50")
    core.add_intent(lid, "quiz_completed")
    core.add_intent(lid, "handoff_business")
    lead_row = core.recompute(lid)
    assert lead_row["score"] == 70 and lead_row["tier"] == "hot"
    assert sum(p for _, p in scoring.breakdown(lead_row, core.activity_rows(lid))) == lead_row["score"]


def test_scoring_json_override(make_client):
    client(make_client, LEAD_SCORING_JSON='{"intent": {"quiz_completed": {"points": 15}}, "tiers": {"hot": 50}}')
    from app.leads import scoring
    assert scoring.points_for("quiz_completed", 0) == 15 and scoring.tier_for(50) == "hot"


@respx.mock
def test_non_contactable_hot_lead_never_alerted(make_client):
    _, slack = mocks()
    client(make_client)
    from app import leads
    from app.leads import core
    lid = core.engage(USER, "x")["lead_id"]
    core.set_tier_override(lid, "hot", "asked_meeting")
    core.recompute(lid)
    import asyncio
    asyncio.run(leads.effects(USER))
    assert core.get(lid)["tier"] == "hot" and not slack.called
    core.record_consent(lid, True, "v", "test")
    core.recompute(lid)
    asyncio.run(leads.effects(USER))
    asyncio.run(leads.effects(USER))
    assert slack.call_count == 1 and "Lead caliente" in json.loads(slack.calls[0].request.content)["text"]


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
def test_stages_auto_up_to_sql_and_never_over_human(make_client):
    client(make_client)
    from app.leads import core
    lid = core.engage(USER, "x")["lead_id"]
    assert core.recompute(lid)["stage"] == "new"
    core.add_intent(lid, "quiz_completed")
    assert core.recompute(lid)["stage"] == "engaged"
    core.record_consent(lid, True, "v", "t")
    core.set_profile(lid, role_level="owner_exec", company_size="11-50")
    assert core.recompute(lid)["stage"] == "mql"
    core.add_intent(lid, "handoff_business")
    assert core.recompute(lid)["stage"] == "sql"
    core.set_stage(lid, "contacted", "slack")
    core.add_intent(lid, "buying_signal")
    assert core.recompute(lid)["stage"] == "contacted"


def test_zoho_pull_wins_from_contacted(make_client):
    client(make_client)
    from app.leads import core, zoho
    lid = core.engage(USER, "x")["lead_id"]
    core.set_stage(lid, "meeting", "dashboard")
    assert zoho.apply_status(core.get(lid), "Contacted") is None          # Dexter already past it
    assert zoho.apply_status(core.get(lid), "Lost Lead") == "lost"        # Zoho wins >= contacted
    assert core.get(lid)["stage_source"] == "zoho"
    core.update(lid, stage="sql", stage_source="dexter")
    assert zoho.apply_status(core.get(lid), "Contacted") == "contacted"
    assert zoho.apply_status(core.get(lid), "Not Contacted") is None


# --------------------------------------------------------------------------- #
# "Que Alan me contacte" in WhatsApp
# --------------------------------------------------------------------------- #
def run_form(c, *steps):
    for rid, title in steps:
        say(c, reply_id=rid, title=title) if rid else say(c, title)


@respx.mock
def test_menu_4_offers_choice_and_now_hands_off(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "hola")
    say(c, reply_id="menu:human", title="Hablar con Alan")
    assert sent(meta)[-1][2] == ["contact:choice:now", "contact:choice:later"]
    say(c, reply_id="contact:choice:now", title="Hablar con Alan ahora")
    from app import store
    assert store.in_handoff(USER)


@respx.mock
@pytest.mark.parametrize("entry", ["4", "que me llame", "Contáctenme", "call me"])
def test_contact_form_entry_points(make_client, entry):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "hola")
    say(c, entry)
    if entry == "4":
        say(c, "2")
    assert sent(meta)[-1][2] == ["contact:ch:whatsapp", "contact:ch:call", "contact:ch:email"]


@respx.mock
def test_contact_form_confirm_records_everything(make_client):
    meta, slack = mocks()
    c = client(make_client)
    say(c, "que me llame")
    run_form(c, ("contact:ch:call", "Llamada"), ("contact:time:afternoon", "Tarde"),
             ("contact:topic:skip", "Omitir"), (None, "Ana López, Acme"))
    assert sent(meta)[-1][2] == ["contact:confirm:yes", "contact:confirm:fix"]
    assert "Llamada" in sent(meta)[-1][1] and "Tarde" in sent(meta)[-1][1]
    run_form(c, ("contact:confirm:yes", "Confirmar"))
    assert "Alan te contactará por Llamada por la tarde" in sent(meta)[-1][1]
    from app.leads import core
    row = lead()
    reqs = core.open_requests(row["lead_id"])
    assert [(r["channel"], r["best_time"], r["topic"]) for r in reqs] == [("call", "afternoon", None)]
    assert row["consent_followup"] and row["consent_text_version"] == "wa-form-2026-10-v1"
    assert row["tier"] == "hot" and row["stage"] == "sql" and row["company"] == "Acme"
    alerts = [json.loads(x.request.content)["text"] for x in slack.calls if "Lead caliente" in json.loads(x.request.content)["text"]]
    assert len(alerts) == 1 and "Solicitud de contacto" in alerts[0]
    from app import store
    assert not store.in_handoff(USER)                    # the bot stays in control


@respx.mock
def test_contact_form_corregir_and_email_retry(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "contact me")
    run_form(c, ("contact:ch:whatsapp", "WhatsApp"), ("contact:time:skip", "Skip"), ("contact:topic:talk", "x"),
             (None, "Ana López, Acme"), ("contact:confirm:fix", "Edit"))
    assert sent(meta)[-1][2][0] == "contact:ch:whatsapp"            # back to step 1
    run_form(c, ("contact:ch:email", "Email"), ("contact:time:any", "x"), ("contact:topic:skip", "x"))
    say(c, "no-es-correo")
    assert "no parece válido" in sent(meta)[-1][1]
    say(c, "ana@acme.mx")
    run_form(c, ("contact:confirm:yes", "Confirm"))
    from app.leads import core
    assert core.open_requests(lead()["lead_id"])[0]["channel"] == "email" and lead()["email"] == "ana@acme.mx"


@respx.mock
def test_abandoning_the_form_saves_nothing(make_client):
    meta, slack = mocks()
    c = client(make_client)
    say(c, "que me llame")
    run_form(c, ("contact:ch:call", "Llamada"))
    say(c, "adiós")                                       # closes the chat mid-form
    from app.leads import core
    assert not core.open_requests() and not lead()["consent_followup"]
    say(c, "hola")                                        # next conversation: the menu, no form
    assert "contact:ch:call" not in sent(meta)[-1][2]


@respx.mock
def test_contact_form_not_offered_in_human_mode(make_client):
    meta, _ = mocks()
    c = client(make_client)
    say(c, "humano")
    n = len(sent(meta))
    say(c, "que me llame")
    assert len(sent(meta)) == n


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
@respx.mock
def test_leads_disabled_is_legacy(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true", LEADS_ENABLED="false")
    twilio_post(c, tw_msg("baja", sid="S1"))
    twilio_post(c, tw_msg("4", sid="S2"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert "echo: baja" in bodies                         # just a message to the agent
    from app import db, store
    assert store.in_handoff(USER)                         # menu 4 hands off directly
    assert db.count("leads") == 0
