"""The opening message: info requests get the welcome menu (no model call), a first message is never
an off-topic warning, and every inbound message gets exactly one routed reply bundle."""
import pytest

from app import sessions
from app.flows import menu

from .test_channels import tw_msg, twilio_post
from .test_close_intent import ALGO_MAS, CLAUDE, USER, bodies, install, send  # noqa: F401  (send is a fixture)
from .test_close_intent import _text

OFF_TOPIC = "¿Cuál es la capital de Francia y cuánto mide el Everest?"


@pytest.fixture
def routed(monkeypatch):
    """Record the route of every _route call (one per routed reply bundle)."""
    from app import main
    seen = []
    real = main._route
    monkeypatch.setattr(main, "_route", lambda msg, name: (seen.append(name), real(msg, name)))
    return seen


@pytest.mark.parametrize("text,lang", [
    ("Hola, quisiera más información", "es"), ("más info", "es"),
    ("¿de qué se trata?", "es"), ("what can you do?", "en"),
])
def test_an_opening_info_request_gets_the_welcome_and_the_menu_only(make_client, send, routed, text, lang):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, gl = install(lambda *_: _text("nunca debe llegar al modelo"))
    twilio_post(c, tw_msg(text, sid="S1"))
    b = bodies(send)
    assert len(b) == 2                                           # the welcome, then the menu list
    assert "Estrada AI Lab" in b[0] and menu.TEXT["hint"][lang] in b[1]
    assert not any("Aviso" in x for x in b)                      # no warning
    assert llm.calls == [] and gl.calls == []                    # no agent, no guard
    assert routed == ["menu"]                                    # one routed bundle
    assert sessions.get(USER)["offtopic"] == 0


def test_a_first_message_the_guard_rejects_gets_welcome_not_a_warning(make_client, send, routed):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, gl = install(lambda *_: _text("unused"), guard_says="OUT")
    twilio_post(c, tw_msg(OFF_TOPIC, sid="S1"))
    b = bodies(send)
    assert "Estrada AI Lab" in b[0] and menu.TEXT["hint"]["es"] in b[1]
    assert not any("Aviso" in x for x in b)
    assert routed == ["menu"] and llm.calls == [] and len(gl.calls) == 1   # the guard still ran
    assert sessions.get(USER)["offtopic"] == 0
    twilio_post(c, tw_msg("¿Y quién ganó el mundial de 2018?", sid="S2"))   # the next one is the first warning
    assert "Aviso 1 de 2" in bodies(send)[-1]


def test_every_inbound_message_gets_exactly_one_routed_bundle(make_client, send, routed):  # noqa: F811
    c = make_client(**CLAUDE)
    install(lambda *_: _text(f"Respuesta. {ALGO_MAS}"), guard_says="OUT")
    script = [
        ("Hola, quisiera más información", ["menu"]),
        (OFF_TOPIC, ["offtopic_warning"]),
        ("¿Y quién ganó el mundial de 2018?", ["offtopic_warning"]),
        ("gracias, buen día", ["close"]),
        ("hola", ["menu"]),
    ]
    for i, (text, expected) in enumerate(script):
        routed.clear()
        twilio_post(c, tw_msg(text, sid=f"S{i}"))
        assert routed == expected, text


def test_info_words_in_the_middle_of_a_chat_still_go_to_the_agent(make_client, send, routed):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, _ = install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))   # opens the chat
    routed.clear()
    twilio_post(c, tw_msg("más info", sid="S2"))
    assert routed == ["agent"] and len(llm.calls) == 2


def test_the_welcome_copy_uses_the_brand_name():
    assert "Estrada AI Lab" in menu.TEXT["welcome"]["es"] and "Estrada AI Lab" in menu.TEXT["intro"]["es"]
    assert "Estrada AI Lab" in menu.TEXT["welcome"]["en"] and "Estrada AI Lab" in menu.TEXT["intro"]["en"]
    assert "Estrada Lab" not in menu.TEXT["welcome"]["es"].replace("Estrada AI Lab", "")


@pytest.mark.parametrize("text", [
    "Hola, quisiera más información", "más info", "¿de qué se trata?", "what can you do?",
    "buenos días, ¿qué haces?", "quiero más información",
])
def test_info_requests_are_recognised(text):
    assert menu.is_info_request(text)


@pytest.mark.parametrize("text", [
    "hola", "¿Qué es la regla 10-20-70?", "más info sobre ownership", "gracias", "buenas tardes",
])
def test_other_messages_are_not_info_requests(text):
    assert not menu.is_info_request(text)
