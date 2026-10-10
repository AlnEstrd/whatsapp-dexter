"""Inbound photos / PDFs from attendees."""
import json

import httpx
import pytest
import respx

from app import attachments, docs, store
from app.agent import Agent, _with_attachments
from app.channels import MediaError
from app.channels.meta import parse_meta_payload

GRAPH = "https://graph.facebook.com/v20.0"
META_SEND = f"{GRAPH}/111/messages"
CDN = "https://lookaside.fbsbx.com/file/abc"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def pdf_bytes() -> bytes:
    return docs.make_deck("1").data


def payload(kind="image", **body):
    body = {"id": "MID1", "mime_type": "image/png", **body}
    return {"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"from": "5213312345678", "id": "wamid.m", "type": kind, kind: body}]}}]}]}


def sent_texts(meta_route):
    out = []
    for c in meta_route.calls:
        j = json.loads(c.request.content)
        if j.get("type") == "text":
            out.append(j["text"]["body"])
    return out


# ---- parsing ---------------------------------------------------------------
def test_parse_image_with_caption():
    m = parse_meta_payload(payload("image", caption="mi política")).messages[0]
    assert m.text == "mi política" and m.media == {"id": "MID1", "mime": "image/png", "filename": None}


def test_parse_document_filename():
    m = parse_meta_payload(payload("document", mime_type="application/pdf", filename="p.pdf")).messages[0]
    assert m.media["filename"] == "p.pdf" and m.msg_type == "document"


def test_parse_text_has_no_media():
    p = {"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"from": "5213312345678", "id": "w", "type": "text", "text": {"body": "hola"}}]}}]}]}
    assert parse_meta_payload(p).messages[0].media is None


# ---- validation ------------------------------------------------------------
def test_sniff_and_classify():
    assert attachments.sniff(PNG) == "image/png" and attachments.sniff(JPG) == "image/jpeg"
    assert attachments.sniff(pdf_bytes()) == "application/pdf"
    assert attachments.sniff(b"<html>") is None
    assert attachments.classify("image/jpeg; x=y") == "image"
    assert attachments.classify("audio/ogg") is None


class FakeChannel:
    def __init__(self, data=b"", err=None):
        self.data, self.err, self.limits = data, err, []

    async def download_media(self, media_id, max_bytes):
        self.limits.append(max_bytes)
        if self.err:
            raise self.err
        return self.data, "x"


async def load(data, mime, err=None):
    return await attachments.load(FakeChannel(data, err), {"id": "1", "mime": mime, "filename": "f"})


@pytest.mark.asyncio
async def test_load_valid_image_and_pdf():
    a = await load(PNG, "image/png")
    assert (a.kind, a.media_type) == ("image", "image/png")
    b = await load(pdf_bytes(), "application/pdf")
    assert b.kind == "pdf"
    blk = attachments.content_blocks(b)[0]
    assert blk["type"] == "document" and blk["source"]["media_type"] == "application/pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("data,mime,reason", [
    (b"x", "audio/ogg", "unsupported"),
    (b"MZ not an image", "image/png", "unreadable"),
    (PNG, "application/pdf", "unreadable"),          # declared type must match the bytes
    (b"%PDF-1.4 garbage", "application/pdf", "unreadable"),
])
async def test_load_rejects(data, mime, reason):
    with pytest.raises(attachments.Rejected) as e:
        await load(data, mime)
    assert e.value.reason == reason


@pytest.mark.asyncio
async def test_load_too_large_and_page_limit(monkeypatch):
    with pytest.raises(attachments.Rejected) as e:
        await load(b"", "image/png", err=MediaError("too_large"))
    assert e.value.reason == "too_large"
    with pytest.raises(MediaError):
        await load(b"", "image/png", err=MediaError("fetch_failed"))
    monkeypatch.setattr(attachments, "MAX_PDF_PAGES", 1)
    with pytest.raises(attachments.Rejected) as e:
        await load(docs.make_deck().data, "application/pdf")
    assert e.value.reason == "too_many_pages"


