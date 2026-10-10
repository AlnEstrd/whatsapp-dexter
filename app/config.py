"""Centralized configuration, loaded from environment variables / .env.

Every provider's credentials are optional: a channel is only enabled when its
credentials are present. That lets Twilio and Meta live side by side so a
pivot (approval / rejection) is an .env change, not a code change.
"""
from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Channel selection -------------------------------------------------
    # Provider used for *outbound-initiated* messages (e.g. /admin/send).
    # Inbound replies always go back through the channel the message came in on.
    wa_provider: str = "twilio"  # "twilio" | "meta"

    # Public HTTPS base URL (ngrok / Vercel / DO). Needed to validate Twilio
    # signatures behind a tunnel, e.g. https://abcd.ngrok-free.app
    public_base_url: str = ""

    # --- Agent ---------------------------------------------------------------
    agent_mode: str = "claude"  # "claude" | "echo"
    anthropic_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-4-5"
    # Talk-companion flows: welcome menu, AI-maturity quiz, KB agent.
    # false = plain agent only (used by the channel plumbing tests).
    flows_enabled: bool = True

    # --- Conversation lifecycle (app/sessions.py) ----------------------------
    sessions_file: str = "data/sessions.json"  # survives restarts; "" = memory only
    idle_warn_minutes: float = 10     # "are you still there?" after this long without activity
    idle_close_minutes: float = 5     # ...then close this long after the warning (0 = never)
    restart_notice: bool = True       # tell recent users when a restart wiped their chat
    # A user's "that's all, thanks" (deterministic phrases + the agent's end_conversation tool)
    # closes the chat like "adiós" does. Bare "gracias" never closes.
    close_on_intent: bool = True

    # --- Persistence (app/db.py) ---------------------------------------------
    db_path: str = "data/bot.sqlite3"   # "" = memory only (state lost on restart)
    history_max_messages: int = 40      # agent history kept per user (tool pairs never split)
    retention_days: float = 30          # purge closed/idle conversations older than this
    processed_retention_days: float = 7  # webhook dedupe ids kept this long

    # --- Usage and cost caps (app/limits.py) --------------------------------
    limits_enabled: bool = True         # false = no caps (usage is still recorded)
    user_msg_per_minute: int = 6
    user_msg_per_day: int = 120
    user_files_per_day: int = 10
    user_tokens_per_day: int = 300_000  # input + output, all model calls
    daily_token_budget: int = 5_000_000  # all users; 80% -> Slack alert, 100% -> no model calls

    # --- Off-topic guard (app/guard.py) --------------------------------------
    guard_enabled: bool = True
    guard_model: str = "claude-haiku-4-5"   # cheap classifier run before the main agent
    offtopic_max_warnings: int = 2          # the next off-topic message escalates to Alan

    # --- Twilio WhatsApp (Sandbox or production sender) ---------------------
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_whatsapp_from: str = "+14155238886"  # Twilio Sandbox number by default
    twilio_validate_signature: bool = True

    # --- Meta WhatsApp Cloud API -------------------------------------------
    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_api_version: str = "v20.0"
    whatsapp_verify_token: Optional[str] = None
    meta_app_secret: Optional[str] = None  # enables X-Hub-Signature-256 checks

    # --- Human escalation / alerts -----------------------------------------
    # Where humans pick up conversations: "slack" (default) or "flex".
    # Flex only applies to Twilio-channel users; others always fall back to Slack.
    handoff_target: str = "slack"
    flex_workspace_sid: Optional[str] = None   # WSxxx (TaskRouter > Workspaces)
    flex_workflow_sid: Optional[str] = None    # WWxxx (needed if >1 workflow)
    flex_task_channel: str = "chat"            # TaskRouter channel for messaging
    # Safety net: Twilio closes the handoff conversation after this ISO-8601
    # duration even if nobody wraps up the Flex task.
    flex_conversation_close_timer: str = "PT24H"
    slack_webhook_url: Optional[str] = None  # Slack Incoming Webhook URL

    # Slack app (bot token) -> one thread per WhatsApp conversation, two-way.
    # When SLACK_BOT_TOKEN is unset the Incoming Webhook behavior above is
    # used unchanged. See README "Slack threads".
    slack_bot_token: Optional[str] = None      # xoxb-... (OAuth & Permissions)
    slack_signing_secret: Optional[str] = None  # Basic Information > Signing Secret
    slack_channel_id: Optional[str] = None     # C0123... channel that hosts the threads
    slack_alert_user_id: Optional[str] = None  # U0123... member to @mention on escalation
    slack_thread_idle_hours: float = 24        # new thread after this long w/o customer msgs
    slack_state_file: str = ".slack_threads.json"  # survives restarts/--reload; "" = memory only
    admin_token: Optional[str] = None        # protects /admin/* endpoints

    # --- App -----------------------------------------------------------------
    allowed_senders: str = ""

    @property
    def allowed_senders_list(self) -> List[str]:
        return [s.strip().lstrip("+") for s in self.allowed_senders.split(",") if s.strip()]

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def flex_enabled(self) -> bool:
        return self.handoff_target == "flex" and self.twilio_enabled and bool(self.flex_workspace_sid)

    @property
    def slack_threads_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_channel_id)

    @property
    def meta_enabled(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
