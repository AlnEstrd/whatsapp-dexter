"""Slack thread-per-conversation bridge (SLACK_BOT_TOKEN set)."""
import hashlib
import hmac
import json
import time

import httpx
import pytest
import respx

from app import store
from tests.test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post

POST = "https://slack.com/api/chat.postMessage"
REACT = "https://slack.com/api/reactions.add"
SECRET = "slack-signing-secret"
BOT_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": SECRET,
    "SLACK_CHANNEL_ID": "C123",
    "SLACK_ALERT_USER_ID": "UOWNER",
}
USER = "523312345678"
OUR_BOT = "UBOT"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class SlackMock:
    """chat.postMessage mock that hands out increasing ts values."""

    def __init__(self):
        self.n = 0
        self.route = respx.post(POST).mock(side_effect=self._reply)
        self.react = respx.post(REACT).mock(return_value=httpx.Response(200, json={"ok": True}))

    def _reply(self, request):
        self.n += 1
        return httpx.Response(200, json={"ok": True, "ts": f"1700000000.{self.n:06d}"})

    @property
    def posts(self):
        return [json.loads(c.request.content) for c in self.route.calls]

    def parents(self):
        return [p for p in self.posts if "thread_ts" not in p]

    def replies(self):
        return [p for p in self.posts if "thread_ts" in p]

    def reactions(self):
        return [json.loads(c.request.content) for c in self.react.calls]


def sign(body: bytes, ts: str | None = None, secret: str = SECRET):
    ts = ts or str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


_eid = 0


def slack_event(client, event, event_id=None, headers=None):
    global _eid
    _eid += 1
    payload = {"type": "event_callback", "event_id": event_id or f"Ev{_eid}",
               "authorizations": [{"user_id": OUR_BOT, "is_bot": True}], "event": event}
    body = json.dumps(payload).encode()
    return client.post("/slack/events", content=body, headers={**sign(body), **(headers or {})})


def thread_reply(text, thread_ts, user="UOWNER", ts="1700000099.000001", **extra):
    return {"type": "message", "channel": "C123", "user": user, "text": text,
            "thread_ts": thread_ts, "ts": ts, **extra}


def reaction(name, item_ts, user="UOWNER"):
    return {"type": "reaction_added", "user": user, "reaction": name,
            "item": {"type": "message", "channel": "C123", "ts": item_ts}}


def tw_sent(send):
    return [dict(httpx.QueryParams(c.request.content.decode())) for c in send.calls]


def parent_ts():
    return store.get_thread(USER)["thread_ts"]


@pytest.fixture
def bot_client(make_client):
    return make_client(**BOT_ENV)


# --------------------------------------------------------------------------- #
# threads
# --------------------------------------------------------------------------- #
@respx.mock
def test_first_message_creates_thread_second_reuses_idle_reopens(bot_client, monkeypatch):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("hola", sid="SM1"))
    twilio_post(bot_client, tw_msg("otra", sid="SM2"))

    parents = sm.parents()
    assert len(parents) == 1
    assert parents[0]["text"] == f"💬 New chat — Alan (+{USER}) · twilio"
    assert parents[0]["channel"] == "C123"
    first = parent_ts()
    texts = [p["text"] for p in sm.replies() if p["thread_ts"] == first]
    assert texts == ["👤 hola", "🤖 echo: hola", "👤 otra", "🤖 echo: otra"]
    assert not any(p.get("reply_broadcast") or "<@" in p["text"] for p in sm.replies())

    real_now = time.time
    monkeypatch.setattr(store, "now", lambda: real_now() + 25 * 3600)
    twilio_post(bot_client, tw_msg("volví", sid="SM3"))
    assert len(sm.parents()) == 2
    assert parent_ts() != first
    assert store.user_for_thread(parent_ts()) == USER


@respx.mock
def test_customer_text_escaped_and_non_text_labelled(make_client):
    respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    sm = SlackMock()
    c = make_client(**BOT_ENV)
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [
            {"from": "5213312345678", "id": "wamid.1", "type": "text", "text": {"body": "a<b> & <@U1>"}},
            {"from": "5213312345678", "id": "wamid.2", "type": "image", "image": {}},
        ]}}]}]}
    c.post("/webhooks/meta", json=payload)
    texts = [p["text"] for p in sm.replies()]
    assert "👤 a&lt;b&gt; &amp; &lt;@U1&gt;" in texts
    assert "👤 (image) 📎 image" in texts
    assert sm.parents()[0]["text"].endswith("· meta")


# --------------------------------------------------------------------------- #
# escalation
# --------------------------------------------------------------------------- #
@respx.mock
def test_escalation_mentions_owner_with_broadcast(bot_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    webhook = respx.post(SLACK).mock(return_value=httpx.Response(200))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))

    esc = [p for p in sm.replies() if p["text"].startswith("🚨")]
    assert len(esc) == 1
    assert esc[0]["reply_broadcast"] is True
    assert "<@UOWNER> escalated: User asked for a human" in esc[0]["text"]
    assert store.get_mode(USER) == "human"
    assert not webhook.called  # threaded path replaces the webhook


