"""Voice notes (transcribed, then handled as text) and assessments pre-scored from a story."""
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import voice
from app.flows import quiz

from .test_flows import FakeMessages, _resp, _text, _tool, meta_payload, meta_sends

GRAPH = "https://graph.facebook.com/v20.0"
META_SEND = f"{GRAPH}/111/messages"
META_MEDIA = f"{GRAPH}/111/media"
CDN = "https://lookaside.fbsbx.com/file/voice"
STT = "https://api.openai.com/v1/audio/transcriptions"
SLACK = "https://hooks.slack.test/T/B/X"
OGG = b"OggS" + b"\x00" * 64
UID = "523312345678"
VOICE_ON = {"TRANSCRIBE_API_KEY": "stt-key"}


def voice_payload(mid="wamid.v", mime="audio/ogg; codecs=opus"):
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": "5213312345678", "profile": {"name": "Ana López"}}],
        "messages": [{"from": "5213312345678", "id": mid, "type": "audio",
                      "audio": {"id": "AUD1", "mime_type": mime, "voice": True}}]}}]}]}


def texts(meta_route):
    return [m["text"]["body"] if m["type"] == "text" else m["interactive"]["body"]["text"]
            for m in meta_sends(meta_route)]


def mock_download(data=OGG):
    respx.get(f"{GRAPH}/AUD1").mock(return_value=httpx.Response(
        200, json={"url": CDN, "mime_type": "audio/ogg", "file_size": len(data)}))
    respx.get(CDN).mock(return_value=httpx.Response(200, content=data))


def mock_stt(text):
    return respx.post(STT).mock(return_value=httpx.Response(200, json={"text": text}))


# ---- units -----------------------------------------------------------------
def test_sniff_audio_formats():
    assert voice.sniff(OGG) == ("audio/ogg", "ogg")
    assert voice.sniff(b"ID3" + b"\x00" * 20)[1] == "mp3"
    assert voice.sniff(b"\x00\x00\x00\x18ftypM4A ")[1] == "m4a"
    assert voice.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ")[1] == "wav"
    assert voice.sniff(b"#!AMR\n") is None


def test_quiz_parses_spoken_answers():
    st = quiz.QuizState()
    assert quiz._parse(st, "La tercera.", None) == 3
    assert quiz._parse(st, "opción 2", None) == 2
    assert quiz._parse(st, "Option four", None) == 4
    assert quiz._parse(st, "entre la dos y la tres", None) is None
    assert quiz._parse(st, "hola", None) is None


def test_clean_known_drops_bad_entries():
    assert quiz.clean_known({"data": 2, "nope": 1, "people": 7, "yield": "2", "process": True}) == {"data": 2}


def test_quiz_with_known_domains_asks_only_the_rest():
    quiz.reset()
    outs = quiz.start("u", "es", known={"adoption": 1, "governance": 0, "data": 1})
    assert "califiqué 3 de 9" in outs[0].text and "Gobernanza y riesgo: _Ninguna_" in outs[0].text
    assert outs[1].text.startswith("*1/6 · Estrategia y valor*")  # adoption skipped
    out = quiz.answer("u", "3")[0]
    assert out.text.startswith("*2/6 · Liderazgo")  # governance skipped
    for _ in range(5):
        outs = quiz.answer("u", "3")
    r = quiz.last_result("u")
    assert r.answers == {"adoption": 1, "strategy": 2, "governance": 0, "leadership": 2, "ownership": 2,
                         "people": 2, "process": 2, "data": 1, "yield": 2}
    assert r.inferred == ["adoption", "governance", "data"]
    assert "(de lo que me contaste)" in outs[0].text
    assert not quiz.active("u")


def test_quiz_all_known_returns_result_right_away():
    quiz.reset()
    outs = quiz.start("u", "en", known={k: 2 for k in quiz.KEYS})
    assert "Nothing missing" in outs[0].text and "L2 · Systematic" in outs[1].text
    assert not quiz.active("u") and quiz.last_result("u").level == 2


def test_old_persisted_state_still_loads():
    st = quiz.QuizState(**{"lang": "es", "index": 2, "answers": [1, 2]})
    assert st.known == {} and quiz._question(st).text.startswith("*3/9")


