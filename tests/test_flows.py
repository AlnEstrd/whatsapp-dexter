"""Talk-companion flows: menu, AI-maturity quiz, KB agent tools."""
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import knowledge
from app.flows import menu, quiz

from .test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post

N = len(quiz.QUESTIONS)


# --------------------------------------------------------------------------- #
# Pure units
# --------------------------------------------------------------------------- #
def test_quiz_content_fits_whatsapp_limits():
    for q in quiz.QUESTIONS:
        for lang in ("es", "en"):
            opts = q["opts"][lang]
            assert len(opts) == 4
            for title, desc in opts:
                assert len(title) <= 24, title
                assert len(desc) <= 72, desc
    for lang in ("es", "en"):
        for _, title, desc in menu.MENU_OPTIONS[lang]:
            assert len(title) <= 24 and len(desc) <= 72
        st = quiz.QuizState(lang=lang)
        for i in range(N):
            st.index = i
            assert len(quiz._question(st).text) <= 1024
        assert len(menu.welcome(lang, "Ana López").text) <= 1024
    assert set(quiz.TIPS) == {q["key"] for q in quiz.QUESTIONS}


@pytest.mark.parametrize("answers,level,capped", [
    ([0] * 9, 0, False),
    ([1] * 9, 1, False),
    ([2] * 9, 2, False),
    ([3] * 9, 3, False),
    ([3, 3, 3, 1, 3, 3, 3, 3, 3], 1, True),   # leadership weak -> capped at L1
    ([3, 3, 3, 3, 0, 3, 3, 3, 3], 1, True),   # ownership weak -> capped at L1
    ([2, 3, 3, 3, 3, 2, 3, 3, 3], 2, False),  # avg 2.78 < 2.8 -> still L2
    ([3, 3, 3, 3, 3, 2, 3, 3, 3], 3, False),  # avg 2.89 -> L3
])
def test_quiz_scoring(answers, level, capped):
    r = quiz.score(answers)
    assert (r.level, r.capped) == (level, capped)


def test_quiz_result_render_has_level_and_tips():
    r = quiz.score([0, 1, 2, 3, 1, 2, 0, 1, 2], "es")
    text = quiz.render(r)
    assert "L1" in text and "Por dominio" in text
    assert quiz.TIPS["adoption"]["es"] in text      # weakest first
    assert len(r.weakest) == 3


def test_detect_lang():
    assert menu.detect_lang("What is the 10-20-70 rule?") == "en"
    assert menu.detect_lang("¿Qué es la regla 10-20-70?") == "es"
    assert menu.detect_lang("ok", "en") == "en"


def test_knowledge_core_and_search():
    core = knowledge.core_text()
    assert "Rol = responsabilidades + actividades" in core and "Sobre Alan" in core
    assert "dora-2025" in knowledge.search_sources("postura clara comunicada sobre la IA")


# --------------------------------------------------------------------------- #
# End to end through the webhooks
# --------------------------------------------------------------------------- #
def meta_payload(mid, text=None, reply_id=None, title=""):
    if reply_id:
        msg = {"from": "5213312345678", "id": mid, "type": "interactive",
               "interactive": {"type": "list_reply", "list_reply": {"id": reply_id, "title": title}}}
    else:
        msg = {"from": "5213312345678", "id": mid, "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": "5213312345678", "profile": {"name": "Ana López"}}],
        "messages": [msg]}}]}]}


def meta_sends(route):
    out = [json.loads(c.request.content) for c in route.calls]
    return [m for m in out if m.get("type") in ("text", "interactive")]


