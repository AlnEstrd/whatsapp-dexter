"""Closing: composite closers, the ask-then-close rule, the idle softening and the gap rule."""
import asyncio
import json
import time

import pytest

from app import db, main, sessions
from app.analytics import events

from .test_channels import tw_msg, twilio_post
from .test_close_intent import ALGO_MAS, CLAUDE, USER, _text, bodies, install, send  # noqa: F401  (send is a fixture)

PRIOR = "¿Qué es la regla 10-20-70?"


def plain(*_):   # an answer that does not ask anything else
    return _text("La regla 10-20-70 viene de BCG.")


def asking(*_):  # an answer that ends with "¿Algo más…?"
    return _text(f"La regla 10-20-70 viene de BCG. {ALGO_MAS}")


def opened(make_client, decide):
    """A chat opened with "hola" (the menu, as in production) and one answered question, so the
    answer is not a chat's first message (a first answer also gets the menu offer, which would
    clear the closing question). Returns (client, agent llm, guard llm)."""
    c = make_client(**CLAUDE)
    llm, gl = install(decide)
    twilio_post(c, tw_msg("hola", sid="S0"))
    twilio_post(c, tw_msg(PRIOR, sid="S1"))
    return c, llm, gl


def clock(monkeypatch, seconds_from_now):
    """Pin sessions.now to the real time plus ``seconds_from_now`` (idle and ask-window tests)."""
    t0 = time.time()
    monkeypatch.setattr(sessions, "now", lambda: t0 + seconds_from_now)


def events_of(type_):
    events.drain()
    return [json.loads(r["data_json"]) for r in db.query(
        "SELECT data_json FROM events WHERE type = ? ORDER BY id", (type_,))]


@pytest.fixture(autouse=True)
def no_bubble_pauses(monkeypatch):
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MIN", 0.0)
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MAX", 0.0)


# --------------------------------------------------------------------------- #
# Composite closers (keyword path, zero tokens)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("closer,lang", [
    ("gracias, buen día", "es"), ("muchas gracias, buenas noches", "es"),
    ("gracias que tengas bonito día", "es"), ("thanks, have a good day", "en"),
])
def test_composite_closer_closes_with_one_farewell_and_no_tokens(make_client, send, closer, lang):  # noqa: F811
    c, llm, _ = opened(make_client, plain)
    n, calls = len(send.calls), len(llm.calls)
    twilio_post(c, tw_msg(closer, sid="S2"))
    assert bodies(send)[n:] == [sessions.text("close_user", lang)]
    assert len(llm.calls) == calls
    assert not sessions.is_open(USER) and sessions.get(USER)["closed_reason"] == "user_intent"


@pytest.mark.parametrize("text", ["gracias", "buenas noches, tengo una pregunta", "buen día, ¿qué es el nivel L2?"])
def test_near_misses_are_answered_not_closed(make_client, send, text):  # noqa: F811
    c, llm, _ = opened(make_client, plain)
    calls = len(llm.calls)
    twilio_post(c, tw_msg(text, sid="S2"))
    assert sessions.is_open(USER) and len(llm.calls) == calls + 1


