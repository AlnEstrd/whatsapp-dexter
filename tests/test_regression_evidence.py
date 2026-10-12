"""The two production conversations of 2026-10-11, replayed with mocked models.

Conversation fb603a208fe4: "Hola, quisiera más información" got the welcome and then an off-topic
warning; four answers ran 519-796 output tokens; the chat then idled out as a gap.
Conversation 699f3e5c8d62: "gracias, buen dia" after an answer was not a goodbye, and the chat idled
out as a gap.
"""
import json
from types import SimpleNamespace

import pytest

from app import db, main, sessions
from app.analytics import events
from app.flows import menu

from .test_channels import tw_msg, twilio_post
from .test_close_intent import CLAUDE, USER, FakeLLM, _text, bodies, send  # noqa: F401  (send is a fixture)


def install_by_text(agent_decide, guard_decide):
    """Agent and guard fakes that answer from the text they are given (install() answers one way)."""
    from app import agent, guard
    llm = FakeLLM(agent_decide)
    agent.get_agent()._client = SimpleNamespace(messages=llm)
    gl = FakeLLM(guard_decide)
    g = guard.Guard()
    g._client = SimpleNamespace(messages=gl)
    guard._guard = g
    return llm, gl


def words(texts):
    return sum(len(t.split()) for t in texts)


@pytest.fixture(autouse=True)
def no_bubble_pauses(monkeypatch):
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MIN", 0.0)
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MAX", 0.0)


def test_conversation_one_opens_with_the_menu_and_keeps_answers_short(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    short = ("El liderazgo marca si la IA se usa con criterio: quien decide qué se automatiza y quién responde "
             "por el resultado. ¿Quieres ver cómo se reparten esas decisiones en un equipo?")
    llm, gl = install_by_text(
        lambda user, last: _text(short),
        lambda user, last: _text("OUT" if "quisiera más" in user else "IN"),   # the guard as in production
    )
    turns = []
    for i, text in enumerate(["Hola, quisiera más información", "Ok", "Liderazgo",
                              "Como se desarrolla en un equipo", "Dime como iniciar"]):
        before = len(send.calls)
        twilio_post(c, tw_msg(text, sid=f"S{i}"))
        turns.append(bodies(send)[before:])

    welcome = turns[0]
    assert len(welcome) == 2 and "Estrada AI Lab" in welcome[0] and menu.TEXT["hint"]["es"] in welcome[1]
    assert not any("Aviso" in b for turn in turns for b in turn)   # no off-topic warning at 16:45
    for turn in turns[1:]:
        assert words(turn) <= 120                                    # each answer is short
    assert len(llm.calls) == 4                                       # four agent turns, the menu cost nothing
    assert len(gl.calls) == 1                                        # the guard ran once: only one message has 4+ words


def test_conversation_two_closes_on_a_thanks_and_a_farewell_with_no_tokens(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, _ = install_by_text(lambda user, last: _text("La regla 10-20-70 viene de BCG."),
                             lambda user, last: _text("IN"))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    calls, n = len(llm.calls), len(send.calls)
    twilio_post(c, tw_msg("gracias, buen dia", sid="S2"))

    assert bodies(send)[n:] == [sessions.text("close_user", "es")]   # one farewell
    assert len(llm.calls) == calls                                    # zero tokens for the goodbye
    assert not sessions.is_open(USER)
    events.drain()
    closes = [json.loads(r["data_json"]) for r in db.query(
        "SELECT data_json FROM events WHERE type = 'conversation_close' ORDER BY id")]
    assert closes[-1]["reason"] == "user_intent"
    assert not [r for r in db.query("SELECT 1 FROM events WHERE type = 'gap'")]   # not a gap
