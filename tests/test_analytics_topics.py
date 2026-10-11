"""Topic labels (guard JSON + route fallback), question samples, note_gap and the gap sources."""
import httpx
import pytest

from app import db, sessions
from app.analytics import events, topics

from .test_analytics_events import CLAUDE, FLOWS, USER, evs, install, meta, tw  # noqa: F401 (fixtures)
from .test_channels import tw_msg, twilio_post

LONG_Q = "¿Quién debe ser el owner de un agente de IA en compras?"


# --------------------------------------------------------------------------- #
# Guard output parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ('{"on_topic": true, "topic": "roles_ownership"}', (True, "roles_ownership")),
    ('{"on_topic": false, "topic": "fuera_de_tema"}', (False, "fuera_de_tema")),
    ('```json\n{"on_topic": true, "topic": "GOBERNANZA_RIESGO"}\n```', (True, "gobernanza_riesgo")),
    ('{"on_topic": true, "topic": "recetas"}', (True, "otro")),          # unknown id -> otro
    ('{"on_topic": true}', (True, "otro")),
    ('{"on_topic": true, "topic": 7}', (True, "otro")),
    ("IN", (True, None)), ("OUT", (False, None)), ("OUT.", (False, None)),   # legacy one-word
    ("garbage", (None, None)), ('{"on_topic": "yes"}', (None, None)),
])
def test_parse_guard(raw, expected):
    assert topics.parse_guard(raw) == expected


@pytest.mark.parametrize("reply,warned", [
    ('{"on_topic": false, "topic": "fuera_de_tema"}', True),
    ('{"on_topic": true, "topic": "procesos"}', False),
    ("OUT", True), ("IN", False),
    ("¿?", False),                 # unparseable: same fail-open rule as before (only OUT is off-topic)
])
def test_guard_decision_unchanged(make_client, tw, reply, warned):  # noqa: F811
    c = make_client(**CLAUDE)
    install([{"text": "Respuesta."}], guard=reply)
    twilio_post(c, tw_msg("dame una receta de enchiladas suizas por favor", sid="S1"))
    sent = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in tw.calls]
    assert any("Aviso 1 de 2" in b for b in sent) == warned


def test_guard_prompt_asks_for_topic_json(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    _, gl = install([{"text": "Ok."}], guard='{"on_topic": true, "topic": "roles_ownership"}')
    twilio_post(c, tw_msg(LONG_Q, sid="S1"))
    system = gl.calls[0]["system"]
    assert '"on_topic"' in system and "roles_ownership" in system and "one word" not in system
    assert gl.calls[0]["max_tokens"] >= 30
    m = evs("user_msg")[0]["data"]
    assert (m["route"], m["topic"], m["lang"]) == ("agent", "roles_ownership", "es")


# --------------------------------------------------------------------------- #
# Route-based labels for messages that skip the guard
# --------------------------------------------------------------------------- #
def test_skipped_messages_get_route_topics(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    _, gl = install([{"text": "Respuesta."}] * 3, guard='{"on_topic": true, "topic": "procesos"}')
    for i, text in enumerate(["hola", "3", "¿Cómo rediseño procesos con IA en mi empresa?", "¿y en ventas?",
                              "diagnóstico", "salir", "lámina 5", "humano"]):
        twilio_post(c, tw_msg(text, sid=f"S{i}"))
    got = [(m["data"]["route"], m["data"]["topic"]) for m in evs("user_msg")]
    assert got == [("menu", "saludo_menu"), ("agent", "sobre_alan"), ("agent", "procesos"),
                   ("agent", "procesos"),             # short follow-up inherits the last topic
                   ("quiz", "diagnostico"), ("quiz", "diagnostico"), ("file_reply", "laminas"),
                   ("handoff", "contacto_alan")]
    assert len(gl.calls) == 1                         # only the long question went to the guard


def test_closing_message_is_despedida(make_client, tw):  # noqa: F811
    c = make_client(**FLOWS)
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("Sería todo, gracias", sid="S2"))
    m = evs("user_msg")[-1]["data"]
    assert (m["route"], m["topic"]) == ("close", "despedida")


def test_from_route():
    assert topics.from_route("agent", "liderazgo") == "liderazgo"
    assert topics.from_route("agent", "saludo_menu") == "otro"
    assert topics.from_route("limited") == "otro"
    assert topics.from_route("quiz") == "diagnostico"
    assert all(topics.valid(t) == t for t in topics.TOPICS)


# --------------------------------------------------------------------------- #
# Question samples (scrubbed)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,clean", [
    ("Llámame al +52 1 33 1234 5678 porfa", "Llámame al […] porfa"),
    ("mi correo es ana.lopez+ia@empresa.com.mx", "mi correo es […]"),
    ("checa https://empresa.mx/politica?id=9 y www.foo.com", "checa […] y […]"),
    ("pagué con 4111 1111 1111 1111 ayer", "pagué con […] ayer"),
    ("folio 98765432", "folio […]"),
    ("la regla 10-20-70 de 2024", "la regla 10-20-70 de 2024"),
])
def test_scrub(raw, clean):
    assert events.scrub(raw) == clean


