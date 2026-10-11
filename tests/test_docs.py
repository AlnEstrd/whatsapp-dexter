"""PDF documents: slides, topic one-pagers, assessment report, and WhatsApp delivery."""
import io
import json

import httpx
import pytest
import respx
from pypdf import PdfReader

from app import agent, docs
from app.docs import slides, topic
from app.flows import menu, quiz

from .test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from .test_flows import N, meta_payload, meta_sends

META_MEDIA = META_SEND.replace("/messages", "/media")
UID = "523312345678"


def pdf_text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def pages(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def meta_files(route, kind):
    return [json.loads(c.request.content) for c in route.calls
            if c.request.content.startswith(b"{") and json.loads(c.request.content).get("type") == kind]


def meta_docs(route):
    return meta_files(route, "document")


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #
def test_whole_deck_has_one_page_per_slide():
    d = docs.make_deck()
    assert d.data.startswith(b"%PDF-") and pages(d.data) == len(slides.SLIDES) == 13
    assert d.filename == "Gobernanza-de-IA-laminas.pdf"
    text = pdf_text(d.data)
    assert "Rol = responsabilidades + actividades" in text and "Hoy, con la IA, los computers somos todos." in text


@pytest.mark.parametrize("sid", [s["id"] for s in slides.SLIDES])
def test_every_slide_fits_on_one_page(sid):
    d = docs.make_deck(sid)  # raises ValueError if the slide overflows
    assert pages(d.data) == 1 and d.filename == f"Gobernanza-de-IA-lamina-{sid.lower()}.pdf"


@pytest.mark.parametrize("raw,expected", [
    ("lámina 8", "8"), ("Slide 3", "3"), ("anexo a", "A"), ("B", "B"), ("08", "8"),
    ("90 días", "90"), ("todas", None), ("all", None), ("", None), (None, None),
])
def test_resolve_slide_references(raw, expected):
    assert slides.resolve(raw) == expected


def test_unknown_slide_is_rejected():
    with pytest.raises(ValueError):
        docs.make_deck("77")


def test_slide_request_shortcut():
    assert menu.slide_request("lámina 8") == ("8", "image")
    assert menu.slide_request("Slide A!") == ("A", "image")
    assert menu.slide_request("lámina 8 pdf") == ("8", "pdf")
    assert menu.slide_request("slide 3 image") == ("3", "image")
    assert menu.slide_request("¿Qué dice la lámina 8?") is None
    assert menu.slide_request("lámina") is None


@pytest.mark.parametrize("sid", [s["id"] for s in slides.SLIDES])
def test_slide_image_is_a_png_within_whatsapp_limits(sid):
    from PIL import Image
    d = docs.make_slide_image(sid)
    im = Image.open(io.BytesIO(d.data))
    assert d.data.startswith(b"\x89PNG") and im.size == (1280, 720) and len(d.data) < 1_000_000
    assert d.mime == "image/png" and d.filename == f"Gobernanza-de-IA-lamina-{sid.lower()}.png"
    assert "pdf" in d.caption.lower()


def test_slide_image_needs_one_valid_slide():
    for bad in ("all", "", "77"):
        with pytest.raises(ValueError):
            docs.make_slide_image(bad)


def test_captions_follow_language():
    assert "(in Spanish)" in docs.make_deck("5", "en").caption and "Lámina 5" in docs.make_deck("5", "es").caption


# --------------------------------------------------------------------------- #
# Assessment report
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("answers", [[0] * 9, [1] * 9, [2] * 9, [3] * 9, [3, 3, 3, 1, 3, 3, 3, 3, 3]])
def test_assessment_report_is_multipage_and_matches_result(answers, lang):
    r = quiz.score(answers, lang)
    d = docs.make_assessment(r, "Ana López")
    text = pdf_text(d.data)
    assert pages(d.data) >= 3
    assert f"L{r.level}" in text and f"{r.average:.1f}" in text and "Ana López" in text
    for q in quiz.QUESTIONS:
        assert q["domain"][lang] in text
        assert q["opts"][lang][r.answers[q["key"]]][0] in text      # the answer the person picked
    for k in r.weakest:
        assert quiz.TIPS[k][lang][:40] in text                      # next steps for weakest domains
    assert ("Ana López" in d.caption) is False and f"{r.average:.1f}/3" in d.caption


def test_assessment_report_shows_gate_note_only_when_capped():
    capped = pdf_text(docs.make_assessment(quiz.score([3, 3, 3, 1, 3, 3, 3, 3, 3], "es")).data)
    clean = pdf_text(docs.make_assessment(quiz.score([2] * 9, "es")).data)
    assert "topado en L1" in capped and "topado en L1" not in clean


def test_assessment_report_handles_odd_names():
    d = docs.make_assessment(quiz.score([1] * 9, "es"), "Zoë 😀 <b>&</b> 张伟")
    assert pages(d.data) >= 3


# --------------------------------------------------------------------------- #
# Topic one-pager
# --------------------------------------------------------------------------- #
SPEC = {
    "language": "es", "title": "La regla 10-20-70", "summary": "El valor está en personas y procesos.",
    "figures": [{"value": "74%", "label": "sin valor tangible", "source": "BCG, 2024"}],
    "sections": [{"heading": "Qué dice", "paragraphs": ["70% personas y procesos."], "bullets": ["10% algoritmos"]}],
    "table": {"headers": ["Capa", "Peso"], "rows": [["Algoritmos", "10%"]]},
    "sources": ["BCG, oct 2024"],
}


def test_topic_pdf_renders_content_and_sources():
    d = docs.make_topic(SPEC)
    text = pdf_text(d.data)
    assert pages(d.data) == 1 and d.filename == "La-regla-10-20-70.pdf"
    assert "La regla 10-20-70" in text and "BCG, 2024" in text and "Fuentes" in text


def test_topic_spec_is_validated():
    for bad in ({}, {"title": "x"}, {"title": "x", "sections": [{"heading": "h"}]}, "nope"):
        with pytest.raises(ValueError):
            docs.make_topic(bad)


def test_topic_spec_is_clipped():
    spec = {"title": "T" * 500, "summary": "S" * 5000, "sources": ["x"] * 50,
            "sections": [{"heading": "H", "paragraphs": ["p" * 5000], "bullets": ["b"] * 100}] + [
                {"heading": "H", "bullets": ["b"]}] * 20,
            "figures": [{"value": "1", "label": "l"}] * 20}
    clean = topic.validate(spec)
    assert len(clean["title"]) <= 90 and len(clean["sections"]) == 5 and len(clean["sources"]) == 8
    assert len(clean["figures"]) == 4 and len(clean["sections"][0]["bullets"]) == 7
    assert len(clean["sections"][0]["paragraphs"][0]) <= 700


def test_topic_too_long_asks_the_agent_to_shorten():
    long = {"title": "t", "sections": [{"heading": "h", "paragraphs": ["palabra " * 400] * 3,
                                        "bullets": ["palabra " * 60] * 7}] * 5}
    with pytest.raises(ValueError, match="too long"):
        docs.make_topic(long)


def test_topic_at_the_word_budget_stays_within_three_pages():
    para = " ".join(["palabra"] * 120)                      # ~120 words, under the paragraph cap
    spec = {"title": "t", "summary": para, "sources": ["BCG"] * 8,
            "sections": [{"heading": "h", "paragraphs": [para], "bullets": ["uno dos tres"] * 7}] * 3}
    assert topic._word_count(topic.validate(spec)) <= topic.MAX_WORDS
    assert pages(topic.build_topic_pdf(spec)) <= 3


def test_topic_text_is_not_interpreted_as_markup():
    spec = {"title": "A <b>bold</b> & more → ≥ 😀", "sections": [{"heading": "<i>h</i>", "bullets": ["<font color=red>x</font>"]}]}
    text = pdf_text(docs.make_topic(spec).data)
    assert "<b>bold</b>" in text and "<font color=red>x</font>" in text and "->" in text


# --------------------------------------------------------------------------- #
# Meta delivery
# --------------------------------------------------------------------------- #
@respx.mock
async def test_meta_send_document_uploads_media_then_sends_by_id(make_client):
    make_client()
    from app.channels import get_channel
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "MEDIA1"}))
    send = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    await get_channel("meta").send_document("5213312345678", b"%PDF-1.4 test", "reporte.pdf", "📄 Hola")
    body = media.calls.last.request.content
    assert b'name="messaging_product"' in body and b"application/pdf" in body and b'filename="reporte.pdf"' in body
    assert media.calls.last.request.headers["authorization"] == "Bearer meta-token"
    assert json.loads(send.calls.last.request.content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": UID, "type": "document",
        "document": {"id": "MEDIA1", "filename": "reporte.pdf", "caption": "📄 Hola"}}


