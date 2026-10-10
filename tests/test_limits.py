"""Usage recording and cost caps (app/limits.py)."""
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import db, limits, store
from app.flows import quiz

from .test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from .test_flows import meta_payload

USER = "523312345678"
CLAUDE = {"FLOWS_ENABLED": "true", "AGENT_MODE": "claude", "ANTHROPIC_API_KEY": "sk-test",
          "GUARD_ENABLED": "false", "SLACK_ALERT_USER_ID": "UOWNER"}
QUESTION = "¿Qué es la regla 10-20-70?"


class CountingLLM:
    """Fake messages.create: text reply with a usage block, counting calls."""

    def __init__(self, inp=100, out=20, text="Respuesta"):
        self.calls, self.inp, self.out, self.text = 0, inp, out, text

    async def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=self.inp, output_tokens=self.out,
                                  cache_read_input_tokens=5000, cache_creation_input_tokens=7))


@pytest.fixture
def wa():
    with respx.mock(assert_all_called=False) as router:
        slack = router.post(SLACK).mock(return_value=httpx.Response(200))
        send = router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
        yield SimpleNamespace(send=send, slack=slack, router=router)


def bodies(route):
    return [dict(httpx.QueryParams(c.request.content.decode()))["Body"] for c in route.calls]


def slack_texts(route):
    return [json.loads(c.request.content)["text"] for c in route.calls]


def client(make_client, llm=None, **env):
    from app import agent
    c = make_client(**{**CLAUDE, **env})
    agent.get_agent()._client = SimpleNamespace(messages=llm or CountingLLM())
    return c


def seed(user_id, **cols):
    limits._bump(user_id, **cols)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_usage_recorded_per_user_and_day_with_cache_tokens(make_client, wa, monkeypatch):
    from app import guard
    c = client(make_client, GUARD_ENABLED="true")
    g = guard.Guard()
    g._client = SimpleNamespace(messages=CountingLLM(inp=30, out=1, text="IN"))
    guard._guard = g
    twilio_post(c, tw_msg("¿Por qué 3 de cada 4 empresas no ven valor en la IA?", sid="S1"))
    row = dict(db.query("SELECT * FROM usage WHERE user_id = ?", (USER,))[0])
    assert row["day"] == limits.today()
    assert row["requests"] == 1 and row["files"] == 0
    assert row["input_tokens"] == 130 and row["output_tokens"] == 21          # agent + guard
    assert row["cache_read_tokens"] == 10000 and row["cache_write_tokens"] == 14


def test_day_is_mexico_city(monkeypatch):
    monkeypatch.setattr(limits, "now", lambda: 1_760_000_000 + 0)  # 2025-10-09 08:53 UTC
    assert limits.today() == "2025-10-09"
    monkeypatch.setattr(limits, "now", lambda: 1_760_068_800)      # 2025-10-10 04:00 UTC = 22:00 CDMX
    assert limits.today() == "2025-10-09"


# --------------------------------------------------------------------------- #
# Per-user caps
# --------------------------------------------------------------------------- #
def test_per_minute_limit_notice_once_then_resets(make_client, wa, monkeypatch):
    llm = CountingLLM()
    c = client(make_client, llm, USER_MSG_PER_MINUTE="6")
    t = [1_000_000.0]
    monkeypatch.setattr(limits, "now", lambda: t[0])
    for i in range(9):
        twilio_post(c, tw_msg(f"{QUESTION} {i}", sid=f"S{i}"))
    b = bodies(wa.send)
    assert b.count(limits.text("minute", "es")) == 1
    assert llm.calls == 6                                   # messages 7-9 never reached the model
    assert sum("Rate limit" in x for x in slack_texts(wa.slack)) == 1

    t[0] += 61                                              # the minute is over
    twilio_post(c, tw_msg(QUESTION, sid="S10"))
    assert llm.calls == 7


def test_quiz_answers_do_not_count_against_the_minute(make_client, wa):
    c = client(make_client, USER_MSG_PER_MINUTE="3")
    twilio_post(c, tw_msg("diagnóstico", sid="Q0"))
    for i in range(len(quiz.QUESTIONS)):
        twilio_post(c, tw_msg("2", sid=f"Q{i + 1}"))
    assert quiz.last_result(USER) is not None
    assert limits.text("minute", "es") not in bodies(wa.send)


def test_per_day_message_limit_offers_handoff_and_humano_still_works(make_client, wa):
    llm = CountingLLM()
    c = client(make_client, llm, USER_MSG_PER_DAY="3")
    for i in range(5):
        twilio_post(c, tw_msg(f"{QUESTION} {i}", sid=f"S{i}"))
    b = bodies(wa.send)
    assert b.count(limits.text("day", "es")) == 1 and "*humano*" in limits.text("day", "es")
    assert llm.calls == 3
    assert any("Daily message limit" in x for x in slack_texts(wa.slack))
    twilio_post(c, tw_msg("humano", sid="S9"))
    assert store.handoff_target(USER) == "slack"


@respx.mock
def test_per_day_file_limit_skips_download(make_client):
    meta = respx.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
    slack = respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = client(make_client, USER_FILES_PER_DAY="2")
    seed(USER, files=2)
    payload = meta_payload("img1", "x")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "5213312345678", "id": "img1", "type": "image",
        "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}}
    c.post("/webhooks/meta", json=payload)              # no Graph media mock: a download would fail
    texts = [json.loads(x.request.content).get("text", {}).get("body") for x in meta.calls]
    assert limits.text("files", "es") in texts
    assert any("Daily file limit" in json.loads(x.request.content)["text"] for x in slack.calls)


