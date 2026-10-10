import json
from urllib.parse import urlencode

import httpx
import pytest
import respx

from app.channels import normalize_user_id
from app.channels.twilio import compute_twilio_signature

TWILIO_SEND = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json"
META_SEND = "https://graph.facebook.com/v20.0/111/messages"
SLACK = "https://hooks.slack.test/T/B/X"


def twilio_post(client, params, token="twilio-secret", url="https://bot.example.com/webhooks/twilio"):
    sig = compute_twilio_signature(token, url, params)
    return client.post(
        "/webhooks/twilio",
        content=urlencode(params),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": sig},
    )


def tw_msg(body="hola", sid="SM1", sender="whatsapp:+5213312345678"):
    return {"From": sender, "To": "whatsapp:+14155238886", "Body": body,
            "MessageSid": sid, "NumMedia": "0", "ProfileName": "Alan"}


def test_normalize_user_id_mexico():
    assert normalize_user_id("whatsapp:+5213312345678") == "523312345678"
    assert normalize_user_id("523312345678") == "523312345678"
    assert normalize_user_id("+1 (415) 523-8886") == "14155238886"


def test_health_reports_both_channels(make_client):
    body = make_client().get("/").json()
    assert body["channels_enabled"] == ["twilio", "meta"]
    assert body["outbound_provider"] == "twilio"
    assert body["agent_mode"] == "echo"


@respx.mock
def test_twilio_inbound_echo_reply(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201, json={"sid": "SMx"}))
    r = twilio_post(make_client(), tw_msg("hola"))
    assert r.status_code == 200 and "<Response>" in r.text
    sent = dict(httpx.QueryParams(send.calls.last.request.content.decode()))
    assert sent["Body"] == "echo: hola"
    assert sent["To"] == "whatsapp:+5213312345678"     # replies to Twilio's exact address
    assert sent["From"] == "whatsapp:+14155238886"


@respx.mock
def test_twilio_bad_signature_rejected(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    r = twilio_post(make_client(), tw_msg(), token="wrong")
    assert r.status_code == 403
    assert not send.called


@respx.mock
def test_twilio_duplicate_delivery_ignored(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client()
    twilio_post(c, tw_msg(sid="SMdup"))
    twilio_post(c, tw_msg(sid="SMdup"))
    assert send.call_count == 1


@respx.mock
def test_twilio_send_retries_on_5xx(make_client):
    send = respx.post(TWILIO_SEND).mock(side_effect=[httpx.Response(503), httpx.Response(201)])
    twilio_post(make_client(), tw_msg())
    assert send.call_count == 2


@respx.mock
def test_agent_failure_escalates_and_pauses_bot(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client()
    twilio_post(c, tw_msg("fail", sid="SM1"))          # echo agent raises on "fail"
    assert "Escalation" in json.loads(slack.calls[0].request.content)["text"]
    assert "human" in dict(httpx.QueryParams(send.calls.last.request.content.decode()))["Body"]

    twilio_post(c, tw_msg("are you there?", sid="SM2"))  # in handoff: bot stays quiet
    assert send.call_count == 1
    assert "in human handoff" in json.loads(slack.calls[-1].request.content)["text"]

    # Human replies from /admin/reply and releases the user back to the bot.
    r = c.post("/admin/reply", headers={"X-Admin-Token": "admin"},
               json={"channel": "twilio", "to": "whatsapp:+5213312345678", "text": "Hi, Alan here", "release": True})
    assert r.status_code == 200
    twilio_post(c, tw_msg("hola otra vez", sid="SM3"))
    assert dict(httpx.QueryParams(send.calls.last.request.content.decode()))["Body"] == "echo: hola otra vez"


@respx.mock
def test_keyword_requests_human(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    twilio_post(make_client(), tw_msg("Humano"))
    assert "User asked for a human" in json.loads(slack.calls[0].request.content)["text"]


def test_meta_verify_challenge(make_client):
    c = make_client()
    ok = c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    legacy = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "7"})
    bad = c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert (ok.text, legacy.text, bad.status_code) == ("42", "7", 403)


@respx.mock
def test_meta_inbound_replies_on_meta(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    tw = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": "5213312345678", "profile": {"name": "Alan"}}],
        "messages": [{"from": "5213312345678", "id": "wamid.1", "type": "text", "text": {"body": "hi"}}],
    }}]}]}
    make_client().post("/webhooks/meta", json=payload)
    sends = [json.loads(c.request.content) for c in meta.calls if json.loads(c.request.content).get("type") == "text"]
    assert sends[0]["to"] == "523312345678" and sends[0]["text"]["body"] == "echo: hi"
    assert not tw.called  # answered on the channel it came in on