@respx.mock
async def test_meta_upload_without_id_is_an_error(make_client):
    from app.channels import SendError, get_channel
    make_client()
    respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(SendError):
        await get_channel("meta").send_document("5213312345678", b"x", "a.pdf")


@respx.mock
def test_menu_slides_sends_the_deck_then_an_index(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "M1"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("d1", reply_id="menu:slides", title="Láminas en PDF"))
    assert media.call_count == 1 and b"Gobernanza-de-IA-laminas.pdf" in media.calls.last.request.content
    sent = meta_docs(meta)
    assert sent[0]["document"]["id"] == "M1" and "13 páginas" in sent[0]["document"]["caption"]
    assert "lámina 8" in meta_sends(meta)[-1]["text"]["body"]


@respx.mock
async def test_meta_send_image_uploads_media_then_sends_by_id(make_client):
    make_client()
    from app.channels import get_channel
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "IMG1"}))
    send = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    await get_channel("meta").send_image("5213312345678", b"\x89PNG fake", "s.png", "📄 Hola", mime="image/png")
    assert b"image/png" in media.calls.last.request.content and b'filename="s.png"' in media.calls.last.request.content
    assert json.loads(send.calls.last.request.content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": UID, "type": "image",
        "image": {"id": "IMG1", "caption": "📄 Hola"}}


