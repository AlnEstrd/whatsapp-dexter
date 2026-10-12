"""Reply length: the prompt rule, the backstop on the text sent, and what the metrics record."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import agent, db, formatting
from app.analytics import events

from .test_channels import tw_msg, twilio_post
from .test_close_intent import CLAUDE, USER, bodies, install, send  # noqa: F401  (send is a fixture)

OFFER_ES = "¿Quieres que profundice en algún punto?"


def resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


class Seq:
    """Scripted messages.create: one response per call, in order; every request is recorded."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


class Always:
    """messages.create that gives the same answer every time (the guard, or a chatty agent)."""

    def __init__(self, text):
        self.text, self.calls = text, []

    async def create(self, **kw):
        self.calls.append(kw)
        return resp(self.text)


def paragraphs(n, words):
    """n paragraphs of ``words`` words each; every paragraph ends with a full stop."""
    return "\n\n".join(" ".join(f"palabra{p}x{i}" for i in range(words)) + "." for p in range(n))


def agent_turns():
    events.drain()
    return [json.loads(r["data_json"]) for r in db.query("SELECT data_json FROM events WHERE type = 'agent_turn' ORDER BY id")]


@pytest.fixture(autouse=True)
def no_bubble_pauses(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MIN", 0.0)
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MAX", 0.0)


def test_prompt_states_the_length_rule_with_the_configured_target(make_client):
    make_client(**CLAUDE, REPLY_MAX_WORDS="90")
    prompt = "".join(b["text"] for b in agent.system_blocks())
    assert "at most about 90 words" in prompt
    assert "{reply_max_words}" not in prompt
    assert "Match the user" not in prompt and "30-80 words" not in prompt   # the old length lines are gone


def test_length_defaults_are_120_and_220_words(make_client):
    make_client(**CLAUDE)
    from app.config import get_settings
    s = get_settings()
    assert (s.reply_max_words, s.reply_max_words_hard) == (120, 220)


def test_agent_calls_leave_room_for_a_topic_pdf_input(make_client):
    make_client(**CLAUDE)
    llm = Seq(resp("Listo."))
    agent.get_agent()._client = SimpleNamespace(messages=llm)
    asyncio.run(agent.get_agent().handle_message(USER, "hola"))
    assert llm.calls[0]["max_tokens"] >= 1500


# --------------------------------------------------------------------------- #
# The backstop (formatting.cap_words)
# --------------------------------------------------------------------------- #
def test_a_short_reply_is_returned_as_written():
    text = "La regla 10-20-70 viene de BCG.\n\n¿Te cuento cómo se aplica en compras?"
    assert formatting.cap_words(text, 220, OFFER_ES) == (text, False)


def test_a_long_reply_is_cut_at_a_whole_paragraph_under_the_cap():
    text = paragraphs(4, 100)                       # 400 words in four paragraphs
    out, trimmed = formatting.cap_words(text, 220, OFFER_ES)
    assert trimmed and out.endswith(OFFER_ES)
    assert out[: -len(OFFER_ES)].rstrip() == "\n\n".join(text.split("\n\n")[:2])   # the first two paragraphs
    assert len(out.split()) <= 220 + len(OFFER_ES.split())


def test_a_long_paragraph_is_cut_at_a_sentence_instead():
    text = " ".join(f"Frase {i} de prueba." for i in range(200))   # one paragraph
    out, trimmed = formatting.cap_words(text, 220, OFFER_ES)
    body = out[: -len(OFFER_ES)].rstrip()
    assert trimmed and body.endswith(".") and len(body.split()) <= 220


def test_a_reply_cut_off_by_max_tokens_drops_its_incomplete_sentence():
    text = "Primera frase completa. Segunda frase completa. Tercera frase que se cor"
    assert formatting.cap_words(text, 220, OFFER_ES, truncated=True) == \
        (f"Primera frase completa. Segunda frase completa.\n\n{OFFER_ES}", True)


def test_nothing_whole_to_send_after_max_tokens_gives_an_empty_reply():
    assert formatting.cap_words("sin ningún punto", 220, OFFER_ES, truncated=True) == ("", True)


def test_a_single_long_sentence_is_never_cut_mid_sentence():
    text = " ".join(["palabra"] * 300)
    assert formatting.cap_words(text, 220, OFFER_ES) == (text, False)


# --------------------------------------------------------------------------- #
# Through the agent and the channel
# --------------------------------------------------------------------------- #
def test_a_400_word_reply_is_trimmed_before_it_is_sent(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    install(lambda *_: resp(paragraphs(4, 100)))
    twilio_post(c, tw_msg("hola", sid="S0"))        # opens the chat with the menu
    n = len(send.calls)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70 y por qué importa?", sid="S1"))
    reply = bodies(send)[n:]
    assert reply[-1] == OFFER_ES
    assert "\n\n".join(reply[:-1]) == "\n\n".join(paragraphs(4, 100).split("\n\n")[:2])
    turn = agent_turns()[-1]
    assert turn["trimmed"] is True and turn["reply_words"] == 200 + len(OFFER_ES.split())


def test_a_reply_under_the_cap_is_sent_untouched(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    install(lambda *_: resp("La regla 10-20-70 viene de BCG.\n\n¿Te cuento cómo se aplica en compras?"))
    twilio_post(c, tw_msg("hola", sid="S0"))
    n = len(send.calls)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70 y por qué importa?", sid="S1"))
    assert bodies(send)[n:] == ["La regla 10-20-70 viene de BCG.", "¿Te cuento cómo se aplica en compras?"]
    turn = agent_turns()[-1]
    assert turn["trimmed"] is False and turn["reply_words"] == 13


def test_a_max_tokens_reply_is_trimmed_the_same_way(make_client, send):  # noqa: F811
    c = make_client(**CLAUDE)
    cut = ("La regla 10-20-70 viene de BCG. Explica que 70% del esfuerzo va a personas. "
           "Y que la herramienta casi nunca es el problema, aunque la")
    install(lambda *_: resp(cut, stop="max_tokens"))
    twilio_post(c, tw_msg("hola", sid="S0"))
    n = len(send.calls)
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70 y por qué importa?", sid="S1"))
    assert bodies(send)[n:] == ["La regla 10-20-70 viene de BCG. Explica que 70% del esfuerzo va a personas.",
                                OFFER_ES]


def test_a_topic_pdf_input_is_not_capped(make_client):
    make_client(**CLAUDE)
    long_paragraph = " ".join(f"palabra{i}" for i in range(480)) + "."   # the PDF's content, longer than the cap
    pdf = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", id="tu_1", name="create_topic_pdf",
        input={"language": "es", "title": "La regla 10-20-70", "summary": "Resumen breve.",
               "sections": [{"heading": "Idea", "paragraphs": [long_paragraph]}], "sources": ["BCG, 2024"]})])
    llm = Seq(pdf, resp("Te mando el resumen en PDF."))
    agent.get_agent()._client = SimpleNamespace(messages=llm)
    reply = asyncio.run(agent.get_agent().handle_message(USER, "hazme un PDF de la regla 10-20-70"))
    assert reply == "Te mando el resumen en PDF."                    # the short text is not capped or changed
    tool_result = llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert tool_result.startswith("PDF queued")                      # the whole tool input was used
    assert len(agent.pop_documents(USER)) == 1
