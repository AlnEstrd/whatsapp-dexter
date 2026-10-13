"""KPI definitions (app/analytics/metrics.py) on a fixture DB with known answers,
Mexico City day boundaries and the anomaly rules."""
import json
from datetime import datetime, timedelta

import pytest

from app import db, limits
from app.analytics import events, metrics

TZ = metrics.TZ
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=TZ).timestamp()   # "today" = 2026-10-12 (CDMX)
D = "2026-10-11"                                              # "yesterday"


def ts(local: str) -> str:
    """'2026-10-11 23:30' (Mexico City) -> UTC ISO as stored."""
    dt = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return events.iso(dt.timestamp())


def ev(local, type_, conv, user="5213300000001", step=None, **data):
    db.execute("INSERT INTO events (ts, user_id, conversation_id, type, step, data_json) VALUES (?, ?, ?, ?, ?, ?)",
               (ts(local), user, conv, type_, step, json.dumps(data)))


def call(local, purpose, model, conv=None, inp=0, out=0, read=0, write=0, error=None):
    db.execute("INSERT INTO llm_calls (ts, user_id, conversation_id, purpose, model, input_tokens, output_tokens, "
               "cache_read_tokens, cache_write_tokens, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (ts(local), "5213300000002", conv, purpose, model, inp, out, read, write, error))


def sample(sid, local, text, kind="question", topic="medicion_costo"):
    db.execute("INSERT INTO question_samples (id, ts, conversation_id, topic, lang, kind, text) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, ts(local), "c2", topic, "en", kind, text))


@pytest.fixture
def fixture_db(make_client, monkeypatch):
    make_client(CSAT_ENABLED="true", LIMITS_ENABLED="false")
    monkeypatch.setattr(metrics, "now", lambda: NOW)
    A, B, C = "5213300000001", "5213300000002", "5213300000003"
    # c1: QR, menu -> quiz, abandoned at Q4 (idle, read)
    ev(f"{D} 09:00", "conversation_start", "c1", A, "start", source="qr_talk", lang="es", channel="meta")
    ev(f"{D} 09:00", "user_msg", "c1", A, topic="saludo_menu", route="menu", lang="es")
    ev(f"{D} 09:01", "menu_choice", "c1", A, option="menu:quiz")
    ev(f"{D} 09:01", "quiz_start", "c1", A, "quiz_q1", known=0)
    for q in (1, 2, 3):
        ev(f"{D} 09:02", "quiz_answer", "c1", A, f"quiz_q{q + 1}", q=q)
    ev(f"{D} 09:20", "conversation_close", "c1", A, reason="idle", last_step="quiz_q4", turns=5,
       duration_s=1200, last_bot_msg_read=True)
    # c2: organic EN, two answered agent turns, a gap noted by the agent, closes by intent, 👍
    ev(f"{D} 10:00", "conversation_start", "c2", B, "start", source="organic", lang="en", channel="meta")
    for i, (lat, tok) in enumerate([(1000, 10000), (3000, 12000)]):
        ev(f"{D} 10:0{i + 1}", "user_msg", "c2", B, topic="liderazgo", route="agent", lang="en")
        ev(f"{D} 10:0{i + 1}", "agent_turn", "c2", B, "agent_turn", outcome="answered",
           tools=["search_sources"] if i == 0 else [], steps=1, latency_ms=lat, input_tokens=tok, output_tokens=80)
    sample("s2", f"{D} 10:01", "How much does Copilot cost per seat in MXN?")
    sample("n2", f"{D} 10:01", "Copilot price per seat", kind="gap_note")
    ev(f"{D} 10:02", "gap", "c2", B, topic="medicion_costo", source="tool", sample_id="s2", note_id="n2")
    ev(f"{D} 10:05", "conversation_close", "c2", B, reason="user_intent", last_step="agent_turn", turns=3,
       duration_s=300, last_bot_msg_read=True, csat_offered=True)
    ev(f"{D} 10:06", "csat", "c2", B, value="up")
    call(f"{D} 10:01", "guard", "claude-haiku-4-5", "c2", inp=500, out=10)
    call(f"{D} 10:01", "agent", "claude-sonnet-4-5", "c2", inp=1000, out=100, read=9000)
    # c3: agent error -> handoff (agent_error) -> gap -> admin close
    ev(f"{D} 11:00", "conversation_start", "c3", C, "start", source="qr_talk", lang="es", channel="meta")
    ev(f"{D} 11:00", "user_msg", "c3", C, topic="gobernanza_riesgo", route="agent", lang="es")
    ev(f"{D} 11:00", "agent_turn", "c3", C, "agent_turn", outcome="error", tools=[], steps=0,
       latency_ms=500, input_tokens=0, output_tokens=0)
    ev(f"{D} 11:00", "error", "c3", C, kind="RuntimeError", where="agent")
    ev(f"{D} 11:00", "handoff", "c3", C, "handoff", reason="agent_error")
    ev(f"{D} 11:00", "gap", "c3", C, topic="gobernanza_riesgo", source="handoff", sample_id=None)
    ev(f"{D} 11:30", "conversation_close", "c3", C, reason="admin", last_step="handoff", turns=1,
       duration_s=1800, last_bot_msg_read=None)
    call(f"{D} 11:00", "agent", "claude-sonnet-4-5", "c3", error="RuntimeError")
    ev(f"{D} 11:01", "msg_status", "c3", C, status="failed", error_code=131047)
    # c4: quiz done + report + slide, then asks for Alan, human takes, closes with a keyword
    ev(f"{D} 12:00", "conversation_start", "c4", A, "start", source="organic", lang="es", channel="twilio")
    ev(f"{D} 12:00", "user_msg", "c4", A, topic="diagnostico", route="quiz", lang="es")
    ev(f"{D} 12:00", "quiz_start", "c4", A, "quiz_q1", known=0)
    ev(f"{D} 12:03", "quiz_complete", "c4", A, "quiz_result", level=2)
    ev(f"{D} 12:03", "report_sent", "c4", A, "report", pages=4)
    ev(f"{D} 12:04", "user_msg", "c4", A, topic="laminas", route="file_reply", lang="es")
    ev(f"{D} 12:04", "slides_sent", "c4", A, "slides", slide="8", pages=1, format="image")
    ev(f"{D} 12:05", "handoff", "c4", A, "handoff", reason="user_asked")
    ev(f"{D} 12:06", "human_take", "c4", A, "human")
    ev(f"{D} 12:30", "conversation_close", "c4", A, reason="user_keyword", last_step="human", turns=4,
       duration_s=1800, last_bot_msg_read=None)
    # c5: 23:30 local on D (05:30 UTC on D+1) -> belongs to D
    ev(f"{D} 23:30", "conversation_start", "c5", B, "start", source="organic", lang="es", channel="meta")
    ev(f"{D} 23:30", "user_msg", "c5", B, topic="saludo_menu", route="menu", lang="es")
    # c6: 00:30 local on D+1 -> today, not yesterday
    ev("2026-10-12 00:30", "conversation_start", "c6", C, "start", source="organic", lang="es", channel="meta")
    events.drain()


def test_ranges_follow_mexico_city_days(fixture_db):
    r = metrics.parse_range("yesterday")
    assert (r.first_day.isoformat(), r.last_day.isoformat()) == (D, D)
    assert r.start == "2026-10-11T06:00:00.000000Z" and r.end == "2026-10-12T06:00:00.000000Z"
    assert metrics.conversations("yesterday")["conversations"] == 5          # c5 at 23:30 counts on D
    assert metrics.conversations("today")["conversations"] == 1              # c6
    assert metrics.conversations("7d")["conversations"] == 6
    assert metrics.conversations(D)["conversations"] == 5
    assert metrics.parse_range(None, "2026-10-10", "2026-10-11").label == "2026-10-10..2026-10-11"
    out = metrics.conversations("yesterday")
    assert out["timezone"] == "America/Mexico_City" and out["from"] == D and "Mexico_City" in out["note"]
    with pytest.raises(ValueError):
        metrics.parse_range("last week")


def test_conversations_and_sources(fixture_db):
    out = metrics.conversations("yesterday")
    assert out["by_source"] == {"qr_talk": 2, "organic": 3}


def test_containment(fixture_db):
    out = metrics.containment("yesterday")
    assert (out["closed"], out["contained"], out["containment_rate"]) == (4, 2, 0.5)


def test_escalation(fixture_db):
    out = metrics.escalation("yesterday")
    assert out["escalation_rate"] == 0.4
    assert out["handoffs_by_reason"] == {"agent_error": 1, "user_asked": 1}


def test_completion(fixture_db):
    out = metrics.completion("yesterday")
    assert out["quiz"] == {"started": 2, "completed": 1, "rate": 0.5}
    assert out["report_delivery"] == {"sent": 1, "rate": 1.0}
    assert out["answered_turns"] == {"answered": 2, "turns": 3, "rate": 0.6667}


def test_gap_rate_and_board(fixture_db):
    assert metrics.gap_rate("yesterday")["gap_rate"] == 0.6667
    board = metrics.gaps_board("yesterday")
    rows = {r["topic"]: r for r in board["topics"]}
    assert rows["medicion_costo"]["by_source"] == {"tool": 1}
    assert rows["gobernanza_riesgo"]["by_source"] == {"handoff": 1}
    texts = [s["text"] for s in rows["medicion_costo"]["samples"]]
    assert set(texts) == {"How much does Copilot cost per seat in MXN?", "Copilot price per seat"}
    assert "untrusted" in board["warning"]
    assert "samples" not in metrics.gaps_board("yesterday", with_samples=False)["topics"][0]


def test_funnel(fixture_db):
    steps = {s["step"]: s for s in metrics.funnel("yesterday")["funnel"]}
    assert [steps[k]["conversations"] for k in ("conversation_start", "engaged", "feature_started",
                                                "feature_completed", "closed_by_user")] == [5, 4, 4, 2, 2]
    assert steps["engaged"]["dropped"] == 1 and steps["feature_completed"]["from_previous"] == 0.5


def test_close_reasons_and_drop_off(fixture_db):
    out = metrics.close_reasons("yesterday")
    assert {r["reason"]: (r["count"], r["share"]) for r in out["reasons"]} == {
        "idle": (1, 0.25), "user_intent": (1, 0.25), "admin": (1, 0.25), "user_keyword": (1, 0.25)}
    assert out["idle_drop_off"] == [{"last_step": "quiz_q4", "closes": 1, "read": 1, "unread": 0, "unknown": 0}]


def test_topics(fixture_db):
    out = metrics.topics_board("yesterday")
    rows = {r["topic"]: r for r in out["topics"]}
    assert out["messages"] == 7
    assert (rows["liderazgo"]["messages"], rows["liderazgo"]["conversations"], rows["liderazgo"]["en"]) == (2, 1, 2)
    assert (rows["saludo_menu"]["messages"], rows["saludo_menu"]["conversations"]) == (2, 2)
    assert rows["liderazgo"]["share"] == round(2 / 7, 4) and rows["liderazgo"]["label"] == "Liderazgo"
    assert [r["topic"] for r in metrics.topics_board("yesterday", lang="en")["topics"]] == ["liderazgo"]
    assert len(metrics.topics_board("yesterday", top_n=2)["topics"]) == 2


def test_tools_and_slides(fixture_db):
    out = metrics.tools_and_slides("yesterday")
    assert out["tools"] == {"search_sources": 1} and out["slides"] == {"8": 1} and out["reports"] == 1


def test_costs(fixture_db):
    out = metrics.costs("yesterday")
    agent, guard = 0.0072, 0.00055
    assert out["total_usd"] == pytest.approx(agent + guard)
    assert out["by_model"]["claude-sonnet-4-5"]["cost_usd"] == pytest.approx(agent)
    assert out["by_purpose"]["guard"]["cost_usd"] == pytest.approx(guard)
    pc = out["per_conversation"]
    assert pc["conversations"] == 2 and pc["mean_usd"] == pytest.approx((agent + guard) / 2)
    assert pc["p50_usd"] == 0 and pc["p90_usd"] == pytest.approx(agent + guard)
    assert out["per_agent_turn_usd"] == pytest.approx(agent / 3, abs=1e-6)
    assert out["per_completed_quiz_usd"] == pytest.approx(agent + guard)
    assert out["cache_hit_ratio"] == round(9000 / 10500, 4)
    assert list(metrics.costs("yesterday", "hour")["breakdown"]) == [f"{D} 10:00", f"{D} 11:00"]
    conv_rows = metrics.costs("yesterday", "conversation")["breakdown"]
    assert conv_rows[0]["user"] == "****0002" and "5213300000002" not in json.dumps(conv_rows)


def test_tokens_latency_errors_leads_csat(fixture_db):
    t = metrics.agent_turns("yesterday")
    assert t["input_tokens"] == {"mean": 7333, "p90": 12000}
    assert t["latency_ms"] == {"p50": 1000, "p95": 3000}
    e = metrics.errors("yesterday")
    assert e["by_kind"] == {"RuntimeError": 1} and e["failed_by_code"] == {"131047": 1}
    assert e["error_rate"] == round(1 / 7, 4)
    assert metrics.leads("yesterday")["quiz_completed"] == 1
    assert metrics.leads("yesterday")["asked_for_alan"] == 1
    c = metrics.csat("yesterday")
    assert (c["up"], c["down"], c["csat"], c["offered"], c["response_rate"]) == (1, 0, 1.0, 1, 1.0)


def test_overview_and_timeline_are_masked(fixture_db):
    out = metrics.overview("yesterday")
    blob = json.dumps(out)
    assert out["conversations"] == 5 and out["timezone"] == "America/Mexico_City"
    assert "52133000000" not in blob
    tl = metrics.conversation_timeline("c2")
    assert tl["user"] == "****0002" and [e["type"] for e in tl["events"]][:2] == ["conversation_start", "user_msg"]
    assert tl["samples"][0]["text"].startswith("How much") and "untrusted" in tl["warning"]
    assert "5213300000002" not in json.dumps(tl)


def test_no_anomalies_on_normal_data(fixture_db):
    assert metrics.anomalies("yesterday")["anomalies"] == []


# --------------------------------------------------------------------------- #
# Anomaly rules: each fires on its fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean(make_client, monkeypatch):
    make_client(LIMITS_ENABLED="false")
    monkeypatch.setattr(metrics, "now", lambda: NOW)


def _history(cost_input_tokens=1_000_000):
    """30 active hours over the previous 7 days, $3 each (1M Sonnet input tokens)."""
    start = datetime(2026, 10, 4, 8, 0, tzinfo=TZ)
    for i in range(30):
        call((start + timedelta(hours=5 * i)).strftime("%Y-%m-%d %H:%M"), "agent", "claude-sonnet-4-5",
             inp=cost_input_tokens)


@pytest.mark.parametrize("spike,fires", [(4_000_000, True), (2_000_000, False)])
def test_hourly_cost_spike(clean, spike, fires):
    _history()
    call(f"{D} 15:10", "agent", "claude-sonnet-4-5", inp=spike)
    found = [a for a in metrics.anomalies("yesterday")["anomalies"] if a["rule"] == "hourly_cost_spike"]
    assert bool(found) == fires
    if fires:
        assert found[0]["hour"] == f"{D} 15:00" and found[0]["median_usd"] == 3.0


def test_hourly_rule_needs_history(clean):
    call(f"{D} 15:10", "agent", "claude-sonnet-4-5", inp=50_000_000)
    assert metrics.anomalies("yesterday")["anomalies"] == []


@pytest.mark.parametrize("read,fires", [(1000, True), (50_000, False)])
def test_low_cache_hit(clean, read, fires):
    for i in range(21):
        ev(f"{D} 12:{i:02d}", "agent_turn", f"x{i}", outcome="answered")
        call(f"{D} 12:{i:02d}", "agent", "claude-sonnet-4-5", inp=5000, read=read)
        call(f"{D} 12:{i:02d}", "guard", "claude-haiku-4-5", inp=500)
    rules = [a["rule"] for a in metrics.anomalies("yesterday")["anomalies"]]
    assert ("low_cache_hit" in rules) == fires


@pytest.mark.parametrize("guards,enabled,fires", [(2, "true", True), (10, "true", False), (2, "false", False)])
def test_guard_ratio(make_client, monkeypatch, guards, enabled, fires):
    make_client(LIMITS_ENABLED="false", GUARD_ENABLED=enabled)
    monkeypatch.setattr(metrics, "now", lambda: NOW)
    for i in range(10):
        ev(f"{D} 12:{i:02d}", "agent_turn", f"x{i}", outcome="answered")
    for i in range(guards):
        call(f"{D} 12:{i:02d}", "guard", "claude-haiku-4-5", inp=500)
    found = [a for a in metrics.anomalies("yesterday")["anomalies"] if a["rule"] == "guard_ratio"]
    assert bool(found) == fires
    if fires:
        assert found[0]["ratio"] == 0.2


@pytest.mark.parametrize("errs,fires", [(2, True), (1, False)])
def test_error_rate(clean, errs, fires):
    for i in range(20):
        ev(f"{D} 12:{i:02d}", "user_msg", f"x{i}", topic="otro", route="agent", lang="es")
    for i in range(errs):
        ev(f"{D} 12:{i:02d}", "error", f"x{i}", kind="SendError")
    rules = [a["rule"] for a in metrics.anomalies("yesterday")["anomalies"]]
    assert ("error_rate" in rules) == fires


@pytest.mark.parametrize("used,fires", [(900, True), (700, False)])
def test_token_budget(make_client, monkeypatch, used, fires):
    make_client(LIMITS_ENABLED="true", DAILY_TOKEN_BUDGET="1000")
    monkeypatch.setattr(metrics, "now", lambda: NOW)
    limits._bump("5213300000001", input_tokens=used)
    found = [a for a in metrics.anomalies("today")["anomalies"] if a["rule"] == "token_budget"]
    assert bool(found) == fires


def test_admin_metrics_endpoint(fixture_db, make_client):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/admin/metrics?range=yesterday").status_code == 403
    r = c.get("/admin/metrics?range=yesterday", headers={"X-Admin-Token": "admin"})
    assert r.status_code == 200 and r.json()["conversations"] == 5
    assert c.get("/admin/metrics?range=nope", headers={"X-Admin-Token": "admin"}).status_code == 400


# --------------------------------------------------------------------------- #
# WhatsApp formatting: what the sanitizer fixed (agent_turn.fmt_fixed) and the model's violation rate
# --------------------------------------------------------------------------- #
def _fixed(tables=0, headers=0):
    return {"tables": tables, "headers": headers, "links": 0, "bold": 0, "other": 0}


@pytest.mark.parametrize("tables,fires", [(3, True), (2, False)])   # 3/21 > 10%, 2/21 is not
def test_format_drift_fires_when_tables_are_fixed_on_over_10_percent_of_a_busy_day(clean, tables, fires):
    for i in range(21):
        ev(f"{D} 12:{i:02d}", "agent_turn", f"x{i}", outcome="answered",
           fmt_fixed=_fixed(tables=1 if i < tables else 0))
    found = [a for a in metrics.anomalies("yesterday")["anomalies"] if a["rule"] == "format_drift"]
    assert bool(found) == fires
    if fires:
        assert found[0]["agent_turns"] == 21 and found[0]["table_fix_rate"] == round(3 / 21, 4)


def test_format_drift_needs_more_than_20_turns(clean):
    for i in range(20):
        ev(f"{D} 12:{i:02d}", "agent_turn", f"x{i}", outcome="answered", fmt_fixed=_fixed(tables=1))
    assert not [a for a in metrics.anomalies("yesterday")["anomalies"] if a["rule"] == "format_drift"]


def test_agent_turns_reports_the_format_fix_rate_and_skips_older_events(clean):
    for i in range(4):
        ev(f"{D} 12:0{i}", "agent_turn", f"y{i}", outcome="answered", reply_words=50, trimmed=False,
           fmt_fixed=_fixed(tables=1 if i == 0 else 0, headers=1 if i == 1 else 0))
    ev(f"{D} 12:09", "agent_turn", "old", outcome="answered")          # before the counters: not counted
    t = metrics.agent_turns("yesterday")
    assert t["format_fix_rate"] == 0.5 and t["table_fix_rate"] == 0.25
    assert metrics.overview("yesterday")["agent_turns"]["format_fix_rate"] == 0.5


def test_the_digest_shows_the_format_fix_rate(clean):
    from app.analytics import digest
    ev(f"{D} 12:00", "conversation_start", "c1", "5213300000001", "start", source="organic", lang="es", channel="meta")
    ev(f"{D} 12:00", "user_msg", "c1", "5213300000001", topic="otro", route="agent", lang="es", kind="text")
    for i in range(4):
        ev(f"{D} 12:0{i}", "agent_turn", f"y{i}", outcome="answered", reply_words=50, trimmed=False,
           fmt_fixed=_fixed(tables=1 if i < 2 else 0))
    assert "Formato: 50% de respuestas con tabla o título corregidos" in digest.build("yesterday")