@respx.mock
def test_meta_account_review_event_alerts_slack(make_client):
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    payload = {"entry": [{"changes": [{"field": "account_review_update",
                                       "value": {"decision": "APPROVED"}}]}]}
    make_client().post("/webhooks/meta", json=payload)
    text = json.loads(slack.calls[0].request.content)["text"]
    assert "account_review_update" in text and "APPROVED" in text


@respx.mock
def test_meta_signature_enforced_when_secret_set(make_client):
    c = make_client(META_APP_SECRET="appsecret")
    assert c.post("/webhooks/meta", json={"entry": []}).status_code == 403


@respx.mock
def test_pivot_switch_routes_outbound(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    tw = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(WA_PROVIDER="meta")
    r = c.post("/admin/send", headers={"X-Admin-Token": "admin"}, json={"to": "5213312345678", "text": "hello"})
    assert r.json()["channel"] == "meta" and meta.called and not tw.called


def test_disabled_channel_returns_503(make_client):
    c = make_client(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="")
    assert c.post("/webhooks/twilio", content="From=whatsapp%3A%2B1").status_code == 503
    assert c.get("/").json()["channels_enabled"] == ["meta"]


def test_admin_requires_token(make_client):
    c = make_client()
    assert c.post("/admin/send", json={"to": "1", "text": "x"}).status_code == 403


def test_twilio_signature_matches_official_vector():
    # Example from Twilio's request-validation docs / twilio-python tests.
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
              "From": "+14158675310", "To": "+18005551212"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert compute_twilio_signature("12345", url, params) == "GvWf1cFY/Q7PnoempGyD5oXAezc="


def _typing_calls(route):
    bodies = [json.loads(c.request.content) for c in route.calls]
    return [b for b in bodies if "typing_indicator" in b]


@respx.mock
def test_meta_shows_typing_while_agent_runs(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": "5213312345678", "profile": {"name": "Alan"}}],
        "messages": [{"from": "5213312345678", "id": "wamid.T1", "type": "text", "text": {"body": "hola"}}],
    }}]}]}
    make_client().post("/webhooks/meta", json=payload)
    typing = _typing_calls(meta)
    assert typing and typing[0] == {
        "messaging_product": "whatsapp", "status": "read",
        "message_id": "wamid.T1", "typing_indicator": {"type": "text"},
    }
    bodies = [json.loads(c.request.content) for c in meta.calls]
    first_text = next(i for i, b in enumerate(bodies) if b.get("type") == "text")
    assert bodies.index(typing[0]) < first_text  # dots appear before the reply


@respx.mock
def test_typing_failure_does_not_block_reply(make_client):
    def responder(request):
        body = json.loads(request.content)
        return httpx.Response(500 if "typing_indicator" in body else 200, json={})
    meta = respx.post(META_SEND).mock(side_effect=responder)
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "5213312345678", "id": "wamid.T2", "type": "text", "text": {"body": "hi"}}],
    }}]}]}
    make_client().post("/webhooks/meta", json=payload)
    texts = [json.loads(c.request.content) for c in meta.calls if json.loads(c.request.content).get("type") == "text"]
    assert texts and texts[0]["text"]["body"] == "echo: hi"


@respx.mock
def test_no_typing_while_in_human_handoff(make_client):
    from app import store
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    client = make_client()
    store.start_handoff("523312345678", "slack")
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "5213312345678", "id": "wamid.T3", "type": "text", "text": {"body": "sigues ahi?"}}],
    }}]}]}
    client.post("/webhooks/meta", json=payload)
    assert not _typing_calls(meta)


def test_split_bubbles():
    from app.main import split_bubbles
    assert split_bubbles("una sola idea") == ["una sola idea"]
    assert split_bubbles("a\n\nb") == ["a", "b"]
    assert split_bubbles("a\n\nb\n\nc\n\nd") == ["a", "b", "c\n\nd"]
    assert split_bubbles("intro\n\n• uno\n• dos") == ["intro", "• uno\n• dos"]


@respx.mock
def test_meta_multi_paragraph_reply_goes_out_as_bubbles(make_client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MIN", 0)
    monkeypatch.setattr(main, "BUBBLE_PAUSE_MAX", 0)
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "5213312345678", "id": "wamid.B1", "type": "text",
                      "text": {"body": "primera idea\n\n¿seguimos?"}}],
    }}]}]}
    make_client().post("/webhooks/meta", json=payload)
    bodies = [json.loads(c.request.content) for c in meta.calls]
    texts = [b["text"]["body"] for b in bodies if b.get("type") == "text"]
    assert texts == ["echo: primera idea", "¿seguimos?"]
    i1 = next(i for i, b in enumerate(bodies) if b.get("type") == "text")
    assert any("typing_indicator" in b for b in bodies[i1 + 1:])  # dots between bubbles