def test_samples_only_for_agent_messages_scrubbed_and_truncated(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    install([{"text": "Ok."}] * 2, guard='{"on_topic": true, "topic": "datos_plataforma"}')
    twilio_post(c, tw_msg("hola", sid="S1"))
    long = "¿Qué datos necesito para un agente? Escríbeme a ana@empresa.com " + "x" * 300
    twilio_post(c, tw_msg(long, sid="S2"))
    twilio_post(c, tw_msg("3", sid="S3"))             # menu:about -> synthetic text, no sample
    events.drain()
    rows = [dict(r) for r in db.query("SELECT * FROM question_samples")]
    assert len(rows) == 1
    s = rows[0]
    assert s["kind"] == "question" and s["topic"] == "datos_plataforma" and s["lang"] == "es"
    assert "ana@empresa.com" not in s["text"] and "[…]" in s["text"] and len(s["text"]) <= 200
    assert s["conversation_id"] == sessions.conversation_id(USER)


# --------------------------------------------------------------------------- #
# note_gap
# --------------------------------------------------------------------------- #
def test_note_gap_recorded_invisible_and_turn_continues(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    llm, _ = install([{"text": "", "tool": "note_gap",
                       "input": {"topic": "medicion_costo", "summary": "Costo de Copilot por usuario en MXN"}},
                      {"text": "Eso no lo cubre la plática; te cuento cómo medir el yield."}],
                     guard='{"on_topic": true, "topic": "medicion_costo"}')
    twilio_post(c, tw_msg("¿Cuánto cuesta Copilot por usuario al mes en pesos?", sid="S1"))
    sent = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in tw.calls]
    assert any("yield" in b for b in sent)                       # the turn went on to answer
    assert not any("Costo de Copilot" in b or "note_gap" in b for b in sent)   # never shown
    assert len(llm.calls) == 2
    assert any(t["name"] == "note_gap" for t in llm.calls[0]["tools"])
    assert "note_gap" in llm.calls[0]["system"][0]["text"]
    gap = evs("gap")[0]["data"]
    assert gap["topic"] == "medicion_costo" and gap["source"] == "tool"
    events.drain()
    note = dict(db.query("SELECT * FROM question_samples WHERE id = ?", (gap["note_id"],))[0])
    assert note["kind"] == "gap_note" and note["text"] == "Costo de Copilot por usuario en MXN"
    assert gap["sample_id"] and evs("agent_turn")[0]["data"]["tools"] == ["note_gap"]


def test_note_gap_unknown_topic_is_otro(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    install([{"tool": "note_gap", "input": {"topic": "astrologia", "summary": "x"}}, {"text": "Ok."}])
    twilio_post(c, tw_msg("¿Qué opina Alan del horóscopo y la IA en empresas?", sid="S1"))
    assert evs("gap")[0]["data"]["topic"] == "otro"


def test_all_three_gap_sources(make_client, tw, monkeypatch):  # noqa: F811
    """tool (above), handoff with reason != user_asked, idle after a read answer."""
    c = make_client(**CLAUDE)
    install([{"text": "ok"}])

    async def boom(*a, **k):
        raise RuntimeError("model down")
    from app import agent
    monkeypatch.setattr(agent.get_agent(), "handle_message", boom)
    twilio_post(c, tw_msg("¿Qué dice el EU AI Act sobre supervisión humana?", sid="S1"))
    assert evs("gap")[0]["data"]["source"] == "handoff"
    # a handoff the user asked for is not a gap
    twilio_post(c, tw_msg("humano", sid="S2", sender="whatsapp:+5213300000001"))
    assert evs("gap", user="523300000001") == []
    # idle_after_answer: tests/test_analytics_events.py::test_idle_after_answer_is_a_gap
