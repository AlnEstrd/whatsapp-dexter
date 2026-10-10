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
}


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """Build a TestClient with a clean config (ignores the real .env)."""

    def _make(**overrides):
        monkeypatch.chdir(tmp_path)  # so pydantic-settings can't find the real .env
        for k, v in {**BASE_ENV, **overrides}.items():
            monkeypatch.setenv(k, v)

        from app import agent, guard, sessions, slack_bridge, store
        from app.flows import quiz
        from app.channels import reset_channels
        from app.config import get_settings

        get_settings.cache_clear()
        reset_channels()
        agent._agent = None
        store._conversations.clear()
        store._handoff.clear()
        store._seen_ids.clear()
        store._transcripts.clear()
        store._lang.clear()
        quiz.reset()
        agent._pending.clear()
        agent._docs.clear()
        store.reset_threads()
        slack_bridge.reset()
        sessions.reset()
        guard.reset()

        from app.main import app
        return TestClient(app)

    yield _make
    from app.config import get_settings
    get_settings.cache_clear()
