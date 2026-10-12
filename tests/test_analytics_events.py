"""Event log (app/analytics/events.py): hook points, conversation ids, close bookkeeping,
Meta statuses, entry sources, and that analytics never breaks a reply."""
import asyncio
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import db, sessions, store
from app.analytics import events

from .test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from .test_flows import meta_payload

USER = "523312345678"
WA = "5213312345678"
FLOWS = {"FLOWS_ENABLED": "true"}
CLAUDE = {**FLOWS, "AGENT_MODE": "claude", "ANTHROPIC_API_KEY": "sk-test"}
META_UPLOAD = "https://graph.facebook.com/v20.0/111/media"


def evs(type_=None, user=USER):
    """Events written so far (oldest first), data parsed."""
    events.drain()
    sql, params = "SELECT * FROM events WHERE user_id = ?", [user]
    if type_:
        sql += " AND type = ?"
        params.append(type_)
    out = []
    for r in db.query(sql + " ORDER BY id", params):
        d = dict(r)
        d["data"] = json.loads(d.pop("data_json"))
        out.append(d)
    return out


def types():
    return [e["type"] for e in evs()]


def status_payload(wamid, status, code=None):
    st = {"id": wamid, "status": status, "timestamp": "1", "recipient_id": WA}
    if code:
        st["errors"] = [{"code": code, "title": "x"}]
    return {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [st]}}]}]}


class FakeLLM:
    def __init__(self, steps):
        self.steps, self.calls = list(steps), []

    async def create(self, **kw):
        self.calls.append(kw)
        step = self.steps.pop(0) if self.steps else {"text": "Respuesta."}
        content = [SimpleNamespace(type="text", text=step["text"])] if step.get("text") else []
        if step.get("tool"):
            content.append(SimpleNamespace(type="tool_use", id=f"tu{len(self.calls)}", name=step["tool"],
                                           input=step.get("input", {})))
        return SimpleNamespace(stop_reason="tool_use" if step.get("tool") else "end_turn", content=content,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                                     cache_read_input_tokens=9000, cache_creation_input_tokens=0))


def install(agent_steps=(), guard="IN"):
    from app import agent, guard as g
    llm = FakeLLM(agent_steps)
    agent.get_agent()._client = SimpleNamespace(messages=llm)
    gl = FakeLLM([{"text": guard}] * 20)
    gd = g.Guard()
    gd._client = SimpleNamespace(messages=gl)
    g._guard = gd
    return llm, gl


@pytest.fixture
def tw():
    with respx.mock(assert_all_called=False) as router:
        router.post(SLACK).mock(return_value=httpx.Response(200))
        yield router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))


@pytest.fixture
def meta():
    ids = itertools.count(1)
    with respx.mock(assert_all_called=False) as router:
        router.post(SLACK).mock(return_value=httpx.Response(200))
        router.post(META_UPLOAD).mock(return_value=httpx.Response(200, json={"id": "MEDIA"}))
        yield router.post(META_SEND).mock(
            side_effect=lambda req: httpx.Response(200, json={"messages": [{"id": f"wamid.{next(ids)}"}]}))


# --------------------------------------------------------------------------- #
# Hook points
# --------------------------------------------------------------------------- #
def test_menu_and_quiz_events(make_client, tw):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("2", sid="S2"))
    for i in range(9):
        twilio_post(c, tw_msg("3", sid=f"Q{i}"))
    t = types()
    assert t[:3] == ["conversation_start", "menu_shown", "user_msg"]
    start = evs("conversation_start")[0]["data"]
    assert start == {"source": "organic", "lang": "es", "channel": "twilio"}
    assert evs("menu_choice")[0]["data"]["option"] == "menu:quiz"
    assert [e["data"]["q"] for e in evs("quiz_answer")] == list(range(1, 10))
    assert evs("quiz_complete")[0]["data"]["level"] == 2
    assert evs("quiz_start")[0]["step"] == "quiz_q1"
    assert evs("quiz_answer")[2]["step"] == "quiz_q4"         # after answering Q3, Q4 is on screen
    # no answer text anywhere in the event log
    assert all("text" not in e["data"] and "body" not in e["data"] for e in evs())


