import json

import httpx
import respx

from tests.test_channels import SLACK, TWILIO_SEND, tw_msg, twilio_post

CONV = "https://conversations.twilio.com/v1/Conversations"
CH_NEW = "CH" + "a" * 32
CH_OLD = "CH" + "b" * 32
INTERACTIONS = "https://flex-api.twilio.com/v1/Interactions"
FLEX_ENV = {"HANDOFF_TARGET": "flex", "FLEX_WORKSPACE_SID": "WS123", "FLEX_WORKFLOW_SID": "WW123"}


def form(call):
    return dict(httpx.QueryParams(call.request.content.decode()))


def mock_flex_ok():
    conv = respx.post(CONV).mock(return_value=httpx.Response(201, json={"sid": CH_NEW}))
    part = respx.post(f"{CONV}/{CH_NEW}/Participants").mock(return_value=httpx.Response(201, json={"sid": "MB1"}))
    inter = respx.post(INTERACTIONS).mock(return_value=httpx.Response(201, json={"sid": "KD1"}))
    return conv, part, inter


@respx.mock
def test_health_shows_flex(make_client):
    assert make_client(**FLEX_ENV).get("/").json()["handoff_target"] == "flex"


@respx.mock
def test_keyword_hands_off_to_flex(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    conv, part, inter = mock_flex_ok()
    c = make_client(**FLEX_ENV)

    twilio_post(c, tw_msg("hola", sid="SM1"))      # bot turn -> goes into transcript
    twilio_post(c, tw_msg("humano", sid="SM2"))

    p = form(part.calls.last)
    assert p["MessagingBinding.Address"] == "whatsapp:+5213312345678"
    assert p["MessagingBinding.ProxyAddress"] == "whatsapp:+14155238886"
    assert form(conv.calls.last)["Timers.Closed"] == "PT24H"

    body = form(inter.calls.last)
    channel, routing = json.loads(body["Channel"]), json.loads(body["Routing"])["properties"]
    assert channel == {"type": "whatsapp", "initiated_by": "customer", "properties": {"media_channel_sid": CH_NEW}}
    assert routing["workspace_sid"] == "WS123" and routing["workflow_sid"] == "WW123"
    attrs = routing["attributes"]
    assert attrs["handoffReason"] == "User asked for a human"
    assert "user: hola" in attrs["botTranscript"] and "bot: echo: hola" in attrs["botTranscript"]

    assert "human" in form(send.calls.last)["Body"]           # user told they're handed off
    assert "Routed" in json.loads(slack.calls.last.request.content)["text"]  # FYI in Slack


@respx.mock
def test_existing_binding_reuses_conversation(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    respx.post(CONV).mock(return_value=httpx.Response(201, json={"sid": CH_NEW}))
    respx.post(f"{CONV}/{CH_NEW}/Participants").mock(return_value=httpx.Response(409, json={
        "code": 50416,
        "message": f"A binding for this participant and proxy address already exists in Conversation {CH_OLD}"}))
    close = respx.post(f"{CONV}/{CH_NEW}").mock(return_value=httpx.Response(200, json={}))
    inter = respx.post(INTERACTIONS).mock(return_value=httpx.Response(201, json={}))

    twilio_post(make_client(**FLEX_ENV), tw_msg("humano"))
    assert form(close.calls.last)["State"] == "closed"
    assert json.loads(form(inter.calls.last)["Channel"])["properties"]["media_channel_sid"] == CH_OLD


@respx.mock
def test_flex_failure_falls_back_to_slack(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    respx.post(CONV).mock(return_value=httpx.Response(401, json={"code": 20003, "message": "Authenticate"}))
    c = make_client(**FLEX_ENV)

    twilio_post(c, tw_msg("fail", sid="SM1"))          # agent error -> handoff
    text = json.loads(slack.calls.last.request.content)["text"]
    assert "Escalation" in text and "Flex handoff failed" in text
    assert "human" in form(send.calls.last)["Body"]

    # Slack-mode handoff: bot stays quiet on the next message
    twilio_post(c, tw_msg("hello?", sid="SM2"))
    assert send.call_count == 1


@respx.mock
def test_inbound_after_flex_closes_returns_to_bot(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    mock_flex_ok()
    c = make_client(**FLEX_ENV)

    twilio_post(c, tw_msg("humano", sid="SM1"))
    # Message reaching our webhook again == Flex conversation was closed
    twilio_post(c, tw_msg("gracias", sid="SM2"))
    assert form(send.calls.last)["Body"] == "echo: gracias"
    assert "back with the bot" in json.loads(slack.calls[-1].request.content)["text"]


@respx.mock
def test_meta_users_use_slack_even_when_flex_enabled(make_client):
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(return_value=httpx.Response(200, json={}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    conv = respx.post(CONV).mock(return_value=httpx.Response(201, json={"sid": CH_NEW}))
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "5213312345678", "id": "wamid.9", "type": "text", "text": {"body": "humano"}}]}}]}]}
    make_client(**FLEX_ENV).post("/webhooks/meta", json=payload)
    assert not conv.called
    assert "Escalation" in json.loads(slack.calls.last.request.content)["text"]
