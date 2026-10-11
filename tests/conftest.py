import pytest
from fastapi.testclient import TestClient

BASE_ENV = {
    "WA_PROVIDER": "twilio",
    "PUBLIC_BASE_URL": "https://bot.example.com",
    "AGENT_MODE": "echo",
    "FLOWS_ENABLED": "false",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "twilio-secret",
    "TWILIO_WHATSAPP_FROM": "+14155238886",
    "WHATSAPP_ACCESS_TOKEN": "meta-token",
    "WHATSAPP_PHONE_NUMBER_ID": "111",
    "WHATSAPP_VERIFY_TOKEN": "verify-me",
    "META_APP_SECRET": "",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.test/T/B/X",
    "ADMIN_TOKEN": "admin",
    "ALLOWED_SENDERS": "",
    "ANTHROPIC_API_KEY": "",
    "HANDOFF_TARGET": "slack",
    "FLEX_WORKSPACE_SID": "",
    "FLEX_WORKFLOW_SID": "",
    # Slack app (thread bridge) off by default -> legacy webhook path
    "SLACK_BOT_TOKEN": "",
    "SLACK_SIGNING_SECRET": "",
    "SLACK_CHANNEL_ID": "",
    "SLACK_ALERT_USER_ID": "",
    "SLACK_THREAD_IDLE_HOURS": "24",
    "SESSIONS_FILE": "",
    "GUARD_ENABLED": "true",
    "SLACK_STATE_FILE": "",
    "LIMITS_ENABLED": "true",
    "RUN_LIVE_EVALS": "",
    # Metrics: on (the default), every optional extra off; never the real Admin API.
    "ANALYTICS_ENABLED": "true",
    "ENTRY_SOURCES": "",
    "MODEL_PRICES_JSON": "",
    "ANTHROPIC_ADMIN_KEY": "",
    "METRICS_DIGEST_ENABLED": "true",
    "SLACK_METRICS_CHANNEL_ID": "",
    "MCP_ENABLED": "false",
    "MCP_OAUTH_CLIENT_ID": "",
    "MCP_OAUTH_CLIENT_SECRET": "",
    "MCP_OWNER_PASSWORD": "",
    "MCP_PUBLIC_URL": "",
    "CSAT_ENABLED": "false",
}


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """Build a TestClient with a clean config (ignores the real .env)."""

    def _make(**overrides):
        """Fresh process state. Keyword ``fresh_db=False`` keeps the SQLite file (restart tests)."""
        monkeypatch.chdir(tmp_path)  # so pydantic-settings can't find the real .env
        env = {**BASE_ENV, "DB_PATH": str(tmp_path / "data" / "bot.sqlite3"), **overrides}
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        from app import agent, db, guard, limits, main, on_request, sessions, slack_bridge, status, store
        from app.analytics import anthropic_usage, digest, events, mcp_server
        from app.flows import quiz
        from app.channels import reset_channels
        from app.config import get_settings

        events.drain()  # queued analytics writes belong to the previous test's DB
        get_settings.cache_clear()
        reset_channels()
        agent._agent = None
        db.close()
        store.reset_memory()
        limits.reset()
        main.last_route.clear()
        quiz.reset()
        agent._pending.clear()
        agent._docs.clear()
        agent.tools_called.clear()
        store.reset_threads()
        slack_bridge.reset()
        sessions.reset()
        guard.reset()
        agent.turn_stats.clear()
        agent._gaps.clear()
        main._turn_topic.clear()
        digest.reset()
        anthropic_usage.reset()
        mcp_server.reset()
        on_request.reset()
        status.reset()
        agent._unlocked.clear()

        from app.main import app
        return TestClient(app)

    yield _make
    from app.analytics import events
    events.drain()
    from app import db
    from app.config import get_settings
    db.close()
    get_settings.cache_clear()