def test_quiz_exit_event(make_client, tw):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("1", sid="S2"))
    twilio_post(c, tw_msg("salir", sid="S3"))
    assert evs("quiz_exit")[0]["data"]["q"] == 2


def test_agent_turn_tools_and_documents(make_client, meta):
    c = make_client(**CLAUDE)
    install([{"tool": "send_slides", "input": {"slide": "8"}}, {"text": "Te mando la lámina 8."}])
    c.post("/webhooks/meta", json=meta_payload("m1", "¿Me mandas la lámina del diagnóstico de 7 dominios?"))
    turn = evs("agent_turn")[0]
    assert turn["step"] == "agent_turn"
    assert turn["data"]["outcome"] == "answered" and turn["data"]["tools"] == ["send_slides"]
    assert turn["data"]["steps"] == 2 and turn["data"]["input_tokens"] == 2 * 9100
    assert turn["data"]["latency_ms"] >= 0
    sent = evs("slides_sent")[0]
    assert sent["data"] == {"pages": 1, "slide": "8", "format": "image"} and sent["step"] == "slides"


def test_report_and_topic_pdf_events(make_client, meta):
    from app.flows import quiz
    c = make_client(**CLAUDE)
    install([{"tool": "create_topic_pdf", "input": {
        "language": "es", "title": "Regla 10-20-70", "summary": "Resumen corto.",
        "sections": [{"heading": "Idea", "paragraphs": ["El 70% es personas y procesos."]}],
        "sources": ["BCG, 2024"]}}, {"text": "Va el PDF."}])
    c.post("/webhooks/meta", json=meta_payload("m1", "Hazme un PDF de una página sobre la regla 10-20-70"))
    assert evs("topic_pdf_sent")[0]["data"]["pages"] >= 1
    quiz._results[USER] = quiz.score([2] * 9)
    c.post("/webhooks/meta", json=meta_payload("m2", "mi reporte"))
    assert evs("report_sent")[0]["step"] == "report"


def test_file_in_event(make_client, meta):
    c = make_client(**FLOWS)
    with respx.mock(assert_all_called=False) as r:
        r.get("https://graph.facebook.com/v20.0/IMG").mock(return_value=httpx.Response(
            200, json={"url": "https://x.test/f", "mime_type": "video/mp4", "file_size": 10}))
        payload = {"entry": [{"changes": [{"field": "messages", "value": {"messages": [
            {"from": WA, "id": "v1", "type": "video", "video": {"id": "IMG", "mime_type": "video/mp4"}}]}}]}]}
        c.post("/webhooks/meta", json=payload)
    e = evs("file_in")[0]["data"]
    assert e == {"kind": "video", "accepted": False, "reject_reason": "unsupported"}


def test_offtopic_warning_and_handoff_reasons(make_client, tw):
    c = make_client(**CLAUDE)
    install(guard="OUT")
    for i in range(4):  # the first message is welcome + menu (no warning); then two warnings, then Alan
        twilio_post(c, tw_msg("dame una receta de enchiladas suizas por favor", sid=f"S{i}"))
    assert [e["data"]["n"] for e in evs("offtopic_warning")] == [1, 2]
    assert evs("handoff")[0]["data"]["reason"] == "offtopic"


def test_user_asked_handoff_and_slack_take_release(make_client, tw):
    from app import slack_bridge
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("humano", sid="S1"))
    assert evs("handoff")[0]["data"]["reason"] == "user_asked"
    store.end_handoff(USER)
    asyncio.run(slack_bridge.take_control(USER, "UOWNER"))
    asyncio.run(slack_bridge.release_control(USER))
    assert types()[-2:] == ["human_take", "human_release"]


def test_limit_hit_event(make_client, tw):
    c = make_client(**FLOWS, USER_MSG_PER_MINUTE="1")
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("menu", sid="S2"))
    assert evs("limit_hit")[0]["data"]["limit"] == "minute"


def test_agent_error_event_handoff_and_gap(make_client, tw):
    c = make_client()   # echo agent: "fail" raises
    twilio_post(c, tw_msg("fail", sid="S1"))
    assert evs("error")[0]["data"] == {"kind": "RuntimeError", "where": "agent"}
    assert evs("handoff")[0]["data"]["reason"] == "agent_error"
    assert evs("agent_turn")[0]["data"]["outcome"] == "error"
    assert evs("gap")[0]["data"]["source"] == "handoff"