# ---- Meta download ---------------------------------------------------------
@pytest.mark.asyncio
@respx.mock
async def test_meta_download_media(make_client):
    make_client()
    from app.channels import get_channel
    ch = get_channel("meta")
    info = respx.get(f"{GRAPH}/MID1").mock(return_value=httpx.Response(
        200, json={"url": CDN, "mime_type": "image/png", "file_size": 70}))
    dl = respx.get(CDN).mock(return_value=httpx.Response(200, content=PNG))
    data, mime = await ch.download_media("MID1", 1000)
    assert data == PNG and mime == "image/png"
    assert info.calls[0].request.headers["authorization"] == "Bearer meta-token"
    assert dl.calls[0].request.headers["authorization"] == "Bearer meta-token"


@pytest.mark.asyncio
@respx.mock
async def test_meta_download_enforces_size_and_errors(make_client):
    make_client()
    from app.channels import get_channel
    ch = get_channel("meta")
    respx.get(f"{GRAPH}/BIG").mock(return_value=httpx.Response(
        200, json={"url": CDN, "file_size": 99999999}))
    with pytest.raises(MediaError) as e:
        await ch.download_media("BIG", 1000)
    assert e.value.reason == "too_large"
    respx.get(f"{GRAPH}/LIE").mock(return_value=httpx.Response(200, json={"url": CDN, "file_size": 1}))
    respx.get(CDN).mock(return_value=httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(MediaError) as e:
        await ch.download_media("LIE", 1000)  # header lied; caught while streaming
    assert e.value.reason == "too_large"
    respx.get(f"{GRAPH}/GONE").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(MediaError) as e:
        await ch.download_media("GONE", 1000)
    assert e.value.reason == "fetch_failed"
    respx.get(f"{GRAPH}/HTTP").mock(return_value=httpx.Response(200, json={"url": "http://x/y"}))
    with pytest.raises(MediaError):
        await ch.download_media("HTTP", 1000)  # non-https url refused


# ---- agent: files are shown for the turn only ------------------------------
def test_with_attachments_targets_last_plain_user_message():
    blocks = [{"type": "image", "source": {}}]
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}]
    out = _with_attachments(msgs, blocks)
    assert out[0]["content"] == "a" and out[2]["content"] == [*blocks, {"type": "text", "text": "c"}]
    assert msgs[2]["content"] == "c"  # input untouched


@pytest.mark.asyncio
async def test_agent_sends_file_but_stores_only_a_note(make_client, monkeypatch):
    make_client()
    seen = []

    class Resp:
        stop_reason, content = "end_turn", [type("B", (), {"type": "text", "text": "Veo una gráfica."})()]

    class Msgs:
        async def create(self, **kw):
            seen.append(kw["messages"])
            return Resp()

    agent = Agent.__new__(Agent)
    agent._model = "m"
    agent._client = type("C", (), {"messages": Msgs()})()
    att = await load(PNG, "image/png")
    reply = await agent.handle_message("u1", "qué opinas", attachments=[att])
    assert reply == "Veo una gráfica."
    assert seen[0][-1]["content"][0]["type"] == "image"
    stored = json.dumps([m for m in store.get_history("u1")], default=str)
    assert "base64" not in stored and "[Attached image" in stored


# ---- end to end ------------------------------------------------------------
@respx.mock
def test_inbound_image_reaches_agent(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{GRAPH}/MID1").mock(return_value=httpx.Response(
        200, json={"url": CDN, "mime_type": "image/png", "file_size": 70}))
    respx.get(CDN).mock(return_value=httpx.Response(200, content=PNG))
    make_client().post("/webhooks/meta", json=payload("image", caption="hola"))
    assert sent_texts(meta) == ["echo: [image] hola"]


@respx.mock
@pytest.mark.parametrize("kind,mime,fragment", [
    ("audio", "audio/ogg", "fotos"),
    ("image", "image/png", "no pude descargar"),
])
def test_inbound_failures_get_a_polite_reply(make_client, kind, mime, fragment):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{GRAPH}/MID1").mock(return_value=httpx.Response(500, json={}))
    make_client().post("/webhooks/meta", json=payload(kind, mime_type=mime))
    assert fragment in sent_texts(meta)[0].lower()


@respx.mock
def test_caption_asking_for_a_human_still_hands_off(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    slack = respx.post("https://hooks.slack.test/T/B/X").mock(return_value=httpx.Response(200))
    make_client().post("/webhooks/meta", json=payload("image", caption="humano"))
    assert slack.called and sent_texts(meta)
