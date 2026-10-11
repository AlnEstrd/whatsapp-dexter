"""Daily Slack digest (app/analytics/digest.py)."""
import asyncio
import json

import httpx
import pytest
import respx

from app import db
from app.analytics import digest, metrics

from .test_metrics import NOW, D, call, ev, fixture_db  # noqa: F401 (fixture)

SLACK_API = "https://slack.com/api/chat.postMessage"
SLACK_ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "CTHREADS", "SLACK_ALERT_USER_ID": "UOWNER",
             "SLACK_WEBHOOK_URL": "", "LIMITS_ENABLED": "false"}


@pytest.fixture
def slack():
    with respx.mock(assert_all_called=False) as router:
        yield router.post(SLACK_API).mock(return_value=httpx.Response(200, json={"ok": True, "ts": "1.1"}))


def posted(route):
    return [json.loads(c.request.content) for c in route.calls]


@pytest.fixture
def digest_db(make_client, monkeypatch):
    def _make(**env):
        make_client(**{**SLACK_ENV, **env})
        monkeypatch.setattr(metrics, "now", lambda: NOW)
    return _make


def _seed():
    ev(f"{D} 09:00", "conversation_start", "c1", "5213300000001", "start", source="qr_talk", lang="es")
    ev(f"{D} 09:00", "user_msg", "c1", "5213300000001", topic="liderazgo", route="agent", lang="es")
    ev(f"{D} 09:01", "agent_turn", "c1", "5213300000001", "agent_turn", outcome="answered", tools=[])
    ev(f"{D} 09:01", "gap", "c1", "5213300000001", topic="medicion_costo", source="tool")
    ev(f"{D} 09:20", "conversation_close", "c1", "5213300000001", reason="idle", last_step="agent_turn",
       last_bot_msg_read=True)
    call(f"{D} 09:01", "agent", "claude-sonnet-4-5", "c1", inp=1000, out=100, read=9000)


def test_digest_content(digest_db, slack):
    digest_db()
    _seed()
    assert asyncio.run(digest.send("yesterday"))
    msg = posted(slack)[0]
    assert msg["channel"] == "CTHREADS" and "thread_ts" not in msg       # top-level, fallback channel
    t = msg["text"]
    assert f"Dexter — {D}" in t and "CDMX" in t
    assert "Conversaciones: *1* (qr_talk 1)" in t
    assert "Contención: 100%" in t and "Liderazgo 1" in t
    assert "Mayor abandono: `agent_turn`" in t and "Huecos de la KB: Medición y costo" in t
    assert "Costo: $0.01" in t and "cache hit 90%" in t
    assert "<@" not in t and "Anomalías: ninguna" in t


def test_metrics_channel_wins(digest_db, slack):
    digest_db(SLACK_METRICS_CHANNEL_ID="CMETRICS")
    asyncio.run(digest.send("yesterday"))
    assert posted(slack)[0]["channel"] == "CMETRICS"


def test_zero_conversations_one_liner(digest_db, slack):
    digest_db()
    asyncio.run(digest.send("yesterday"))
    assert posted(slack)[0]["text"] == f"📊 *Dexter — {D}* (CDMX): sin conversaciones."


def test_mention_only_with_anomalies(digest_db, slack):
    digest_db()
    _seed()
    for i in range(20):
        ev(f"{D} 12:{i:02d}", "user_msg", f"x{i}", topic="otro", route="agent", lang="es")
        ev(f"{D} 12:{i:02d}", "error", f"x{i}", kind="SendError")
    asyncio.run(digest.send("yesterday"))
    t = posted(slack)[0]["text"]
    assert t.count("<@UOWNER>") == 1 and "Tasa de error" in t


def test_posted_once_per_day_and_not_after_restart(digest_db, slack, monkeypatch):
    digest_db()
    _seed()
    monkeypatch.setattr(metrics, "now", lambda: NOW)            # 10:00 CDMX, after the 8:00 digest hour
    assert asyncio.run(digest.maybe_send())
    assert not asyncio.run(digest.maybe_send())
    digest.reset()                                              # simulated restart (flag lives in SQLite)
    assert not asyncio.run(digest.maybe_send())
    assert len(slack.calls) == 1 and db.has_flag(f"digest:{D}")


def test_not_before_the_digest_hour(digest_db, slack, monkeypatch):
    digest_db(METRICS_DIGEST_HOUR="11")
    assert not asyncio.run(digest.maybe_send())
    assert slack.calls.call_count == 0


def test_slack_failure_never_raises_and_retries_later(digest_db, monkeypatch):
    digest_db()
    with respx.mock() as r:
        r.post(SLACK_API).mock(return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
        assert not asyncio.run(digest.maybe_send())
    assert not db.has_flag(f"digest:{D}")


def test_disabled_without_bot_token(make_client, slack):
    make_client(SLACK_BOT_TOKEN="", SLACK_CHANNEL_ID="")
    assert not digest.enabled() and not asyncio.run(digest.maybe_send())


def test_admin_digest_endpoint(digest_db, slack):
    from fastapi.testclient import TestClient
    from app.main import app
    digest_db()
    c = TestClient(app)
    r = c.post("/admin/digest?range=yesterday", headers={"X-Admin-Token": "admin"})
    assert r.json() == {"posted": True} and len(slack.calls) == 1
    assert not db.has_flag(f"digest:{D}")                     # on demand doesn't mark the day
    assert c.post("/admin/digest?range=bad", headers={"X-Admin-Token": "admin"}).status_code == 400
