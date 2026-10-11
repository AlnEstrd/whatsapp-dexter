"""Per-call cost tracking: llm_calls rows (cache tokens included), pricing, the price
override, prompt caching through the real SDK, and the optional Admin API reconciliation."""
import json
import logging
import os
from datetime import date

import httpx
import pytest
import respx

from app import db
from app.analytics import anthropic_usage, events, metrics, pricing

from .test_analytics_events import CLAUDE, USER, install, tw  # noqa: F401 (fixture)
from .test_channels import SLACK, TWILIO_SEND, tw_msg, twilio_post

ANTHROPIC = "https://api.anthropic.com/v1/messages"
ADMIN = "https://api.anthropic.com/v1/organizations"


def calls():
    events.drain()
    return [dict(r) for r in db.query("SELECT * FROM llm_calls ORDER BY id")]


def test_llm_calls_for_agent_and_guard(make_client, tw):  # noqa: F811
    c = make_client(**CLAUDE)
    install([{"tool": "search_sources", "input": {"query": "DORA"}}, {"text": "Según DORA..."}],
            guard='{"on_topic": true, "topic": "procesos"}')
    twilio_post(c, tw_msg("¿Qué dice el reporte DORA sobre la productividad con IA?", sid="S1"))
    rows = calls()
    assert [r["purpose"] for r in rows] == ["guard", "agent", "agent"]
    agent = rows[1]
    assert agent["model"] == "claude-sonnet-4-5" and agent["user_id"] == USER
    assert (agent["input_tokens"], agent["output_tokens"], agent["cache_read_tokens"],
            agent["cache_write_tokens"]) == (100, 20, 9000, 0)
    assert agent["stop_reason"] == "tool_use" and agent["latency_ms"] >= 0 and agent["error"] is None
    assert rows[0]["model"] == "claude-haiku-4-5"
    assert len({r["conversation_id"] for r in rows}) == 1
    # the hardening usage table (limits) is still fed
    u = dict(db.query("SELECT * FROM usage WHERE user_id = ?", (USER,))[0])
    assert u["input_tokens"] == 300 and u["cache_read_tokens"] == 27000


def test_failed_guard_call_is_logged_and_fails_open(make_client, tw):  # noqa: F811
    from types import SimpleNamespace
    from app import guard

    class Boom:
        async def create(self, **kw):
            raise RuntimeError("404 model not found")
    c = make_client(**CLAUDE)
    install([{"text": "Respuesta del agente."}])
    guard._guard._client = SimpleNamespace(messages=Boom())
    twilio_post(c, tw_msg("¿Qué dice el EU AI Act sobre supervisión humana?", sid="S1"))
    rows = calls()
    assert rows[0]["purpose"] == "guard" and rows[0]["error"] == "RuntimeError"
    assert rows[1]["purpose"] == "agent"                       # fail-open: the agent still answered
    from .test_analytics_events import evs
    assert evs("error")[0]["data"] == {"kind": "RuntimeError", "where": "guard"}


# --------------------------------------------------------------------------- #
# Prompt caching through the real SDK (Part 0): cache_control on a stable prefix and the
# cache fields of the usage object reach llm_calls.
# --------------------------------------------------------------------------- #
def _api_response(content, stop, usage):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5-20250929",
            "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage}


@respx.mock
def test_real_sdk_request_is_cache_friendly_and_usage_is_recorded(make_client, caplog):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    seen = []

    def answer(request):
        body = json.loads(request.content)
        seen.append(body)
        if "tools" not in body:   # guard
            return httpx.Response(200, json=_api_response(
                [{"type": "text", "text": '{"on_topic": true, "topic": "liderazgo"}'}], "end_turn",
                {"input_tokens": 700, "output_tokens": 14}))
        return httpx.Response(200, json=_api_response(
            [{"type": "text", "text": "El sponsor es quien responde."}], "end_turn",
            {"input_tokens": 45, "output_tokens": 60, "cache_read_input_tokens": 17890,
             "cache_creation_input_tokens": 0}))
    respx.post(ANTHROPIC).mock(side_effect=answer)
    c = make_client(**CLAUDE)
    with caplog.at_level(logging.INFO, logger="limits"):
        twilio_post(c, tw_msg("¿Qué papel juega el sponsor en la adopción de IA?", sid="S1"))
    agent_req = next(b for b in seen if "tools" in b)
    system = agent_req["system"]
    # one breakpoint, on the last (largest) system block; nothing per-request before it
    marks = [i for i, b in enumerate(system) if b.get("cache_control")]
    assert marks == [len(system) - 1]
    prefix = json.dumps(agent_req["tools"]) + "".join(b["text"] for b in system)
    assert "3312345678" not in prefix and "¿Qué papel juega" not in prefix
    assert all("cache_control" not in json.dumps(m) for m in agent_req["messages"])
    row = [r for r in calls() if r["purpose"] == "agent"][0]
    assert row["cache_read_tokens"] == 17890 and row["input_tokens"] == 45
    assert row["model"] == "claude-sonnet-4-5"                # dated id from the API -> normalized
    assert any("cache_r=17890" in r.message for r in caplog.records)   # logged by limits.record
    # a second identical-prefix turn sends byte-identical tools + system
    twilio_post(c, tw_msg("¿Y el champion qué hace en ese caso?", sid="S2"))
    second = [b for b in seen if "tools" in b][-1]
    assert second["tools"] == agent_req["tools"] and second["system"] == agent_req["system"]