# --------------------------------------------------------------------------- #
# Conversation id, close bookkeeping, restarts
# --------------------------------------------------------------------------- #
def test_conversation_id_rotates_on_close_and_survives_restart(make_client, tw):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    cid = sessions.conversation_id(USER)
    assert cid and len(cid) == 12
    sessions._sessions.clear()                     # simulated restart: memory gone, DB kept
    sessions.load_all()
    assert sessions.conversation_id(USER) == cid
    twilio_post(c, tw_msg("menu", sid="S2"))
    assert sessions.conversation_id(USER) == cid   # same open conversation
    twilio_post(c, tw_msg("adiós", sid="S3"))
    close = evs("conversation_close")[0]
    assert close["conversation_id"] == cid and close["data"]["reason"] == "user_keyword"
    assert close["data"]["turns"] == 3
    twilio_post(c, tw_msg("hola", sid="S4"))
    assert sessions.conversation_id(USER) != cid
    assert {e["conversation_id"] for e in evs("conversation_start")} == {cid, sessions.conversation_id(USER)}


def _idle_close(monkeypatch, t0):
    from app import main
    monkeypatch.setattr(sessions, "now", lambda: t0 + 10 * 60)
    asyncio.run(main.check_idle())
    monkeypatch.setattr(sessions, "now", lambda: t0 + 16 * 60)
    asyncio.run(main.check_idle())


def test_idle_close_last_step_and_read_receipt(make_client, meta, monkeypatch):
    c = make_client(**FLOWS)
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    c.post("/webhooks/meta", json=meta_payload("m0", reply_id="menu:quiz", title="Diagnóstico"))
    for i in range(3):
        c.post("/webhooks/meta", json=meta_payload(f"m{i + 1}", reply_id=f"quiz:{i}:2", title="x"))
    last = sessions.get(USER)["last_bot_wamid"]
    assert sessions.get(USER)["last_step"] == "quiz_q4"
    c.post("/webhooks/meta", json=status_payload(last, "delivered"))
    c.post("/webhooks/meta", json=status_payload(last, "read"))
    _idle_close(monkeypatch, t0)
    close = evs("conversation_close")[0]["data"]
    assert close["reason"] == "idle" and close["last_step"] == "quiz_q4"
    assert close["last_bot_msg_read"] is True
    assert [e["data"]["status"] for e in evs("msg_status")] == ["delivered", "read"]
    assert evs("msg_status")[0]["conversation_id"] == evs("conversation_close")[0]["conversation_id"]


def test_idle_close_unread_meta(make_client, meta, monkeypatch):
    c = make_client(**FLOWS)
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    c.post("/webhooks/meta", json=meta_payload("m0", "hola"))
    _idle_close(monkeypatch, t0)
    assert evs("conversation_close")[0]["data"]["last_bot_msg_read"] is False    # no read status came


def test_idle_close_unknown_on_twilio(make_client, tw, monkeypatch):
    c = make_client(**FLOWS)
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    twilio_post(c, tw_msg("hola", sid="S1"))
    _idle_close(monkeypatch, t0)
    close = evs("conversation_close")[0]["data"]
    assert close["last_bot_msg_read"] is None and close["last_step"] == "menu"   # Twilio: no receipts


def test_idle_after_answer_is_a_gap(make_client, meta, monkeypatch):
    c = make_client(**CLAUDE)
    install([{"text": "La regla 10-20-70 es de BCG."}])
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    c.post("/webhooks/meta", json=meta_payload("m1", "¿Cómo aplico la regla 10-20-70 en una empresa de 30 personas?"))
    for wamid in [sessions.get(USER)["last_bot_wamid"]]:
        c.post("/webhooks/meta", json=status_payload(wamid, "read"))
    _idle_close(monkeypatch, t0)
    gap = evs("gap")[0]
    assert gap["data"]["source"] == "idle_after_answer"
    assert gap["data"]["sample_id"] == sessions.get(USER)["last_sample_id"]