@respx.mock
def test_typed_slide_request_sends_that_slide_as_an_image(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "M8"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    make_client(FLOWS_ENABLED="true").post("/webhooks/meta", json=meta_payload("d2", "lámina 8"))
    assert b"Gobernanza-de-IA-lamina-8.png" in media.calls.last.request.content
    assert b"image/png" in media.calls.last.request.content
    [img] = meta_files(meta, "image")
    assert img["image"]["id"] == "M8" and img["image"]["caption"].startswith("📄 Lámina 8")
    assert not meta_docs(meta)


@respx.mock
def test_slide_with_pdf_suffix_sends_the_pdf(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "P8"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    make_client(FLOWS_ENABLED="true").post("/webhooks/meta", json=meta_payload("d2b", "lámina 8 pdf"))
    assert b"Gobernanza-de-IA-lamina-8.pdf" in media.calls.last.request.content
    assert meta_docs(meta)[0]["document"]["caption"].startswith("📄 Lámina 8") and not meta_files(meta, "image")


@respx.mock
def test_unknown_slide_lists_the_available_ones(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "M"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    make_client(FLOWS_ENABLED="true").post("/webhooks/meta", json=meta_payload("d3", "lámina 77"))
    assert not media.called
    assert "No encontré esa lámina" in meta_sends(meta)[-1]["text"]["body"]


@respx.mock
def test_quiz_completion_sends_the_report_between_result_and_follow_up(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "R1"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("q0", reply_id="menu:quiz", title="x"))
    for i in range(N):
        c.post("/webhooks/meta", json=meta_payload(f"q{i + 1}", reply_id=f"quiz:{i}:3", title="x"))
    def first(pred) -> int:
        bodies = [json.loads(x.request.content) for x in meta.calls if x.request.content.startswith(b"{")]
        return next(i for i, m in enumerate(bodies) if pred(m))

    def text_of(m: dict) -> str:
        return (m.get("text") or {}).get("body", "")

    result_i = first(lambda m: "Tu resultado" in text_of(m))
    doc_i = first(lambda m: m.get("type") == "document")
    outro_i = first(lambda m: "profundizar" in text_of(m))
    assert result_i < doc_i < outro_i
    assert b"Diagnostico-madurez-IA.pdf" in media.calls.last.request.content
    assert "L2" in meta_docs(meta)[0]["document"]["caption"]


@respx.mock
def test_report_command_resends_or_explains(make_client):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "R"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("r1", "pdf"))
    assert not media.called and "Aún no has hecho el diagnóstico" in meta_sends(meta)[-1]["text"]["body"]
    quiz._results[UID] = quiz.score([2] * 9, "es")
    c.post("/webhooks/meta", json=meta_payload("r2", "reporte"))
    assert media.call_count == 1 and b"Diagnostico-madurez-IA.pdf" in media.calls.last.request.content


@respx.mock
def test_failed_upload_degrades_to_text_and_chat_continues(make_client):
    respx.post(META_MEDIA).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("f1", "slides"))
    assert "No pude enviar el archivo" in meta_sends(meta)[-1]["text"]["body"]
    c.post("/webhooks/meta", json=meta_payload("f2", "Hola"))          # still answers afterwards
    assert meta_sends(meta)[-1]["type"] == "interactive"


@respx.mock
def test_twilio_explains_it_cannot_send_files_but_quiz_stays_quiet(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    twilio_post(c, tw_msg("slides", sid="T0"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert "no puedo enviar archivos" in bodies[-1]
    twilio_post(c, tw_msg("quiz", sid="T1"))
    for i in range(N):
        twilio_post(c, tw_msg("1", sid=f"T{i + 2}"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert sum("no puedo enviar archivos" in b for b in bodies) == 1     # only the explicit request


# --------------------------------------------------------------------------- #
# Agent tools
# --------------------------------------------------------------------------- #
def test_agent_send_slides_queues_an_image_or_a_pdf(make_client):
    make_client()
    assert "queued" in agent.run_tool("u1", "send_slides", {"slide": "5"})          # single slide -> image
    [d] = agent.pop_documents("u1")
    assert d.filename == "Gobernanza-de-IA-lamina-5.png" and d.mime == "image/png"
    assert agent.pop_documents("u1") == []
    agent.run_tool("u1", "send_slides", {"slide": "5", "format": "pdf"})
    assert agent.pop_documents("u1")[0].filename == "Gobernanza-de-IA-lamina-5.pdf"
    agent.run_tool("u1", "send_slides", {"slide": "all", "format": "image"})        # the deck is always a PDF
    assert agent.pop_documents("u1")[0].filename == "Gobernanza-de-IA-laminas.pdf"
    assert agent.run_tool("u1", "send_slides", {"slide": "99"}).startswith("error:")
    assert agent.pop_documents("u1") == []


def test_agent_topic_pdf_validates_and_caps(make_client):
    make_client()
    assert agent.run_tool("u1", "create_topic_pdf", {"title": "x"}).startswith("error:")
    for _ in range(agent.MAX_DOCS_PER_TURN):
        assert "queued" in agent.run_tool("u1", "create_topic_pdf", SPEC)
    assert agent.run_tool("u1", "create_topic_pdf", SPEC).startswith("error:")
    assert len(agent.pop_documents("u1")) == agent.MAX_DOCS_PER_TURN


def test_agent_report_needs_a_result(make_client):
    make_client()
    assert "hasn't completed" in agent.run_tool("u1", "send_assessment_report", {})
    assert agent.pop_action("u1") is None
    quiz._results["u1"] = quiz.score([2] * 9, "en")
    agent.run_tool("u1", "send_assessment_report", {})
    assert agent.pop_action("u1") == {"action": "report"}


@respx.mock
def test_agent_documents_are_sent_after_the_text_reply(make_client, monkeypatch):
    media = respx.post(META_MEDIA).mock(return_value=httpx.Response(200, json={"id": "A1"}))
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))

    class Fake:
        async def handle_message(self, user_id, text):
            agent.run_tool(user_id, "send_slides", {"slide": "9"})
            agent.run_tool(user_id, "create_topic_pdf", SPEC)
            return "Te envío la lámina 9 y un resumen."

    c = make_client(FLOWS_ENABLED="true")
    monkeypatch.setattr("app.main.get_agent", lambda: Fake())
    c.post("/webhooks/meta", json=meta_payload("g1", "mándame la lámina de medición y un resumen del 10-20-70"))
    calls = [x.request.content for x in meta.calls if x.request.content.startswith(b"{")]
    kinds = [json.loads(x).get("type") for x in calls if json.loads(x).get("status") is None]
    assert kinds[:4] == ["text", "text", "image", "document"]  # intro, reply, files
    assert media.call_count == 2


@respx.mock
def test_twilio_slide_request_falls_back_to_text(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    twilio_post(make_client(FLOWS_ENABLED="true"), tw_msg("lámina 8", sid="TS1"))
    bodies = [dict(httpx.QueryParams(x.request.content.decode()))["Body"] for x in send.calls]
    assert "no puedo enviar archivos" in bodies[-1]