# ---- end to end ------------------------------------------------------------
@respx.mock
def test_voice_note_is_transcribed_and_reaches_agent(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    mock_download()
    stt = mock_stt("¿Qué es la regla 10-20-70?")
    make_client(**VOICE_ON).post("/webhooks/meta", json=voice_payload())
    assert texts(meta) == ["echo: [Voice note] ¿Qué es la regla 10-20-70?"]
    req = stt.calls[0].request
    assert req.headers["authorization"] == "Bearer stt-key"
    assert b'filename="voice.ogg"' in req.content and b"gpt-4o-mini-transcribe" in req.content
    assert any("Transcript: ¿Qué es la regla" in json.loads(x.request.content)["text"] for x in slack.calls)


@respx.mock
def test_voice_note_without_key_gets_the_old_reply(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    stt = mock_stt("x")
    make_client().post("/webhooks/meta", json=voice_payload())
    assert "fotos" in texts(meta)[0].lower() and not stt.called


@respx.mock
@pytest.mark.parametrize("setup,fragment", [
    ("stt_down", "no pude escuchar"),
    ("empty", "no alcancé a entender"),
    ("amr", "formato de audio"),
])
def test_voice_failures_get_a_polite_reply(make_client, setup, fragment):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    mock_download(b"#!AMR\n" + b"\x00" * 20 if setup == "amr" else OGG)
    if setup == "stt_down":
        respx.post(STT).mock(return_value=httpx.Response(500, json={}))
    else:
        mock_stt("   ")
    make_client(**VOICE_ON).post("/webhooks/meta", json=voice_payload())
    assert fragment in texts(meta)[0].lower()


@respx.mock
def test_spoken_quiz_answer(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    mock_download()
    mock_stt("La tercera.")
    c = make_client(FLOWS_ENABLED="true", **VOICE_ON)
    c.post("/webhooks/meta", json=meta_payload("m0", reply_id="menu:quiz", title="Diagnóstico"))
    c.post("/webhooks/meta", json=voice_payload())
    assert texts(meta)[-1].startswith("*2/9")
    assert quiz._states[UID].answers == [2]


@respx.mock
def test_spoken_goodbye_closes_the_chat(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    mock_download()
    mock_stt("Adiós.")
    c = make_client(FLOWS_ENABLED="true", **VOICE_ON)
    c.post("/webhooks/meta", json=voice_payload())
    from app import main
    assert main.last_route[UID] == "close"


@respx.mock
def test_story_in_a_voice_note_prefills_the_assessment(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    mock_download()
    mock_stt("Somos una distribuidora de 80 personas; varios usan ChatGPT por su cuenta y no hay reglas. "
             "Hazme el diagnóstico.")
    c = make_client(FLOWS_ENABLED="true", AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test", **VOICE_ON)
    from app import agent
    fake = FakeMessages([
        _resp("tool_use", _tool("start_assessment", {"language": "es",
                                                      "known": {"adoption": 1, "governance": 0}})),
        _resp("end_turn", _text("Va, con lo que me contaste ya avanzamos.")),
    ])
    agent.get_agent()._client = SimpleNamespace(messages=fake)
    c.post("/webhooks/meta", json=voice_payload())
    first_user = fake.calls[0]["messages"][-1]["content"]
    assert first_user.startswith("[Voice note] Somos una distribuidora")
    scale = next(t for t in fake.calls[0]["tools"] if t["name"] == "start_assessment")["description"]
    assert "governance (Governance & risk): 0=None" in scale
    tool_result = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "2 domains scored" in tool_result and "only the 7 missing" in tool_result
    sent = texts(meta)
    assert sent[0].startswith("👋 Hola, Ana, soy Dexter")  # new chat: intro first (menu.intro)
    assert sent[1] == "Va, con lo que me contaste ya avanzamos."
    assert "califiqué 2 de 9" in sent[2] and sent[3].startswith("*1/7 · Estrategia")
    assert quiz._states[UID].known == {"adoption": 1, "governance": 0}


@respx.mock
def test_fully_known_assessment_sends_result_and_pdf(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "PDF1"}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true", AGENT_MODE="claude", ANTHROPIC_API_KEY="sk-test")
    from app import agent
    fake = FakeMessages([
        _resp("tool_use", _tool("start_assessment", {"language": "es", "known": {k: 1 for k in quiz.KEYS}})),
        _resp("end_turn", _text("Listo.")),
    ])
    agent.get_agent()._client = SimpleNamespace(messages=fake)
    c.post("/webhooks/meta", json=meta_payload("m1", "Te cuento todo de mi empresa y hazme el diagnóstico"))
    kinds = [json.loads(x.request.content).get("type") for x in meta.calls]
    sent = texts(meta)
    assert sent[0].startswith("👋") and sent[1] == "Listo."  # new chat: intro, then the agent's line
    assert "No me falta ninguno" in sent[2] and "L1 · Ad hoc" in sent[3]
    # result text, then the PDF, then the outro
    doc_at = kinds.index("document")
    assert kinds[doc_at - 1] == "text" and "L1 · Ad hoc" in json.loads(meta.calls[doc_at - 1].request.content)["text"]["body"]
    assert quiz.last_result(UID).inferred == quiz.KEYS
