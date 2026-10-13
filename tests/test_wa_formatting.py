"""WhatsApp formatting on the wire: bot text is sanitized in the channel and in the agent path; a
human's reply (Slack, /admin/reply) goes out as typed; interactive strings are fitted to Meta's limits;
WA_FORMAT_SANITIZE=false sends what was sent before."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import agent, db
from app import main, slack_bridge, store
from app.channels import get_channel
from app.flows.types import Option

from .test_channels import META_SEND, TWILIO_SEND, tw_msg, twilio_post
from .test_close_intent import CLAUDE, USER, bodies, install, send  # noqa: F401  (send is a fixture)

EVIDENCE_TABLE = (
    "En compras, la IA ya hace parte del trabajo; el responsable sigue siendo una persona.\n\n"
    "| Actividad | Quién ejecuta | Quién responde |\n"
    "|---|---|---|\n"
    "| Analizar cotizaciones y comparar proveedores | IA asistida | Gerente de compras |\n"
    "| Generar órdenes de compra recurrentes | Agente (automático) | Gerente de compras |\n\n"
    "¿Quieres que te cuente cómo asignar esos responsables?"
)
META_TO = "5213312345678"


def resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


class Seq:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


@pytest.fixture
def graph():
    with respx.mock(assert_all_called=False) as router:
        router.post(META_SEND).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.1"}]}))
        yield router.post(META_SEND)


def _sent_payload(route) -> dict:
    return json.loads(route.calls[-1].request.content)


# --------------------------------------------------------------------------- #
# The channel is the choke point for bot text, and human text passes through
# --------------------------------------------------------------------------- #
def test_meta_send_text_sanitizes_bot_text(make_client, graph):
    make_client()
    asyncio.run(get_channel("meta").send_text(META_TO, EVIDENCE_TABLE))
    body = _sent_payload(graph)["text"]["body"]
    assert "|" not in body and "---" not in body
    assert "*Analizar cotizaciones y comparar proveedores*\n• Quién ejecuta: IA asistida" in body


def test_meta_send_text_sends_a_human_reply_as_typed(make_client, graph):
    make_client()
    asyncio.run(get_channel("meta").send_text(META_TO, EVIDENCE_TABLE, sanitize=False))
    assert _sent_payload(graph)["text"]["body"] == EVIDENCE_TABLE


def test_twilio_send_text_sanitizes_bot_text_and_not_human_text(make_client):
    make_client(WA_PROVIDER="twilio")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
        ch = get_channel("twilio")
        asyncio.run(ch.send_text("whatsapp:+5213312345678", "**Hola** equipo\n\n| a | b |\n|---|---|\n| x | y |"))
        asyncio.run(ch.send_text("whatsapp:+5213312345678", "**Hola** equipo | a |", sanitize=False))
    sent = [dict(httpx.QueryParams(c.request.content.decode()))["Body"] for c in route.calls]
    assert sent == ["*Hola* equipo\n\n• *x*: y", "**Hola** equipo | a |"]


def test_admin_reply_is_a_human_message_and_goes_out_as_written(make_client, graph):
    client = make_client()
    r = client.post("/admin/reply", json={"channel": "meta", "to": META_TO, "text": "**Hola** | a |"},
                    headers={"X-Admin-Token": "admin"})
    assert r.status_code == 200
    assert _sent_payload(graph)["text"]["body"] == "**Hola** | a |"


def test_slack_human_reply_is_not_sanitized(make_client, monkeypatch):
    make_client()
    sent = []

    class Chan:
        name = "twilio"

        async def send_text(self, to, body, *, sanitize=True):
            sent.append((to, body, sanitize))

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(store, "get_thread", lambda uid: {"channel_name": "twilio", "reply_to": "whatsapp:+52",
                                                          "channel_id": "C1", "mode": "human"})
    monkeypatch.setattr(slack_bridge, "get_channel", lambda name: Chan())
    monkeypatch.setattr(slack_bridge, "check_windows", nothing)
    monkeypatch.setattr(slack_bridge, "add_reaction", nothing)
    asyncio.run(slack_bridge.send_human_reply(USER, "**tal cual** | x |", "1.2"))
    assert sent == [("whatsapp:+52", "**tal cual** | x |", False)]


def test_a_bot_text_on_twilio_is_sanitized_before_it_is_sent_too(make_client):
    make_client(WA_PROVIDER="twilio")
    text = asyncio.run(_twilio_body("## Título\n**x** y"))
    assert text == "*Título*\n*x* y"


async def _twilio_body(text):
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
        await get_channel("twilio").send_text("whatsapp:+5213312345678", text)
    return dict(httpx.QueryParams(route.calls[0].request.content.decode()))["Body"]


# --------------------------------------------------------------------------- #
# Interactive strings are fitted to Meta's limits
# --------------------------------------------------------------------------- #
def test_interactive_list_is_fitted_to_meta_limits(make_client, graph):
    make_client()
    options = [Option(f"o{i}", "Título de la fila que es muy largo para Meta " * 2,
                      "Descripción " * 20) for i in range(12)]
    asyncio.run(get_channel("meta").send_options(META_TO, "Cuerpo " * 200, "Botón muy largo de la lista", options))
    inter = _sent_payload(graph)["interactive"]
    rows = inter["action"]["sections"][0]["rows"]
    assert len(rows) == 10                                            # Meta's 10 rows
    assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)
    assert all(r["title"].endswith("…") for r in rows)                # cut at a word, marked
    assert len(inter["body"]["text"]) <= 1024 and len(inter["action"]["button"]) <= 20


def test_interactive_buttons_are_fitted_to_meta_limits(make_client, graph):
    make_client()
    buttons = [(f"b{i}", "Un título de botón demasiado largo") for i in range(5)]
    asyncio.run(get_channel("meta").send_buttons(META_TO, "Hola " * 300, buttons))
    inter = _sent_payload(graph)["interactive"]
    assert len(inter["action"]["buttons"]) == 3                       # Meta's 3 reply buttons
    assert all(len(b["reply"]["title"]) <= 20 for b in inter["action"]["buttons"])
    assert len(inter["body"]["text"]) <= 1024


def test_a_short_interactive_string_is_untouched(make_client, graph):
    make_client()
    asyncio.run(get_channel("meta").send_options(META_TO, "¿Qué quieres hacer?", "Ver opciones",
                                                 [Option("menu:talk", "Explorar la plática", "Preguntas")]))
    inter = _sent_payload(graph)["interactive"]
    assert inter["body"]["text"] == "¿Qué quieres hacer?"
    assert inter["action"]["sections"][0]["rows"][0]["title"] == "Explorar la plática"


# --------------------------------------------------------------------------- #
# The agent path: the length rule counts what is sent, and the replay of the evidence
# --------------------------------------------------------------------------- #
def test_the_evidence_reply_through_the_agent_path_sends_no_pipes(make_client, send):
    c = make_client(**CLAUDE)
    install(lambda *_: resp(EVIDENCE_TABLE))
    twilio_post(c, tw_msg("¿Cómo se ve hoy el liderazgo en un rol concreto? Dame una tabla", sid="S-EVID"))
    sent = bodies(send)
    assert sent, "nothing was sent"
    assert not any("|" in b or "---" in b for b in sent)
    assert any("*Analizar cotizaciones y comparar proveedores*\n• Quién ejecuta: IA asistida\n"
               "• Quién responde: Gerente de compras" in b for b in sent)
    from app.analytics import events
    events.drain()   # the agent_turn event carries what the sanitizer fixed (the model's violation rate)
    row = db.query("SELECT data_json FROM events WHERE type = 'agent_turn' ORDER BY id DESC LIMIT 1")
    assert json.loads(row[0]["data_json"])["fmt_fixed"]["tables"] == 1


def test_the_agent_sanitizes_before_it_counts_and_records_what_it_fixed(make_client):
    make_client(**CLAUDE)
    agent.get_agent()._client = SimpleNamespace(messages=Seq(resp(EVIDENCE_TABLE)))
    text = asyncio.run(agent.get_agent().handle_message(USER, "tabla"))
    assert "|" not in text and "*Generar órdenes de compra recurrentes*" in text
    stats = agent.turn_stats[USER]   # read by main._agent_turn for the agent_turn event
    assert stats["fmt_fixed"]["tables"] == 1
    assert stats["reply_words"] == len(text.split())   # counted after the table became a list


def test_an_over_long_list_is_trimmed_as_a_whole_block_never_row_by_row(make_client):
    make_client(**CLAUDE)
    rows = "\n".join(f"• Dato {i} con varias palabras para que la lista pese mucho en el conteo total." for i in range(30))
    agent.get_agent()._client = SimpleNamespace(messages=Seq(resp("Lead corto con la respuesta.\n\n" + rows)))
    text = asyncio.run(agent.get_agent().handle_message(USER, "lista"))
    assert text == "Lead corto con la respuesta.\n\n" + agent.OFFER["es"]   # the list is one block: all or nothing


def test_a_header_stays_in_the_bubble_with_the_paragraph_under_it(make_client):
    make_client()
    bubbles = main.split_bubbles("Así se reparte.\n\n*Ejemplo: Gerente de compras*\n\nLa IA prepara; la persona responde.")
    assert bubbles == ["Así se reparte.", "*Ejemplo: Gerente de compras*\n\nLa IA prepara; la persona responde."]


def test_the_splitter_keeps_a_code_block_whole(make_client):
    make_client()
    code = "```\n" + "\n\n".join(f"bloque {i}" for i in range(3)) + "\n```"
    bubbles = main.split_bubbles("Antes.\n\n" + code)
    assert bubbles == ["Antes.", code]


def test_a_long_reply_is_split_between_blocks_for_the_channel_limit(make_client, graph, monkeypatch):
    make_client()
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MIN", 0.0)
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MAX", 0.0)
    body = "\n\n".join(f"*Bloque {i}*\n" + "\n".join(f"• dato {i}.{j} " + "palabra " * 5 for j in range(40))
                       for i in range(3))
    asyncio.run(main.send_bubbles(get_channel("meta"), SimpleNamespace(user_id=USER, reply_to=META_TO,
                                                                       message_id="", profile_name=None), body))
    sent = [json.loads(c.request.content)["text"]["body"] for c in graph.calls]
    assert len(sent) == 3 and all(len(b) <= 4096 for b in sent)
    assert all(b.startswith("*Bloque") for b in sent)


# --------------------------------------------------------------------------- #
# WA_FORMAT_SANITIZE=false: what was sent before
# --------------------------------------------------------------------------- #
def test_the_flag_off_sends_the_conversion_from_before(make_client, graph):
    make_client(WA_FORMAT_SANITIZE="false")
    text = "**Hola** equipo\n\n- uno"
    asyncio.run(main.send_reply(get_channel("meta"), SimpleNamespace(user_id=USER, reply_to=META_TO,
                                                                     message_id="", profile_name=None), text))
    assert _sent_payload(graph)["text"]["body"] == "*Hola* equipo\n\n• uno"


def test_the_flag_off_does_not_sanitize_on_the_wire(make_client, graph):
    make_client(WA_FORMAT_SANITIZE="false")
    asyncio.run(get_channel("meta").send_text(META_TO, "**x** | a |\n|---|---|"))
    assert _sent_payload(graph)["text"]["body"] == "**x** | a |\n|---|---|"


def test_the_flag_off_leaves_the_interactive_bodies_as_they_were(make_client, graph):
    make_client(WA_FORMAT_SANITIZE="false")
    asyncio.run(get_channel("meta").send_buttons(META_TO, "**Cuerpo**", [("a", "Sí")]))
    assert _sent_payload(graph)["interactive"]["body"]["text"] == "**Cuerpo**"