def test_per_user_token_limit_blocks_model_but_menu_and_quiz_work(make_client, wa):
    llm = CountingLLM(inp=80, out=30)
    c = client(make_client, llm, USER_TOKENS_PER_DAY="100")
    twilio_post(c, tw_msg(QUESTION, sid="S1"))          # 110 tokens recorded
    twilio_post(c, tw_msg(QUESTION + " 2", sid="S2"))
    twilio_post(c, tw_msg(QUESTION + " 3", sid="S3"))
    assert llm.calls == 1
    assert bodies(wa.send).count(limits.text("tokens", "es")) == 1
    assert sum("Daily token limit" in x for x in slack_texts(wa.slack)) == 1
    twilio_post(c, tw_msg("hola", sid="S4"))
    assert "¿Qué quieres hacer?" in bodies(wa.send)[-1]
    twilio_post(c, tw_msg("diagnóstico", sid="S5"))
    assert quiz.active(USER) and llm.calls == 1


def test_global_budget_alert_at_80_then_stop_at_100(make_client, wa):
    llm = CountingLLM(inp=15, out=5)
    c = client(make_client, llm, DAILY_TOKEN_BUDGET="1000", USER_MSG_PER_MINUTE="50")
    seed("5200000001", input_tokens=790)
    twilio_post(c, tw_msg(QUESTION, sid="S1"))          # 810 -> 81%
    twilio_post(c, tw_msg(QUESTION + " b", sid="S2"))   # 830, no second alert
    alerts = [x for x in slack_texts(wa.slack) if "Daily token budget" in x]
    assert len(alerts) == 1 and "<@UOWNER>" in alerts[0] and "83%" not in alerts[0]

    seed("5200000001", input_tokens=200)                # 1030 -> exhausted
    n = llm.calls
    twilio_post(c, tw_msg(QUESTION + " c", sid="S3"))
    twilio_post(c, tw_msg(QUESTION + " d", sid="S4"))
    assert llm.calls == n
    assert bodies(wa.send).count(limits.text("budget", "es")) == 1
    # deterministic paths still work
    twilio_post(c, tw_msg("menu", sid="S5"))
    assert "¿Qué quieres hacer?" in bodies(wa.send)[-1]
    twilio_post(c, tw_msg("lámina 8", sid="S6"))        # Twilio can't send files: polite text, no model
    twilio_post(c, tw_msg("diagnóstico", sid="S7"))
    twilio_post(c, tw_msg("1", sid="S8"))
    assert quiz.active(USER) and quiz._states[USER].index == 1
    assert llm.calls == n


def test_budget_blocks_the_guard_too(make_client, wa):
    from app import guard
    c = client(make_client, GUARD_ENABLED="true", DAILY_TOKEN_BUDGET="10")
    g = guard.Guard()
    gl = CountingLLM(text="IN")
    g._client = SimpleNamespace(messages=gl)
    guard._guard = g
    seed("5200000001", input_tokens=10)
    twilio_post(c, tw_msg("¿Por qué 3 de cada 4 empresas no ven valor en la IA?", sid="S1"))
    assert gl.calls == 0 and bodies(wa.send) == [limits.text("budget", "es")]


def test_human_mode_never_limited(make_client, wa):
    c = client(make_client, USER_MSG_PER_MINUTE="2", DAILY_TOKEN_BUDGET="1")
    seed("5200000001", input_tokens=10)
    twilio_post(c, tw_msg("humano", sid="S0"))
    for i in range(5):
        twilio_post(c, tw_msg(f"mensaje {i}", sid=f"S{i + 1}"))
    mirrored = [x for x in slack_texts(wa.slack) if "[in human handoff]" in x]
    assert len(mirrored) == 5
    assert not any(limits.text(k, "es") in bodies(wa.send) for k in limits.TEXT)


def test_limits_disabled_is_legacy_but_records_usage(make_client, wa):
    llm = CountingLLM()
    c = client(make_client, llm, LIMITS_ENABLED="false", USER_MSG_PER_MINUTE="1",
               USER_MSG_PER_DAY="1", DAILY_TOKEN_BUDGET="1")
    for i in range(3):
        twilio_post(c, tw_msg(f"{QUESTION} {i}", sid=f"S{i}"))
    assert llm.calls == 3
    assert not any(limits.text(k, "es") in bodies(wa.send) for k in limits.TEXT)
    row = dict(db.query("SELECT * FROM usage WHERE user_id = ?", (USER,))[0])
    assert row["requests"] == 3 and row["input_tokens"] == 300


def test_limit_notice_in_english(make_client, wa):
    c = client(make_client, DAILY_TOKEN_BUDGET="1")
    seed("5200000001", input_tokens=10)
    twilio_post(c, tw_msg("What is the 10-20-70 rule about?", sid="S1"))
    assert bodies(wa.send) == [limits.text("budget", "en")]


# --------------------------------------------------------------------------- #
# /admin/usage
# --------------------------------------------------------------------------- #
def test_admin_usage_requires_token_and_masks_numbers(make_client, wa):
    c = client(make_client)
    seed("523312345678", requests=2, input_tokens=500, output_tokens=50)
    seed("5215550001111", requests=1, input_tokens=10)
    assert c.get("/admin/usage").status_code == 403
    assert c.get("/admin/usage", headers={"X-Admin-Token": "nope"}).status_code == 403
    r = c.get("/admin/usage", headers={"X-Admin-Token": "admin"}).json()
    assert r["totals"]["tokens"] == 560 and r["totals"]["requests"] == 3
    assert [u["user"] for u in r["top_users"]] == ["****5678", "****1111"]
    assert "523312345678" not in json.dumps(r)
    assert r["budget"]["daily_token_budget"] == 5_000_000
