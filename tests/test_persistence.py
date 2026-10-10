"""SQLite persistence: restart recovery, JSON import, close wipe, history trim, webhook dedupe."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import db, sessions, store
from app.flows import quiz

from .test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from .test_flows import FakeMessages, _resp, _text, _tool, meta_payload
from .test_slack_bridge import BOT_ENV, SlackMock, parent_ts

USER = "523312345678"
CLAUDE = {"FLOWS_ENABLED": "true", "AGENT_MODE": "claude", "ANTHROPIC_API_KEY": "sk-test",
          "GUARD_ENABLED": "false"}


def bodies(send):
    return [dict(httpx.QueryParams(c.request.content.decode()))["Body"] for c in send.calls]


def restart(make_client, monkeypatch, **env):
    """New process against the same DB file: fresh memory, new boot id, startup recovery."""
    from app import main
    monkeypatch.setattr(sessions, "BOOT_ID", f"boot{id(env)}")
    c = make_client(**env)
    ok = main.restore_state()
    return c, ok


def fake_agent(*responses):
    from app import agent
    fake = FakeMessages(list(responses))
    agent.get_agent()._client = SimpleNamespace(messages=fake)
    return fake


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
@respx.mock
def test_state_survives_restart_without_apology(make_client, monkeypatch):
    from app import main
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    env = {**CLAUDE, **BOT_ENV}
    c = make_client(**env)
    fake_agent(_resp("end_turn", _text("Es 10% algoritmos, 20% tecnología y 70% personas.")))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("diagnóstico", sid="S2"))
    twilio_post(c, tw_msg("2", sid="S3"))                       # answer question 1
    history_before = json.loads(json.dumps(store.get_history(USER)))
    pts = parent_ts()
    assert quiz._states[USER].index == 1

    c, ok = restart(make_client, monkeypatch, **env)
    assert ok
    assert store.get_history(USER) == history_before            # agent history
    assert quiz.active(USER) and quiz._states[USER].index == 1  # quiz progress
    assert quiz._states[USER].answers == [1]
    assert store.user_for_thread(pts) == USER                   # Slack thread map
    assert store.get_mode(USER) == "bot" and store.get_lang(USER) == "es"
    assert sessions.is_open(USER) and not sessions.needs_restart_notice(USER)

    monkeypatch.setattr(main, "RESTART_DELAY", 0)
    n = len(send.calls)
    asyncio.run(main.recover_after_restart())
    assert len(send.calls) == n                                  # no proactive apology

    twilio_post(c, tw_msg("3", sid="S4"))                        # next answer continues the quiz
    new = bodies(send)[n:]
    assert new and new[0].startswith("*3/9")
    assert not any("disculpa" in b.lower() for b in bodies(send))


@respx.mock
def test_human_mode_survives_restart(make_client, monkeypatch):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    SlackMock()
    c = make_client(**BOT_ENV)
    twilio_post(c, tw_msg("humano", sid="S1"))
    c, ok = restart(make_client, monkeypatch, **BOT_ENV)
    assert ok and store.handoff_target(USER) == "slack" and store.get_mode(USER) == "human"
    assert store.transcript(USER)[0] == ("user", "humano")


@respx.mock
def test_failed_recovery_keeps_restart_apology(make_client, monkeypatch):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(**CLAUDE)
    fake_agent(_resp("end_turn", _text("Respuesta")))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))

    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(sessions, "BOOT_ID", "bootX")
    c = make_client(**CLAUDE)
    sessions.load_all()                       # the session row is readable...
    monkeypatch.setattr(store, "load_all", boom)  # ...but the history is not
    from app import main
    assert main.restore_state() is False
    assert sessions.needs_restart_notice(USER)
    fake_agent(_resp("end_turn", _text("Otra")))
    twilio_post(c, tw_msg("¿y en ventas?", sid="S2"))
    assert "Una disculpa de parte de Alan" in bodies(send)[-2]


def test_json_import_runs_once_and_renames(make_client, monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    sess_file, threads_file = data / "sessions.json", tmp_path / ".slack_threads.json"
    sess_file.write_text(json.dumps({USER: {
        "state": "open", "channel_name": "twilio", "reply_to": "whatsapp:+5213312345678",
        "last_activity": sessions.now() - 60, "boot": "old", "context": True, "quiz": False}}))
    threads_file.write_text(json.dumps({
        "threads": {USER: {"thread_ts": "1700000000.000001", "channel_id": "C123",
                           "last_customer_at": store.now(), "mode": "human", "channel_name": "twilio",
                           "reply_to": "whatsapp:+5213312345678"}},
        "handoff": {USER: "slack"}}))
    env = {"SESSIONS_FILE": str(sess_file), "SLACK_STATE_FILE": str(threads_file)}
    c, ok = restart(make_client, monkeypatch, **env)
    assert ok and sessions.is_open(USER) and store.user_for_thread("1700000000.000001") == USER
    assert store.handoff_target(USER) == "slack"
    assert not sess_file.exists() and (data / "sessions.json.imported").exists()
    assert not threads_file.exists() and (tmp_path / ".slack_threads.json.imported").exists()
    # Imported from the old version: there was no history to recover -> the apology still applies.
    assert sessions.needs_restart_notice(USER)

    sess_file.write_text(json.dumps({"999": {"state": "open"}}))   # a stray file again
    restart(make_client, monkeypatch, **env)
    assert sessions.get("999") is None and sess_file.exists()      # table not empty: no re-import
    assert db.count("sessions") == 1 and db.count("slack_threads") == 1


# --------------------------------------------------------------------------- #
# Close + trimming
# --------------------------------------------------------------------------- #
def _rows(table):
    return db.query(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (USER,))[0][0]


@respx.mock
def test_close_wipes_history_and_quiz_state(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(**CLAUDE)
    fake_agent(_resp("end_turn", _text("Respuesta")))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    quiz.start(USER, "es")
    assert _rows("messages") == 2 and _rows("quiz_state") == 1
    r = c.post("/admin/close", json={"user_id": USER}, headers={"X-Admin-Token": "admin"})
    assert r.json() == {"closed": True} and bodies(send)[-1] == sessions.text("close_admin", "es")
    assert _rows("messages") == 0 and _rows("quiz_state") == 0
    assert not quiz.active(USER) and store.get_history(USER) == []


def test_wipe_is_one_transaction(make_client):
    make_client()
    store.append_message(USER, "user", "hola")
    db.execute("DROP TABLE quiz_state")            # second delete will fail
    with pytest.raises(Exception):
        db.wipe_conversation(USER)
    assert _rows("messages") == 1                  # first delete rolled back


def _tool_turn(i):
    return [{"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r"}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}]


def _pairs_ok(history):
    assert history[0]["role"] == "user" and isinstance(history[0]["content"], str)
    for i, m in enumerate(history):
        if m["role"] == "assistant" and isinstance(m["content"], list) and \
                any(b["type"] == "tool_use" for b in m["content"]):
            nxt = history[i + 1]["content"]
            assert nxt[0]["type"] == "tool_result" and nxt[0]["tool_use_id"] == m["content"][0]["id"]


@pytest.mark.parametrize("max_messages", [3, 5, 6, 7, 10])
def test_trim_never_splits_tool_pairs(max_messages):
    history = [m for i in range(5) for m in _tool_turn(i)]
    trimmed = store.trim_history(history, max_messages)
    _pairs_ok(trimmed)
    assert trimmed[-1] == history[-1]
    assert len(trimmed) <= max(max_messages, 4)


def test_history_capped_and_persisted(make_client):
    make_client(HISTORY_MAX_MESSAGES="6")
    for i in range(4):
        for m in _tool_turn(i):
            store.append_message(USER, m["role"], m["content"])
    h = store.get_history(USER)
    assert len(h) <= 6 and h[-1]["content"][0]["text"] == "a3"
    _pairs_ok(h)
    stored = [json.loads(r["content_json"]) for r in
              db.query("SELECT content_json FROM messages WHERE user_id = ? ORDER BY id", (USER,))]
    assert stored == [m["content"] for m in h]


def test_history_never_stores_file_bytes(make_client):
    make_client()
    store.append_message(USER, "user", [{"type": "image", "source": {"data": "AAAA"}},
                                        {"type": "text", "text": "mira"}])
    raw = db.query("SELECT content_json FROM messages")[0][0]
    assert "AAAA" not in raw and "[file omitted]" in raw


def test_sdk_blocks_are_stored_as_plain_dicts(make_client):
    from anthropic.types import TextBlock, ToolUseBlock
    make_client()
    store.append_message(USER, "assistant", [TextBlock(type="text", text="hola"),
                                             ToolUseBlock(type="tool_use", id="t1", name="x", input={"a": 1})])
    assert store.get_history(USER)[0]["content"] == [
        {"type": "text", "text": "hola"}, {"type": "tool_use", "id": "t1", "name": "x", "input": {"a": 1}}]


def test_retention_purges_old_conversations_and_ids(make_client, monkeypatch):
    from app import main
    make_client()
    old = sessions.now() - 40 * 86400
    sessions._sessions["111"] = {"state": "closed", "last_activity": old, "closed_at": old}
    sessions.save("111")
    store.append_message("111", "user", "viejo")
    sessions._sessions[USER] = {"state": "open", "last_activity": sessions.now()}
    sessions.save(USER)
    store.append_message(USER, "user", "nuevo")
    db.execute("INSERT INTO processed_messages VALUES ('meta:old', ?)", (db.now() - 8 * 86400,))
    db.execute("INSERT INTO processed_messages VALUES ('meta:new', ?)", (db.now(),))
    assert main.run_retention() == 1
    assert sessions.get("111") is None and store.get_history("111") == []
    assert db.count("messages") == 1 and db.count("sessions") == 1
    assert [r[0] for r in db.query("SELECT message_id FROM processed_messages")] == ["meta:new"]


def test_db_failure_falls_back_to_memory(make_client, monkeypatch):
    make_client()
    monkeypatch.setattr(db, "conn", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store.append_message(USER, "user", "hola")          # no exception
    assert store.get_history(USER)[0]["content"] == "hola"
    assert not store.seen_before("m1", "meta") and store.seen_before("m1", "meta")  # in-memory dedupe


# --------------------------------------------------------------------------- #
# Webhook idempotency
# --------------------------------------------------------------------------- #
@respx.mock
def test_duplicate_meta_message_processed_once_even_after_restart(make_client, monkeypatch):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    c = make_client()
    for _ in range(3):
        assert c.post("/webhooks/meta", json=meta_payload("wamid.1", "hola")).status_code == 200
    texts = [json.loads(x.request.content) for x in meta.calls]
    assert sum(t.get("type") == "text" for t in texts) == 1
    c, _ = restart(make_client, monkeypatch)
    c.post("/webhooks/meta", json=meta_payload("wamid.1", "hola"))   # Meta retry after a deploy
    assert sum(json.loads(x.request.content).get("type") == "text" for x in meta.calls) == 1
    assert db.query("SELECT message_id FROM processed_messages")[0][0] == "meta:wamid.1"


@respx.mock
def test_meta_status_callbacks_never_reach_handle_inbound(make_client, monkeypatch):
    from app import main
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    calls = []

    async def spy(channel, msg):
        calls.append(msg)
    monkeypatch.setattr(main, "handle_inbound", spy)
    c = make_client()
    for status in ("sent", "delivered", "read", "failed"):
        payload = {"entry": [{"changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.9", "status": status, "recipient_id": "5213312345678"}]}}]}]}
        assert c.post("/webhooks/meta", json=payload).status_code == 200
    assert calls == []
    assert db.count("processed_messages") == 0


@respx.mock
def test_twilio_duplicate_message_sid_uses_db(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client()
    twilio_post(c, tw_msg(sid="SMdup"))
    store._seen_ids.clear()
    twilio_post(c, tw_msg(sid="SMdup"))
    assert send.call_count == 1
    assert db.query("SELECT message_id FROM processed_messages")[0][0] == "twilio:SMdup"


@respx.mock
def test_agent_end_conversation_tool_closes_chat(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(**CLAUDE)
    fake = fake_agent(_resp("end_turn", _text("Hola")),
                      _resp("tool_use", _tool("end_conversation", {})), _resp("end_turn", _text("")))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("ya quedó claro todo, muchas gracias por la ayuda", sid="S2"))
    assert bodies(send)[-1] == sessions.text("close_user", "es")
    assert not sessions.is_open(USER) and store.get_history(USER) == []
    assert any(t["name"] == "end_conversation" for t in fake.calls[0]["tools"])


@respx.mock
@pytest.mark.parametrize("text,closes", [("Sería todo, gracias", True), ("That's all, thanks!", True),
                                         ("gracias", False), ("¿es todo?", False)])
def test_close_on_intent_phrases(make_client, text, closes):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client()
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg(text, sid="S2"))
    assert (not sessions.is_open(USER)) is closes
    if not closes:
        assert bodies(send)[-1] == f"echo: {text}"


@respx.mock
def test_close_on_intent_off_keeps_legacy(make_client):
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client(CLOSE_ON_INTENT="false")
    twilio_post(c, tw_msg("Sería todo, gracias", sid="S1"))
    assert sessions.is_open(USER) and bodies(send)[-1] == "echo: Sería todo, gracias"


@respx.mock
def test_real_startup_lifespan_restores_state(make_client, monkeypatch):
    """Same as a container restart: the FastAPI lifespan runs restore_state."""
    from fastapi.testclient import TestClient

    from app import main
    send = respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    monkeypatch.setattr(main, "RESTART_DELAY", 0)
    c = make_client(FLOWS_ENABLED="true")
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("1", sid="S2"))

    monkeypatch.setattr(sessions, "BOOT_ID", "lifespan-boot")
    make_client(FLOWS_ENABLED="true")               # wipe memory, keep the DB file
    with TestClient(main.app) as live:              # startup: restore + background tasks
        assert quiz._states[USER].index == 1 and not sessions.needs_restart_notice(USER)
        twilio_post(live, tw_msg("1", sid="S3"))
    assert bodies(send)[-1].startswith("*3/9")
    assert not any("disculpa" in b.lower() for b in bodies(send))


def test_corrupt_legacy_json_does_not_block_restore(make_client, monkeypatch, tmp_path):
    make_client()
    sessions._sessions[USER] = {"state": "open", "last_activity": sessions.now()}
    sessions.save(USER)
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    c, ok = restart(make_client, monkeypatch, SLACK_STATE_FILE=str(bad))
    assert ok and sessions.is_open(USER) and bad.exists()
