"""One-tap CSAT (CSAT_ENABLED): 👍/👎 buttons inside the farewell, Meta only."""
import json

import httpx

from app import sessions

from .test_analytics_events import FLOWS, USER, evs, meta, tw  # noqa: F401 (fixtures)
from .test_channels import tw_msg, twilio_post
from .test_flows import meta_payload


def button_reply(mid, rid):
    msg = {"from": "5213312345678", "id": mid, "type": "interactive",
           "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": "👍"}}}
    return {"entry": [{"changes": [{"field": "messages", "value": {"messages": [msg]}}]}]}


def sent(route):
    """Outgoing messages (read receipts / typing indicators left out)."""
    return [m for m in (json.loads(c.request.content) for c in route.calls) if "type" in m]


def test_farewell_carries_the_buttons_in_one_message(make_client, meta):  # noqa: F811
    c = make_client(**FLOWS, CSAT_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("m1", "hola"))
    n = len(sent(meta))
    c.post("/webhooks/meta", json=meta_payload("m2", "Sería todo, gracias"))
    out = sent(meta)[n:]
    assert len(out) == 1 and out[0]["type"] == "interactive"
    inter = out[0]["interactive"]
    assert inter["type"] == "button" and inter["body"]["text"] == sessions.text("close_user", "es")
    assert [b["reply"]["id"] for b in inter["action"]["buttons"]] == ["csat:up", "csat:down"]
    assert evs("conversation_close")[0]["data"]["csat_offered"] is True


def test_button_reply_records_thanks_once_and_does_not_reopen(make_client, meta):  # noqa: F811
    c = make_client(**FLOWS, CSAT_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("m1", "hola"))
    c.post("/webhooks/meta", json=meta_payload("m2", "adiós"))
    closed_conv = evs("conversation_close")[0]["conversation_id"]
    n = len(sent(meta))
    c.post("/webhooks/meta", json=button_reply("m3", "csat:up"))
    c.post("/webhooks/meta", json=button_reply("m4", "csat:down"))     # second tap: ignored
    out = sent(meta)[n:]
    assert [m["text"]["body"] for m in out] == ["¡Gracias!"]
    vote = evs("csat")
    assert len(vote) == 1 and vote[0]["data"]["value"] == "up" and vote[0]["conversation_id"] == closed_conv
    assert not sessions.is_open(USER) and len(evs("conversation_start")) == 1


def test_stale_button_is_a_normal_message(make_client, meta, monkeypatch):  # noqa: F811
    c = make_client(**FLOWS, CSAT_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("m1", "hola"))
    c.post("/webhooks/meta", json=meta_payload("m2", "adiós"))
    t = sessions.now()
    monkeypatch.setattr(sessions, "now", lambda: t + 25 * 3600)
    c.post("/webhooks/meta", json=button_reply("m3", "csat:up"))
    assert evs("csat") == [] and sessions.is_open(USER)


def test_twilio_and_disabled_get_plain_farewell(make_client, tw, meta):  # noqa: F811
    c = make_client(**FLOWS, CSAT_ENABLED="true")
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("adiós", sid="S2"))
    assert dict(httpx.QueryParams(tw.calls[-1].request.content.decode()))["Body"] == \
        sessions.text("close_user", "es")
    assert evs("conversation_close")[0]["data"]["csat_offered"] is False


def test_csat_off_by_default(make_client, meta):  # noqa: F811
    c = make_client(**FLOWS)
    c.post("/webhooks/meta", json=meta_payload("m1", "hola"))
    c.post("/webhooks/meta", json=meta_payload("m2", "adiós"))
    assert sent(meta)[-1]["type"] == "text"