@respx.mock
def test_greeting_shows_interactive_menu_on_meta(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    make_client(FLOWS_ENABLED="true").post("/webhooks/meta", json=meta_payload("w1", "Hola"))
    sent = meta_sends(meta)[-1]
    assert sent["type"] == "interactive"
    assert "Ana" in sent["interactive"]["body"]["text"]
    ids = [r["id"] for r in sent["interactive"]["action"]["sections"][0]["rows"]]
    assert ids == ["menu:talk", "menu:quiz", "menu:about", "menu:human", "menu:slides"]


@respx.mock
def test_full_quiz_via_list_replies(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("m0", reply_id="menu:quiz", title="Diagnóstico de IA"))
    for i in range(N):
        c.post("/webhooks/meta", json=meta_payload(f"m{i + 1}", reply_id=f"quiz:{i}:3", title="x"))
    texts = [m["text"]["body"] if m["type"] == "text" else m["interactive"]["body"]["text"]
             for m in meta_sends(meta)]
    assert texts[0].startswith("📊 *Diagnóstico")
    assert sum(t.startswith("*") and "/9 ·" in t for t in texts) == N
    assert any("L2 · Sistemática" in t for t in texts)
    assert quiz.last_result("523312345678").level == 2
    assert any("Assessment done" in json.loads(x.request.content)["text"] for x in slack.calls)


@respx.mock
def test_quiz_with_typed_numbers_on_twilio_in_english(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    twilio_post(c, tw_msg("quiz", sid="S0"))
    twilio_post(c, tw_msg("banana", sid="S1"))              # invalid -> repeat question
    for i in range(N):
        twilio_post(c, tw_msg("1", sid=f"S{i + 2}"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert any("número del 1 al 4" in b or "number from 1 to 4" in b for b in bodies)
    assert "L0" in bodies[-2] and quiz.last_result("523312345678").average == 0


@respx.mock
def test_quiz_exit(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(FLOWS_ENABLED="true")
    twilio_post(c, tw_msg("quiz", sid="S0"))
    twilio_post(c, tw_msg("salir", sid="S1"))
    assert not quiz.active("523312345678")
    assert "detuve" in dict(httpx.QueryParams(send.calls.last.request.content.decode()))["Body"]


@respx.mock
def test_menu_talk_to_alan_hands_off(make_client):
    respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    from app import store
    make_client(FLOWS_ENABLED="true").post(
        "/webhooks/meta", json=meta_payload("h1", reply_id="menu:human", title="Hablar con Alan"))
    assert store.handoff_target("523312345678") == "slack"
    assert slack.called


@respx.mock
def test_interactive_failure_falls_back_to_text(make_client):
    def responder(request):
        body = json.loads(request.content)
        return httpx.Response(400, json={"error": "x"}) if body.get("type") == "interactive" \
            else httpx.Response(200, json={})
    meta = respx.post(META_SEND).mock(side_effect=responder)
    make_client(FLOWS_ENABLED="true").post("/webhooks/meta", json=meta_payload("f1", "menu"))
    assert meta_sends(meta)[-1]["type"] == "text"


@respx.mock
def test_first_question_gets_intro_answer_then_menu_offer(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(FLOWS_ENABLED="true")
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S0"))
    twilio_post(c, tw_msg("¿Y quién es Alan?", sid="S1"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert "Dexter" in bodies[0] and "¿Qué quieres hacer?" not in bodies[0]
    assert bodies[1] == "echo: ¿Qué es la regla 10-20-70?"
    assert "*menu*" in bodies[2] and "¿Qué quieres hacer?" not in bodies[2]   # offer, not the menu
    assert bodies[3:] == ["echo: ¿Y quién es Alan?"]                          # only once


# --------------------------------------------------------------------------- #
# Claude agent tool loop (Anthropic client faked)
# --------------------------------------------------------------------------- #
class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(name, inp, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


@respx.mock
def test_agent_start_assessment_tool_starts_quiz(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(FLOWS_ENABLED="true", AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    from app import agent
    a = agent.get_agent()
    fake = FakeMessages([
        _resp("tool_use", _tool("start_assessment", {"language": "es"})),
        _resp("end_turn", _text("¡Va! Empecemos.")),
    ])
    a._client = SimpleNamespace(messages=fake)
    twilio_post(c, tw_msg("quiero medir la madurez de mi empresa"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert "Dexter" in bodies[0]                     # new chat: one-line intro first
    assert bodies[1] == "¡Va! Empecemos."
    assert bodies[2].startswith("📊") and bodies[3].startswith("*1/9")
    assert quiz.active("523312345678")
    sys_blocks = fake.calls[0]["system"]
    assert sys_blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert "Gerente de compras" in sys_blocks[-1]["text"]


@respx.mock
def test_agent_search_sources_tool(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(FLOWS_ENABLED="true", AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    from app import agent
    fake = FakeMessages([
        _resp("tool_use", _tool("search_sources", {"query": "postura clara IA"})),
        _resp("end_turn", _text("Según DORA 2025…")),
    ])
    agent.get_agent()._client = SimpleNamespace(messages=fake)
    twilio_post(c, tw_msg("Hola, ¿qué dice DORA de la política de IA?"))
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "dora-2025" in tool_result["content"]


def test_clean_history_drops_orphans():
    from app.agent import _clean_history
    h = [{"role": "user", "content": [{"type": "tool_result"}]},
         {"role": "assistant", "content": "x"},
         {"role": "user", "content": "hola"}]
    assert _clean_history(h) == [h[2]]