@pytest.mark.eval_live
def test_live_cache_hit_on_second_call():
    """Real API (RUN_LIVE_EVALS=1 + ANTHROPIC_API_KEY): prints cache stats of two agent calls."""
    import asyncio
    if not (os.getenv("RUN_LIVE_EVALS") and os.getenv("ANTHROPIC_API_KEY")):
        pytest.skip("live only")
    from anthropic import AsyncAnthropic
    from app import agent
    client = AsyncAnthropic()

    async def once():
        r = await client.messages.create(model="claude-sonnet-4-5", max_tokens=50, system=agent.system_blocks(),
                                         tools=agent.tool_definitions(),
                                         messages=[{"role": "user", "content": "¿Qué es la regla 10-20-70?"}])
        return r.usage
    u1, u2 = asyncio.run(once()), asyncio.run(once())
    print("first:", u1, "\nsecond:", u2)
    assert (getattr(u2, "cache_read_input_tokens", 0) or 0) > 1024


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def test_pricing_defaults_and_cost(make_client):
    make_client()
    # 1M input + 1M output + 1M cache write + 1M cache read on Sonnet 4.5
    assert pricing.cost("claude-sonnet-4-5", 10**6, 10**6, 10**6, 10**6) == pytest.approx(3 + 15 + 0.30 + 3.75)
    assert pricing.cost("claude-haiku-4-5-20251001", input_tokens=2_000_000) == pytest.approx(2.0)
    assert pricing.cost("claude-unknown", 10**6) == 0.0
    assert pricing.normalize("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"


def test_model_prices_json_override_and_sql_view(make_client):
    make_client(MODEL_PRICES_JSON='{"claude-sonnet-4-5": {"input": 6}, "claude-new": {"input": 1, "output": 2, '
                                  '"cache_write": 0, "cache_read": 0}}')
    assert pricing.price_for("claude-sonnet-4-5") == {"input": 6.0, "output": 15.0, "cache_write": 3.75,
                                                      "cache_read": 0.30}
    assert pricing.cost("claude-new", 10**6, 10**6) == pytest.approx(3.0)
    pricing.sync_table()
    events.llm_call(USER, "agent", "claude-sonnet-4-5",
                    type("R", (), {"usage": {"input_tokens": 10**6, "output_tokens": 0}, "stop_reason": "end_turn"})())
    events.drain()
    assert db.query("SELECT cost_usd FROM v_llm_calls")[0][0] == pytest.approx(6.0)


def test_bad_price_json_falls_back_to_defaults(make_client):
    make_client(MODEL_PRICES_JSON="not json")
    assert pricing.price_for("claude-sonnet-4-5")["input"] == 3.0


# --------------------------------------------------------------------------- #
# Anthropic Admin API reconciliation (mocked)
# --------------------------------------------------------------------------- #
def _usage_page(model, uncached, out, read, write5m):
    return {"data": [{"starting_at": "2026-10-10T00:00:00Z", "ending_at": "2026-10-11T00:00:00Z", "results": [
        {"model": model, "uncached_input_tokens": uncached, "output_tokens": out, "cache_read_input_tokens": read,
         "cache_creation": {"ephemeral_5m_input_tokens": write5m, "ephemeral_1h_input_tokens": 0}}]}],
        "has_more": False, "next_page": None}


@respx.mock
def test_reconciliation_percentage(make_client, monkeypatch):
    make_client(ANTHROPIC_ADMIN_KEY="sk-ant-admin01-test")
    usage = respx.get(f"{ADMIN}/usage_report/messages").mock(return_value=httpx.Response(
        200, json=_usage_page("claude-sonnet-4-5-20250929", 1_000_000, 0, 0, 0)))
    respx.get(f"{ADMIN}/cost_report").mock(return_value=httpx.Response(200, json={
        "data": [{"results": [{"amount": "400", "currency": "USD", "model": "claude-sonnet-4-5"}]}],
        "has_more": False}))
    # our log for the same UTC day: 1M input tokens on Sonnet = $3.00
    db.execute("INSERT INTO llm_calls (ts, user_id, purpose, model, input_tokens) VALUES (?, ?, ?, ?, ?)",
               ("2026-10-10T15:00:00.000000Z", USER, "agent", "claude-sonnet-4-5", 1_000_000))
    monkeypatch.setattr(metrics, "now", lambda: 1_760_200_000)   # 2026-10-11 local
    out = metrics.spend_check("2026-10-10")
    assert out["available"] and out["ours_usd"] == pytest.approx(3.0) and out["anthropic_usd"] == 4.0
    assert out["difference_pct"] == -25.0 and out["flag"] is True
    req = usage.calls[0].request
    assert req.headers["x-api-key"] == "sk-ant-admin01-test" and req.headers["anthropic-version"] == "2023-06-01"
    q = dict(req.url.params)
    assert q["starting_at"] == "2026-10-10T00:00:00Z" and q["ending_at"] == "2026-10-11T00:00:00Z"
    assert q["bucket_width"] == "1d" and q["group_by[]"] == "model"
    rep = anthropic_usage.report(date(2026, 10, 10), date(2026, 10, 10))
    assert rep["cost_from_tokens_usd"] == pytest.approx(3.0)
    assert len(usage.calls) == 1                                    # cached for 15 minutes


def test_admin_usage_absent_when_key_unset(make_client):
    make_client()
    assert not anthropic_usage.enabled()
    assert metrics.spend_check("7d")["available"] is False