def test_statuses_never_reach_handle_inbound(make_client, meta, monkeypatch):
    from app import main
    c = make_client(**FLOWS)
    called = []

    async def spy(channel, msg):
        called.append(msg)
    monkeypatch.setattr(main, "handle_inbound", spy)
    c.post("/webhooks/meta", json=status_payload("wamid.X", "failed", code=131047))
    assert called == []
    st = evs("msg_status")[0]["data"]
    assert st["status"] == "failed" and st["error_code"] == 131047


@pytest.mark.parametrize("text,source", [("Hola Dexter", "qr_talk"), ("hola, dexter!", "qr_talk"),
                                         ("HOLA DÉXTER", "qr_talk"), ("hola", "organic")])
def test_entry_sources_map_the_qr_prefill(make_client, tw, text, source):
    c = make_client(**FLOWS, ENTRY_SOURCES='{"hola dexter": "qr_talk"}')
    twilio_post(c, tw_msg(text, sid="S1"))
    assert evs("conversation_start")[0]["data"]["source"] == source


def test_qr_prefill_still_gets_the_normal_greeting(make_client, tw):
    c = make_client(**FLOWS, ENTRY_SOURCES='{"hola dexter": "qr_talk"}')
    twilio_post(c, tw_msg("Hola Dexter", sid="S1"))
    body = dict(httpx.QueryParams(tw.calls[-1].request.content.decode()))["Body"]
    assert "¿Qué quieres hacer?" in body                  # the welcome menu, like "hola"
    assert evs("user_msg")[0]["data"]["route"] == "menu"


def test_user_msg_has_route_topic_and_no_text(make_client, tw):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("5", sid="S2"))
    msgs = evs("user_msg")
    assert [(m["data"]["route"], m["data"]["topic"]) for m in msgs] == [("menu", "saludo_menu"),
                                                                       ("file_reply", "laminas")]
    assert all(set(m["data"]) == {"topic", "route", "lang", "kind"} for m in msgs)


# --------------------------------------------------------------------------- #
# Never break a reply; flag off = legacy
# --------------------------------------------------------------------------- #
def test_record_failures_do_not_break_replies(make_client, tw, monkeypatch):
    c = make_client(**FLOWS)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(events, "iso", boom)
    monkeypatch.setattr(events, "_submit", boom)
    twilio_post(c, tw_msg("hola", sid="S1"))
    assert "¿Qué quieres hacer?" in dict(httpx.QueryParams(tw.calls[-1].request.content.decode()))["Body"]


def test_db_write_failure_is_swallowed(make_client, tw, monkeypatch):
    c = make_client(**FLOWS)
    real = db.execute

    def flaky(sql, params=()):
        if sql.startswith("INSERT INTO events"):
            raise RuntimeError("locked")
        return real(sql, params)
    monkeypatch.setattr(db, "execute", flaky)
    twilio_post(c, tw_msg("hola", sid="S1"))
    events.drain()
    assert len(tw.calls) == 1


def test_analytics_disabled_writes_nothing(make_client, tw):
    from app import agent
    c = make_client(**CLAUDE, ANALYTICS_ENABLED="false")
    llm, gl = install([{"text": "Ok."}])
    twilio_post(c, tw_msg("¿Por qué 3 de cada 4 empresas no ven valor en la IA?", sid="S1"))
    events.drain()
    for table in ("events", "question_samples", "llm_calls"):
        assert db.count(table) == 0
    assert "note_gap" not in [t["name"] for t in agent.tool_definitions()]
    assert "note_gap" not in llm.calls[0]["system"][0]["text"]
    assert "IN or OUT." in gl.calls[0]["system"] and "on_topic" not in gl.calls[0]["system"]
    assert gl.calls[0]["max_tokens"] == 5


def test_on_request_disclosed_event_and_topic(make_client, tw):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("¿Cómo se hizo Dexter?", sid="S1"))
    d = evs("on_request_disclosed")
    assert len(d) == 1 and d[0]["data"] == {"id": "dexter-making-of"} and d[0]["step"] == "on_request"
    m = evs("user_msg")[0]["data"]
    assert (m["route"], m["topic"]) == ("on_request", "making_of")