@respx.mock
def test_agent_error_escalates_in_thread(bot_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("fail", sid="SM1"))
    assert any("escalated: Agent error" in p["text"] for p in sm.replies())


# --------------------------------------------------------------------------- #
# take / human replies / bot
# --------------------------------------------------------------------------- #
@respx.mock
def test_take_text_pauses_bot_and_human_reply_goes_to_whatsapp(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("hola", sid="SM1"))
    pts = parent_ts()

    slack_event(bot_client, thread_reply("Take", pts, ts="1700000099.000001"))
    assert store.get_mode(USER) == "human"
    assert any(p["text"].startswith("✋ <@UOWNER> took control") for p in sm.replies())

    # customer writes: mirrored, but the agent is not called
    twilio_post(bot_client, tw_msg("sigues ahí?", sid="SM2"))
    assert [s["Body"] for s in tw_sent(send)] == ["echo: hola"]
    assert "👤 sigues ahí?" in [p["text"] for p in sm.replies()]

    slack_event(bot_client, thread_reply("Hola, soy Alan &amp; equipo", pts, ts="1700000099.000002"))
    last = tw_sent(send)[-1]
    assert last["Body"] == "Hola, soy Alan & equipo"
    assert last["To"] == "whatsapp:+5213312345678"
    assert sm.reactions()[-1] == {"channel": "C123", "timestamp": "1700000099.000002", "name": "white_check_mark"}
    assert ("human", "Hola, soy Alan & equipo") in store.transcript(USER)


@respx.mock
def test_raised_hand_reaction_takes_control(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    twilio_post(bot_client, tw_msg("hola", sid="SM1"))
    slack_event(bot_client, reaction("raised_hand", parent_ts()))
    assert store.get_mode(USER) == "human"
    twilio_post(bot_client, tw_msg("otra", sid="SM2"))
    assert send.call_count == 1  # bot quiet


@respx.mock
def test_thread_reply_ignored_in_bot_mode(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    twilio_post(bot_client, tw_msg("hola", sid="SM1"))
    slack_event(bot_client, thread_reply("internal note", parent_ts()))
    assert send.call_count == 1


@respx.mock
def test_bot_text_and_check_reaction_resume(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))
    pts = parent_ts()

    slack_event(bot_client, thread_reply("bot", pts))
    assert store.get_mode(USER) == "bot" and not store.in_handoff(USER)
    assert "🤖 Bot resumed." in [p["text"] for p in sm.replies()]
    twilio_post(bot_client, tw_msg("hola", sid="SM2"))
    assert tw_sent(send)[-1]["Body"] == "echo: hola"

    slack_event(bot_client, reaction("raised_hand", pts))
    assert store.get_mode(USER) == "human"
    slack_event(bot_client, reaction("white_check_mark", pts))
    assert store.get_mode(USER) == "bot"


@respx.mock
def test_own_bot_messages_and_duplicates_ignored(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))
    pts = parent_ts()
    before = send.call_count

    slack_event(bot_client, thread_reply("from our bot user", pts, user=OUR_BOT))
    slack_event(bot_client, thread_reply("has bot_id", pts, user="UX", bot_id="B1"))
    slack_event(bot_client, thread_reply("edited", pts, subtype="message_changed"))
    assert send.call_count == before

    slack_event(bot_client, thread_reply("once", pts, ts="1700000099.000005"), event_id="EvDUP")
    slack_event(bot_client, thread_reply("once", pts, ts="1700000099.000005"), event_id="EvDUP",
                headers={"X-Slack-Retry-Num": "1"})
    assert send.call_count == before + 1


@respx.mock
def test_human_reply_window_closed_error_reported(make_client):
    respx.post(META_SEND).mock(return_value=httpx.Response(400, json={
        "error": {"code": 131047, "message": "Re-engagement message"}}))
    sm = SlackMock()
    c = make_client(**BOT_ENV)
    store.start_handoff(USER, "slack")
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "5213312345678", "id": "wamid.1", "type": "text", "text": {"body": "hi"}}]}}]}]}
    c.post("/webhooks/meta", json=payload)
    slack_event(c, thread_reply("respuesta", parent_ts(), ts="1700000099.000007"))
    assert any("131047" in p["text"] for p in sm.replies())
    assert sm.reactions()[-1]["name"] == "warning"