def test_a_composite_as_the_first_message_is_not_a_closer(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, _ = install(plain)
    twilio_post(c, tw_msg("gracias, buen día", sid="S1"))
    assert sessions.is_open(USER) and len(llm.calls) == 1


def test_buenas_tardes_as_the_first_message_is_the_menu(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    install(plain)
    twilio_post(c, tw_msg("buenas tardes", sid="S1"))
    assert "¿Qué quieres hacer?" in bodies(send)[0] and sessions.is_open(USER)


@pytest.mark.parametrize("text,expected", [
    ("gracias, buen día", True), ("muchas gracias, buenas noches", True),
    ("gracias que tengas bonito día", True), ("thanks, have a good day", True),
    ("gracias", False), ("tarde", False), ("noche", False), ("buenas noches, tengo una pregunta", False),
    ("buen día, ¿qué es el nivel L2?", False), ("no cerrar", False),
])
def test_composite_closer_vocabulary(text, expected):
    assert sessions.is_composite_closer(text) is expected


# --------------------------------------------------------------------------- #
# Ask-then-close
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply", ["no", "no gracias", "gracias", "👍", "nope"])
def test_a_bare_answer_to_our_closing_question_closes_without_tokens(make_client, send, reply):  # noqa: F811
    c, llm, _ = opened(make_client, asking)
    n, calls = len(send.calls), len(llm.calls)
    twilio_post(c, tw_msg(reply, sid="S2"))
    assert bodies(send)[n:] == [sessions.text("close_user", "es")]
    assert len(llm.calls) == calls and not sessions.is_open(USER)


def test_the_ask_window_is_fifteen_minutes(make_client, send, monkeypatch):  # noqa: F811
    c, llm, _ = opened(make_client, asking)
    clock(monkeypatch, 16 * 60)
    calls = len(llm.calls)
    twilio_post(c, tw_msg("no", sid="S2"))
    assert sessions.is_open(USER) and len(llm.calls) == calls + 1      # normal flow, not a close


def test_a_new_question_clears_the_ask(make_client, send):  # noqa: F811
    def decide(user, last_bot):
        return _text("El nivel L2 es el segundo de la escala.") if "nivel" in user else asking(user, last_bot)
    c, llm, _ = opened(make_client, decide)
    assert sessions.get(USER).get("asked_anything_else_at")            # we asked
    twilio_post(c, tw_msg("¿Y qué es el nivel L2?", sid="S2"))
    assert sessions.get(USER).get("asked_anything_else_at") is None    # the answer was not a question
    calls = len(llm.calls)
    twilio_post(c, tw_msg("no", sid="S3"))
    assert sessions.is_open(USER) and len(llm.calls) == calls + 1      # so "no" is a normal message now


# --------------------------------------------------------------------------- #
# Idle: no "¿Sigues ahí?" after a thanks, and no gap for a goodbye
# --------------------------------------------------------------------------- #
def test_idle_after_a_thanks_that_got_our_question_closes_without_a_warning(make_client, send, monkeypatch):  # noqa: F811
    c = make_client(**CLAUDE)
    install(lambda user, last: _text(f"Con gusto. {ALGO_MAS}") if user == "gracias" else plain(user, last))
    twilio_post(c, tw_msg(PRIOR, sid="S1"))                 # a plain answer: no question
    twilio_post(c, tw_msg("gracias", sid="S2"))             # not a closer here: the agent answers and asks
    assert sessions.get(USER).get("asked_anything_else_at")
    assert sessions.last_user_kind(USER) == "ack"
    clock(monkeypatch, 11 * 60)
    n = len(send.calls)
    asyncio.run(main.check_idle())
    sent = bodies(send)[n:]
    assert sent == [sessions.text("close_user", "es")]     # the farewell, once; no "¿Sigues ahí?"
    assert not sessions.is_open(USER)
    asyncio.run(main.check_idle())
    assert len(send.calls) == n + 1                        # nothing more is sent
    assert events_of("conversation_close")[-1]["reason"] == "user_intent_late"


def test_idle_after_a_thanks_without_our_question_warns_then_closes_quietly(make_client, send, monkeypatch):  # noqa: F811
    c, llm, _ = opened(make_client, plain)
    twilio_post(c, tw_msg("gracias", sid="S2"))             # the agent answers without asking
    assert not sessions.get(USER).get("asked_anything_else_at")
    clock(monkeypatch, 11 * 60)
    asyncio.run(main.check_idle())
    assert "¿Sigues ahí?" in bodies(send)[-1]              # the warning, as usual
    clock(monkeypatch, 17 * 60)
    asyncio.run(main.check_idle())
    assert events_of("conversation_close")[-1]["reason"] == "user_intent_late"
    assert not any(e["source"] == "idle_after_answer" for e in events_of("gap"))


def test_a_goodbye_or_thanks_before_silence_is_not_a_gap(make_client):
    make_client(**CLAUDE)
    sess = {"conversation_id": "c1", "last_step": "agent_turn", "last_bot_wamid": "w1", "last_bot_read": True,
            "last_topic": "procesos", "last_sample_id": "s1", "turns": 2, "opened_at": time.time() - 60}
    main._record_close(USER, "user_intent_late", sess)     # a close after the user's own goodbye
    main._record_close(USER, "idle", sess)                 # a real idle after an answer
    gaps = events_of("gap")
    assert [g["source"] for g in gaps] == ["idle_after_answer"]
    assert [e["reason"] for e in events_of("conversation_close")] == ["user_intent_late", "idle"]


def test_a_real_idle_after_an_answer_closes_as_idle(make_client, send, monkeypatch):  # noqa: F811
    c, _, _ = opened(make_client, plain)
    twilio_post(c, tw_msg("¿Qué es el nivel L2?", sid="S2"))
    clock(monkeypatch, 11 * 60)
    asyncio.run(main.check_idle())
    clock(monkeypatch, 17 * 60)
    asyncio.run(main.check_idle())
    assert events_of("conversation_close")[-1]["reason"] == "idle"
