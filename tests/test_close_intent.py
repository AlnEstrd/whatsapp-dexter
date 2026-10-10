"""Closing intent: keyword fast path, the agent's end_conversation tool, and what must NOT close."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import sessions, store
from app.flows import quiz

from .test_channels import TWILIO_SEND, tw_msg, twilio_post
from .test_slack_bridge import BOT_ENV, SlackMock

USER = "523312345678"
FLOWS = {"FLOWS_ENABLED": "true"}
CLAUDE = {**FLOWS, "AGENT_MODE": "claude", "ANTHROPIC_API_KEY": "sk-test"}
ALGO_MAS = "¿Algo más en lo que te pueda ayudar?"


def bodies(send):
    return [dict(httpx.QueryParams(c.request.content.decode()))["Body"] for c in send.calls]


@pytest.fixture
def send():
    with respx.mock(assert_all_called=False) as router:
        router.post("https://hooks.slack.test/T/B/X").mock(return_value=httpx.Response(200))
        yield router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))


def _text(t):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=t)])


def _end(farewell, chatter="¡Hasta luego! Que te vaya bien."):
    """Model turn that writes its own goodbye AND calls end_conversation (the text must be dropped)."""
    return SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="text", text=chatter),
        SimpleNamespace(type="tool_use", id="tu_1", name="end_conversation",
                        input={"farewell": farewell}),
    ])


class FakeLLM:
    """Stand-in for messages.create. ``decide(user_text, last_bot_text)`` -> response."""

    def __init__(self, decide):
        self.decide = decide
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        msgs = kw["messages"]
        user = msgs[-1]["content"] if isinstance(msgs[-1]["content"], str) else ""
        last_bot = ""
        for m in reversed(msgs[:-1]):
            if m["role"] == "assistant":
                last_bot = "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
                                   for b in m["content"])
                break
        return self.decide(user, last_bot)


def install(decide, guard_says="IN"):
    """Claude-mode agent + guard with fake LLMs; returns (agent_llm, guard_llm)."""
    from app import agent, guard
    llm = FakeLLM(decide)
    agent.get_agent()._client = SimpleNamespace(messages=llm)
    gl = FakeLLM(lambda *_: _text(guard_says))
    g = guard.Guard()
    g._client = SimpleNamespace(messages=gl)
    guard._guard = g
    return llm, gl


def mimic_model(user, last_bot):
    """Rough model policy from the prompt: 'no' after '¿Algo más?' closes, otherwise answer."""
    if user.strip().lower().strip(",.!") in ("no", "no gracias", "no, gracias") and "Algo más" in last_bot:
        return _end("¡Un gusto! Que tengas excelente día. 👋")
    if user.strip().lower() == "gracias":
        return _text(f"¡De nada! {ALGO_MAS}")
    return _text(f"La regla 10-20-70 es de BCG. {ALGO_MAS}")


# --------------------------------------------------------------------------- #
# Keyword fast path (zero tokens)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("closer,lang", [("Sería todo, gracias", "es"), ("es todo", "es"),
                                          ("Nada más 🙏", "es"), ("That's all, thanks", "en"),
                                          ("ya quedó!", "es"), ("I'm good, thanks", "en")])
def test_keyword_closer_closes_without_agent(make_client, send, closer, lang):
    c = make_client(**CLAUDE)
    llm, _ = install(mimic_model)
    twilio_post(c, tw_msg("hola", sid="S1"))
    n, calls = len(send.calls), len(llm.calls)
    twilio_post(c, tw_msg(closer, sid="S2"))
    assert bodies(send)[n:] == [sessions.text("close_user", lang)]      # exactly one farewell
    assert "*hola*" in bodies(send)[-1] or "*hi*" in bodies(send)[-1]
    assert not sessions.is_open(USER)
    assert sessions.get(USER)["closed_reason"] == "user_keyword"
    assert len(llm.calls) == calls                                        # agent not called


@pytest.mark.parametrize("text", ["gracias", "ok", "perfecto", "no", "👍", "no gracias", "listo",
                                  "no cerrar", "gracias, y otra pregunta: ¿qué es ISO 42001?"])
def test_ambiguous_messages_are_not_keyword_closers(text):
    assert not sessions.is_close_request(text) and not sessions.is_done_intent(text)


def test_legacy_goodbyes_and_new_closers():
    assert sessions.is_close_request("Adiós!") and sessions.is_close_request("bye")
    assert not sessions.is_close_request("Sería todo, gracias")       # needs CLOSE_ON_INTENT
    assert sessions.is_done_intent("Sería todo, gracias")


# --------------------------------------------------------------------------- #
# Agent path: end_conversation
# --------------------------------------------------------------------------- #
@respx.mock
def test_agent_end_conversation_sends_one_farewell_and_closes(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    c = make_client(**CLAUDE, **BOT_ENV)
    llm, _ = install(lambda u, b: _end("¡Un gusto! Que tengas excelente día. 👋")
                     if "nos ayudó" in u else _text(f"Es de BCG. {ALGO_MAS}"))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    n, calls = len(send.calls), len(llm.calls)
    twilio_post(c, tw_msg("Perfecto, con esto ya tengo lo que necesitaba, nos ayudó mucho", sid="S2"))

    out = bodies(send)[n:]
    assert out == ["¡Un gusto! Que tengas excelente día. 👋 Cuando quieras, escribe *hola* para empezar de nuevo."]
    assert "Que te vaya bien" not in "".join(bodies(send))          # the agent's free text is dropped
    assert len(llm.calls) == calls + 1                               # no model call after the tool
    assert any(t["name"] == "end_conversation" for t in llm.calls[-1]["tools"])
    assert "end_conversation" in llm.calls[-1]["system"][0]["text"]
    assert not sessions.is_open(USER) and sessions.get(USER)["closed_reason"] == "user_intent"
    assert store.get_history(USER) == [] and store.transcript(USER) == []
    assert any(p["text"] == '🔒 Closed by user: "Perfecto, con esto ya tengo lo que necesitaba, nos ayudó mucho"'
               for p in sm.replies())
    assert not any(p.get("reply_broadcast") for p in sm.replies() if "Closed by user" in p["text"])


def test_agent_close_with_empty_farewell_uses_default_in_english(make_client, send):
    c = make_client(**CLAUDE)
    install(lambda u, b: _end("") if "helpful" in u else _text("Sure."))
    twilio_post(c, tw_msg("what is the 10-20-70 rule", sid="S1"))
    twilio_post(c, tw_msg("ok that was really helpful, I'm heading out now", sid="S2"))
    assert bodies(send)[-1] == sessions.text("close_user", "en")
    assert not sessions.is_open(USER)


@respx.mock
def test_slack_failure_never_blocks_the_close(make_client, monkeypatch):
    from app import slack_bridge
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    c = make_client(**FLOWS, **BOT_ENV)
    failed = []

    async def boom(*a, **k):
        failed.append(a)
        raise RuntimeError("slack down")

    twilio_post(c, tw_msg("hola", sid="S1"))
    assert store.get_thread(USER)
    monkeypatch.setattr(slack_bridge, "close_thread", boom)
    twilio_post(c, tw_msg("es todo", sid="S2"))
    assert failed                                                   # Slack really failed
    assert bodies(send)[-1] == sessions.text("close_user", "es")
    assert not sessions.is_open(USER) and store.get_history(USER) == []


def test_bare_gracias_and_new_question_do_not_close(make_client, send):
    c = make_client(**CLAUDE)
    install(mimic_model)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("gracias", sid="S2"))
    assert sessions.is_open(USER) and bodies(send)[-1].startswith("¡De nada!")
    twilio_post(c, tw_msg("gracias, y otra pregunta: ¿qué es ISO 42001?", sid="S3"))
    assert sessions.is_open(USER)


def test_no_after_algo_mas_closes_but_not_elsewhere(make_client, send):
    c = make_client(**CLAUDE)
    install(mimic_model)
    twilio_post(c, tw_msg("no", sid="S1"))                         # nothing asked before: not a close
    assert sessions.is_open(USER)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S2"))
    assert ALGO_MAS in bodies(send)[-1]
    n = len(send.calls)
    twilio_post(c, tw_msg("no", sid="S3"))
    assert len(send.calls) == n + 1 and "*hola*" in bodies(send)[-1]
    assert not sessions.is_open(USER)


# --------------------------------------------------------------------------- #
# Lifecycle after a close
# --------------------------------------------------------------------------- #
def test_no_idle_warning_after_close_and_hola_shows_menu(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS, IDLE_WARN_MINUTES="10", IDLE_CLOSE_MINUTES="5")
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("Sería todo, gracias", sid="S2"))
    n = len(send.calls)
    for minutes in (10, 15, 30):
        monkeypatch.setattr(sessions, "now", lambda m=minutes: t0 + m * 60)
        assert sessions.idle_actions(600, 300, set()) == []
        asyncio.run(main.check_idle())
    assert len(send.calls) == n                                      # no "¿Sigues ahí?"
    twilio_post(c, tw_msg("hola", sid="S3"))
    assert sessions.is_open(USER) and "¿Qué quieres hacer?" in bodies(send)[-1]


def test_idle_check_skips_session_closed_mid_loop(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    sessions.mark_closed(USER, "user")
    monkeypatch.setattr(sessions, "idle_actions", lambda *a: [(USER, "warn")])  # stale snapshot
    n = len(send.calls)
    asyncio.run(main.check_idle())
    assert len(send.calls) == n


# --------------------------------------------------------------------------- #
# Quiz, human handoff, guard
# --------------------------------------------------------------------------- #
def test_quiz_salir_exits_quiz_only(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("salir", sid="S2"))
    assert not quiz.active(USER) and sessions.is_open(USER)


def test_quiz_clear_closer_closes_chat(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("es todo", sid="S2"))
    assert not quiz.active(USER) and not sessions.is_open(USER)
    assert bodies(send)[-1] == sessions.text("close_user", "es")


def test_quiz_bare_no_is_not_a_close(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("no", sid="S2"))
    assert quiz.active(USER) and sessions.is_open(USER)


@respx.mock
def test_human_mode_closer_is_mirrored_not_closed(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    c = make_client(**FLOWS, **BOT_ENV)
    twilio_post(c, tw_msg("humano", sid="S1"))
    n = len(send.calls)
    twilio_post(c, tw_msg("Sería todo, gracias", sid="S2"))
    assert len(send.calls) == n                                      # bot stays quiet
    assert sessions.is_open(USER) and store.in_handoff(USER)
    assert any("Sería todo, gracias" in p["text"] for p in sm.replies())


def test_guard_never_counts_a_closer_as_off_topic(make_client, send):
    c = make_client(**CLAUDE)
    llm, gl = install(lambda u, b: _end("¡Gracias a ti! 👋"), guard_says="OUT")
    twilio_post(c, tw_msg("no, eso sería todo por hoy, muchas gracias Dexter", sid="S1"))
    assert gl.calls == []                                           # guard skipped
    assert (sessions.get(USER) or {}).get("offtopic", 0) == 0
    assert not sessions.is_open(USER) and len(llm.calls) == 1


# --------------------------------------------------------------------------- #
# CLOSE_ON_INTENT=false -> legacy
# --------------------------------------------------------------------------- #
def test_flag_off_is_legacy(make_client, send):
    from app import agent
    c = make_client(**CLAUDE, CLOSE_ON_INTENT="false")
    llm, _ = install(lambda u, b: _text("¡Un gusto! Aquí estoy cuando necesites algo."))
    twilio_post(c, tw_msg("Sería todo, gracias", sid="S1"))
    assert sessions.is_open(USER) and len(llm.calls) == 1            # went to the agent
    assert all(t["name"] != "end_conversation" for t in llm.calls[0]["tools"])
    assert "end_conversation" not in llm.calls[0]["system"][0]["text"]
    assert agent.run_tool(USER, "end_conversation", {}).startswith("error")
    twilio_post(c, tw_msg("diagnóstico", sid="S2"))
    twilio_post(c, tw_msg("adiós", sid="S3"))                        # mid-quiz: not a close (as before)
    assert sessions.is_open(USER)
