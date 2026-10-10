"""Conversation lifecycle: close messages, idle warning/close, restart recovery, off-topic guard."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import sessions, store
from app.flows import quiz

from .test_channels import TWILIO_SEND, tw_msg, twilio_post
from .test_slack_bridge import BOT_ENV, SlackMock, parent_ts, reaction, slack_event, thread_reply

USER = "523312345678"
FLOWS = {"FLOWS_ENABLED": "true"}


def bodies(send):
    return [dict(httpx.QueryParams(c.request.content.decode()))["Body"] for c in send.calls]


@pytest.fixture
def send():
    with respx.mock(assert_all_called=False) as router:
        router.post("https://hooks.slack.test/T/B/X").mock(return_value=httpx.Response(200))
        yield router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))


_real_sleep = asyncio.sleep


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Close: user / admin / Slack
# --------------------------------------------------------------------------- #
def test_user_goodbye_closes_with_message_and_fresh_start(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("Adiós!", sid="S2"))
    assert bodies(send)[-1] == sessions.text("close_user", "es")
    assert not sessions.is_open(USER) and store.get_history(USER) == []
    twilio_post(c, tw_msg("hola", sid="S3"))
    assert sessions.is_open(USER)
    assert "¿Qué quieres hacer?" in bodies(send)[-1]


def test_goodbye_during_quiz_only_exits_quiz(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("salir", sid="S2"))
    assert not quiz.active(USER) and sessions.is_open(USER)


def test_admin_close_endpoint(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    r = c.post("/admin/close", json={"user_id": "+5213312345678"}, headers={"X-Admin-Token": "admin"})
    assert r.json() == {"closed": True}
    assert bodies(send)[-1] == sessions.text("close_admin", "es")
    assert not sessions.is_open(USER)


@respx.mock
@pytest.mark.parametrize("how", ["text", "reaction"])
def test_slack_close_sends_closing_message(make_client, how):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    c = make_client(**BOT_ENV)
    twilio_post(c, tw_msg("humano", sid="SM1"))
    first = parent_ts()
    slack_event(c, thread_reply("close", first) if how == "text" else reaction("lock", first))
    assert bodies(send)[-1] == sessions.text("close_admin", "es")
    assert any(p["text"].startswith("🔒 Conversation closed by <@UOWNER>") for p in sm.replies())
    assert not sessions.is_open(USER) and not store.in_handoff(USER)


# --------------------------------------------------------------------------- #
# Idle: warn, then close
# --------------------------------------------------------------------------- #
def test_idle_warning_then_close(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS, IDLE_WARN_MINUTES="10", IDLE_CLOSE_MINUTES="5")
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    twilio_post(c, tw_msg("hola", sid="S1"))
    n = len(send.calls)

    monkeypatch.setattr(sessions, "now", lambda: t0 + 9 * 60)
    run(main.check_idle())
    assert len(send.calls) == n                                  # not yet

    monkeypatch.setattr(sessions, "now", lambda: t0 + 10 * 60)
    run(main.check_idle())
    assert bodies(send)[-1] == sessions.text("idle_warn", "es", m=5)
    run(main.check_idle())
    assert len(send.calls) == n + 1                              # warned once

    monkeypatch.setattr(sessions, "now", lambda: t0 + 15 * 60)
    run(main.check_idle())
    assert bodies(send)[-1] == sessions.text("close_bot", "es")
    assert not sessions.is_open(USER)


def test_activity_after_warning_resets_idle(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS)
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    twilio_post(c, tw_msg("hola", sid="S1"))
    monkeypatch.setattr(sessions, "now", lambda: t0 + 10 * 60)
    run(main.check_idle())                                       # warned
    twilio_post(c, tw_msg("menu", sid="S2"))                     # user is back
    monkeypatch.setattr(sessions, "now", lambda: t0 + 16 * 60)
    run(main.check_idle())
    assert sessions.is_open(USER)


def test_idle_skips_human_handoff(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS)
    t0 = 1_000_000.0
    monkeypatch.setattr(sessions, "now", lambda: t0)
    twilio_post(c, tw_msg("humano", sid="S1"))
    n = len(send.calls)
    monkeypatch.setattr(sessions, "now", lambda: t0 + 60 * 60)
    run(main.check_idle())
    assert len(send.calls) == n and sessions.is_open(USER)


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
def _previous_boot_session(**extra):
    sessions._sessions[USER] = {
        "state": "open", "channel_name": "twilio", "reply_to": "whatsapp:+5213312345678",
        "last_activity": sessions.now() - 120, "boot": "oldboot", "context": True, "quiz": False,
        "lang": "es", "offtopic": 0, **extra}


def test_restart_proactively_notifies_recent_users(make_client, send, monkeypatch):
    from app import main
    make_client(**FLOWS)
    _previous_boot_session(quiz=True)
    monkeypatch.setattr(main, "RESTART_DELAY", 0)
    run(main.recover_after_restart())
    msg = bodies(send)[-1]
    assert "Una disculpa de parte de Alan" in msg and "diagnóstico" in msg
    assert not sessions.needs_restart_notice(USER)


def test_restart_notice_on_next_message_and_stale_sessions_closed(make_client, send, monkeypatch):
    from app import main
    c = make_client(**FLOWS)
    _previous_boot_session()
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    b = bodies(send)
    assert "Una disculpa de parte de Alan" in b[0] and b[1].startswith("echo:")

    sessions.reset()
    _previous_boot_session(last_activity=sessions.now() - 3 * 3600)   # older than warn+close
    monkeypatch.setattr(main, "RESTART_DELAY", 0)
    n = len(send.calls)
    run(main.recover_after_restart())
    assert len(send.calls) == n and not sessions.is_open(USER)


def test_sessions_persist_to_disk(make_client, send):
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    sessions._sessions.clear()
    assert sessions.load_all() == 1 and sessions.is_open(USER)


# --------------------------------------------------------------------------- #
# Off-topic guard
# --------------------------------------------------------------------------- #
class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


def test_off_topic_warns_twice_then_escalates_without_agent_tokens(make_client, send):
    from app import agent, guard
    c = make_client(**FLOWS, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    g = guard.Guard()
    g._client = SimpleNamespace(messages=FakeAnthropic(["OUT", "OUT", "OUT"]))
    guard._guard = g
    main_llm = FakeAnthropic([])
    agent.get_agent()._client = SimpleNamespace(messages=main_llm)

    for i in range(3):
        twilio_post(c, tw_msg("dame una receta de enchiladas suizas por favor", sid=f"S{i}"))
    b = bodies(send)[1:]                             # [0] is the new-chat intro
    assert "Aviso 1 de 2" in b[0] and "Aviso 2 de 2" in b[1]
    assert b[2] == sessions.text("offtopic_escalate", "es")
    assert store.in_handoff(USER)
    assert main_llm.calls == 0                       # the expensive agent never ran


def test_on_topic_and_short_messages_reach_agent(make_client, send):
    from app import agent, guard
    c = make_client(**FLOWS, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    gl = FakeAnthropic(["IN"])
    g = guard.Guard()
    g._client = SimpleNamespace(messages=gl)
    guard._guard = g
    agent.get_agent()._client = SimpleNamespace(messages=FakeAnthropic(["Respuesta 1", "Respuesta 2"]))
    twilio_post(c, tw_msg("¿Por qué 3 de 4 empresas no ven valor en la IA?", sid="S1"))
    twilio_post(c, tw_msg("¿y en ventas?", sid="S2"))          # short follow-up: no guard call
    assert gl.calls == 1
    assert "Respuesta 1" in bodies(send) and "Respuesta 2" in bodies(send)


def test_guard_fails_open(make_client, send):
    from app import agent, guard

    class Boom:
        async def create(self, **kw):
            raise RuntimeError("down")

    c = make_client(**FLOWS, AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    g = guard.Guard()
    g._client = SimpleNamespace(messages=Boom())
    guard._guard = g
    agent.get_agent()._client = SimpleNamespace(messages=FakeAnthropic(["ok"]))
    twilio_post(c, tw_msg("¿Qué dice el EU AI Act sobre supervisión humana?", sid="S1"))
    assert "ok" in bodies(send)