@respx.mock
def test_window_warning_once(bot_client, monkeypatch):
    import asyncio

    from app import slack_bridge
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))
    real_now = time.time
    monkeypatch.setattr(store, "now", lambda: real_now() + 23.5 * 3600)
    asyncio.run(slack_bridge.check_windows())
    asyncio.run(slack_bridge.check_windows())
    warns = [p for p in sm.replies() if p["text"].startswith("⏰")]
    assert len(warns) == 1 and "<@UOWNER>" in warns[0]["text"]


@respx.mock
def test_admin_reply_mirrors_into_thread(bot_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))
    r = bot_client.post("/admin/reply", headers={"X-Admin-Token": "admin"},
                        json={"channel": "twilio", "to": "whatsapp:+5213312345678",
                              "text": "Hola desde admin", "release": True})
    assert r.status_code == 200
    texts = [p["text"] for p in sm.replies()]
    assert "🧑 admin: Hola desde admin" in texts and "🤖 Bot resumed." in texts
    assert store.get_mode(USER) == "bot"


# --------------------------------------------------------------------------- #
# endpoint security / fallback
# --------------------------------------------------------------------------- #
def test_signature_verification_and_challenge(bot_client):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    ok = bot_client.post("/slack/events", content=body, headers=sign(body))
    assert ok.status_code == 200 and ok.json() == {"challenge": "abc123"}

    bad = bot_client.post("/slack/events", content=body, headers=sign(body, secret="wrong"))
    assert bad.status_code == 403

    stale = bot_client.post("/slack/events", content=body, headers=sign(body, ts=str(int(time.time()) - 600)))
    assert stale.status_code == 403

    missing = bot_client.post("/slack/events", content=body)
    assert missing.status_code == 403


@respx.mock
def test_without_bot_token_legacy_webhook_unchanged(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    webhook = respx.post(SLACK).mock(return_value=httpx.Response(200))
    api = respx.post(POST).mock(return_value=httpx.Response(200, json={"ok": True, "ts": "1"}))
    c = make_client()
    assert c.get("/").json()["slack_threads"] is False
    twilio_post(c, tw_msg("humano", sid="SM1"))
    assert "Escalation" in json.loads(webhook.calls.last.request.content)["text"]
    assert not api.called


@respx.mock
def test_slack_api_failure_never_breaks_whatsapp(bot_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(POST).mock(return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"}))
    twilio_post(bot_client, tw_msg("hola", sid="SM1"))
    assert tw_sent(send)[-1]["Body"] == "echo: hola"


@respx.mock
def test_provider_alert_uses_bot_token_when_no_webhook(make_client):
    sm = SlackMock()
    c = make_client(SLACK_WEBHOOK_URL="", **BOT_ENV)
    payload = {"entry": [{"changes": [{"field": "account_review_update",
                                       "value": {"decision": "APPROVED"}}]}]}
    c.post("/webhooks/meta", json=payload)
    assert len(sm.parents()) == 1 and "account_review_update" in sm.parents()[0]["text"]


# --------------------------------------------------------------------------- #
# close
# --------------------------------------------------------------------------- #
@respx.mock
@pytest.mark.parametrize("how", ["text", "reaction"])
def test_close_clears_memory_and_next_message_opens_new_thread(bot_client, how):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    sm = SlackMock()
    twilio_post(bot_client, tw_msg("humano", sid="SM1"))
    first = parent_ts()
    assert store.in_handoff(USER) and store.transcript(USER)

    if how == "text":
        slack_event(bot_client, thread_reply("Close", first))
    else:
        slack_event(bot_client, reaction("lock", first))

    assert any(p["text"].startswith("🔒 Conversation closed by <@UOWNER>") for p in sm.replies())
    assert store.get_thread(USER) is None and store.user_for_thread(first) is None
    assert not store.in_handoff(USER)
    assert store.get_history(USER) == [] and store.transcript(USER)[:1] != [("user", "humano")]

    twilio_post(bot_client, tw_msg("hola de nuevo", sid="SM2"))
    assert len(sm.parents()) == 2 and parent_ts() != first
    assert tw_sent(send)[-1]["Body"] == "echo: hola de nuevo"   # bot answers again


@respx.mock
def test_command_in_unlinked_thread_gets_notice(bot_client):
    sm = SlackMock()
    slack_event(bot_client, thread_reply("close", "1699999999.000001"))
    assert len(sm.posts) == 1 and "no longer linked" in sm.posts[0]["text"]
    slack_event(bot_client, thread_reply("just a note", "1699999999.000001"))
    assert len(sm.posts) == 1  # non-commands stay silent


@respx.mock
def test_thread_links_survive_restart(make_client):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    c = make_client(**BOT_ENV)
    twilio_post(c, tw_msg("humano", sid="SM1"))
    pts = parent_ts()

    store.reset_memory()             # simulate a restart (SQLite file kept)
    assert store.load_all()["threads"] == 1
    assert store.user_for_thread(pts) == USER and store.get_mode(USER) == "human"
    assert store.handoff_target(USER) == "slack"
