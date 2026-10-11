"""SQLite persistence (stdlib ``sqlite3``, no extra dependency).

The bot keeps its working state in process memory (``store``, ``sessions``,
``flows.quiz``) and writes every change through to ``data/bot.sqlite3``
(``DB_PATH``). On startup ``main.restore_state`` reads it all back, so a
deploy or crash no longer wipes conversations.

- One connection, guarded by a lock: safe with uvicorn's single worker,
  ``BackgroundTasks`` and the occasional ``asyncio.to_thread`` call.
- WAL mode + ``busy_timeout``: readers never block the writer, and a backup
  (``sqlite3 data/bot.sqlite3 ".backup ..."``) can run while the bot is up.
- Versioned migrations: ``schema_version`` holds the last applied step of
  ``MIGRATIONS``; new steps are appended, never edited.
- Failures never break a WhatsApp reply: callers use :func:`safe`, which logs,
  alerts Slack once and lets the turn continue from memory.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("db")

MIGRATIONS: list[list[str]] = [
    # 1 - initial schema
    [
        """CREATE TABLE conversations (
            user_id TEXT PRIMARY KEY,
            channel TEXT, reply_to TEXT, profile_name TEXT,
            mode TEXT NOT NULL DEFAULT 'bot',          -- bot | slack | flex (human handoff)
            lang TEXT,
            transcript_json TEXT NOT NULL DEFAULT '[]', -- plain-text log shown to humans on handoff
            last_customer_at REAL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, role TEXT NOT NULL,
            content_json TEXT NOT NULL,                 -- Anthropic message content, text only
            created_at REAL NOT NULL)""",
        "CREATE INDEX messages_user ON messages(user_id, id)",
        "CREATE TABLE quiz_state (user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        "CREATE TABLE quiz_results (user_id TEXT PRIMARY KEY, result_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        "CREATE TABLE sessions (user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        """CREATE TABLE slack_threads (
            user_id TEXT PRIMARY KEY, thread_ts TEXT NOT NULL, channel_id TEXT,
            last_customer_at REAL, mode TEXT, state_json TEXT NOT NULL)""",
        "CREATE UNIQUE INDEX slack_threads_ts ON slack_threads(thread_ts)",
        "CREATE TABLE processed_messages (message_id TEXT PRIMARY KEY, received_at REAL NOT NULL)",
        """CREATE TABLE usage (
            user_id TEXT NOT NULL, day TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            files INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day))""",
        # one-time flags: "limit notice sent today", "80% budget alert sent", ...
        "CREATE TABLE flags (key TEXT PRIMARY KEY, created_at REAL NOT NULL)",
    ],
    # 2 - small key/value store: last Meta account events and public counters (app/status.py)
    [
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)",
    ],
    # 3 - metrics & governance (app/analytics/). ts columns are UTC ISO-8601 strings
    #     ("2026-10-11T14:05:09.123456Z"), so range filters are plain string compares.
    [
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL, user_id TEXT, conversation_id TEXT,
            type TEXT NOT NULL, step TEXT,
            data_json TEXT NOT NULL DEFAULT '{}')""",  # never free message text
        "CREATE INDEX events_ts ON events(ts)",
        "CREATE INDEX events_conversation ON events(conversation_id)",
        "CREATE INDEX events_type_ts ON events(type, ts)",
        # Scrubbed attendee questions that reached the agent (kind=question) and the agent's
        # own note_gap summaries (kind=gap_note). Text is untrusted data.
        """CREATE TABLE question_samples (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, conversation_id TEXT,
            topic TEXT, lang TEXT, kind TEXT NOT NULL DEFAULT 'question', text TEXT NOT NULL)""",
        "CREATE INDEX question_samples_ts ON question_samples(ts)",
        """CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL, user_id TEXT, conversation_id TEXT,
            purpose TEXT NOT NULL, model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER, stop_reason TEXT, error TEXT)""",
        "CREATE INDEX llm_calls_ts ON llm_calls(ts)",
        # Meta message id (wamid) of each bot send -> conversation, for status callbacks.
        """CREATE TABLE outbound_messages (
            wamid TEXT PRIMARY KEY, ts TEXT NOT NULL, user_id TEXT, conversation_id TEXT)""",
        # USD per million tokens; rewritten from app/analytics/pricing.py on every start, so a
        # price fix re-prices history (cost is computed at query time, never stored).
        """CREATE TABLE model_prices (
            model TEXT PRIMARY KEY, input REAL NOT NULL, output REAL NOT NULL,
            cache_write REAL NOT NULL, cache_read REAL NOT NULL)""",
        # MCP OAuth tokens, stored as SHA-256 hashes only.
        """CREATE TABLE mcp_tokens (
            token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL,
            client_id TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '',
            expires_at REAL NOT NULL, created_at REAL NOT NULL)""",
        # Read-only views: the only thing the MCP `query` tool may read. User ids masked.
        """CREATE VIEW v_events AS
            SELECT id, ts, '****' || substr(user_id, -4) AS user, conversation_id, type, step, data_json
            FROM events""",
        """CREATE VIEW v_conversations AS
            SELECT conversation_id,
                   '****' || substr(MAX(user_id), -4) AS user,
                   MIN(CASE WHEN type = 'conversation_start' THEN ts END) AS started_at,
                   MAX(CASE WHEN type = 'conversation_close' THEN ts END) AS closed_at,
                   MAX(CASE WHEN type = 'conversation_close' THEN json_extract(data_json, '$.reason') END) AS close_reason,
                   MAX(CASE WHEN type = 'conversation_close' THEN json_extract(data_json, '$.last_step') END) AS last_step,
                   MAX(CASE WHEN type = 'conversation_close' THEN json_extract(data_json, '$.turns') END) AS turns,
                   MAX(CASE WHEN type = 'conversation_start' THEN json_extract(data_json, '$.source') END) AS source,
                   MAX(CASE WHEN type = 'conversation_start' THEN json_extract(data_json, '$.lang') END) AS lang,
                   MAX(type = 'handoff') AS handoff
            FROM events WHERE conversation_id IS NOT NULL GROUP BY conversation_id""",
        """CREATE VIEW v_llm_calls AS
            SELECT l.id, l.ts, '****' || substr(l.user_id, -4) AS user, l.conversation_id, l.purpose,
                   l.model, l.input_tokens, l.output_tokens, l.cache_read_tokens, l.cache_write_tokens,
                   l.latency_ms, l.stop_reason, l.error,
                   ROUND((l.input_tokens * p.input + l.output_tokens * p.output
                          + l.cache_write_tokens * p.cache_write + l.cache_read_tokens * p.cache_read)
                         / 1000000.0, 6) AS cost_usd
            FROM llm_calls l LEFT JOIN model_prices p ON p.model = l.model""",
        """CREATE VIEW v_topics AS
            SELECT ts, conversation_id, json_extract(data_json, '$.topic') AS topic,
                   json_extract(data_json, '$.route') AS route, json_extract(data_json, '$.lang') AS lang
            FROM events WHERE type = 'user_msg'""",
        """CREATE VIEW v_gaps AS
            SELECT e.ts, e.conversation_id, json_extract(e.data_json, '$.topic') AS topic,
                   json_extract(e.data_json, '$.source') AS source, s.text AS sample, n.text AS note
            FROM events e
            LEFT JOIN question_samples s ON s.id = json_extract(e.data_json, '$.sample_id')
            LEFT JOIN question_samples n ON n.id = json_extract(e.data_json, '$.note_id')
            WHERE e.type = 'gap'""",
    ],
]

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: str = ""
_alerted = False  # Slack alert about DB failures: once per process


def path() -> str:
    from .config import get_settings
    return get_settings().db_path


def enabled() -> bool:
    return bool(path())


def _open(p: str) -> sqlite3.Connection:
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending MIGRATIONS. Returns the schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version, statements in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql in statements:
                conn.execute(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info("DB migrated to schema version %d", version)
        current = version
    return current


def conn() -> sqlite3.Connection:
    """The shared connection, opened (and migrated) on first use."""
    global _conn, _conn_path
    p = path()
    with _lock:
        if _conn is None or _conn_path != p:
            if _conn is not None:
                _conn.close()
            _conn, _conn_path = _open(p), p
        return _conn


def close() -> None:  # tests / shutdown
    global _conn, _conn_path, _alerted
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path, _alerted = None, "", False


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """``with db.transaction() as c: c.execute(...)`` - all or nothing."""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")


def safe(label: str, fn: Callable[[], Any], default: Any = None) -> Any:
    """Run a DB call; on failure log, alert Slack once, and return ``default``.

    State stays in memory for the rest of the turn, so the user still gets a reply.
    No-op (returns ``default``) when ``DB_PATH`` is empty."""
    if not enabled():
        return default
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception("DB %s failed", label)
        _alert_once(f"{label}: {exc.__class__.__name__}: {exc}")
        return default


def _alert_once(detail: str) -> None:
    global _alerted
    if _alerted:
        return
    _alerted = True
    try:
        import asyncio

        from .escalation import notify_slack
        loop = asyncio.get_running_loop()
        loop.create_task(notify_slack(f":warning: Bot database error, running from memory: {detail[:300]}"))
    except RuntimeError:
        pass  # no running loop (startup / tests): the log line is enough
    except Exception:  # noqa: BLE001
        logger.exception("Could not alert Slack about the DB error")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Webhook idempotency (Meta / Twilio message ids, Slack event ids)
# --------------------------------------------------------------------------- #
def mark_processed(key: str) -> bool:
    """Record ``key``. True if it is new, False if it was already processed."""
    cur = execute("INSERT OR IGNORE INTO processed_messages (message_id, received_at) VALUES (?, ?)",
                  (key, now()))
    return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# One-time flags
# --------------------------------------------------------------------------- #
def set_flag_once(key: str) -> bool:
    """True the first time ``key`` is set, False afterwards."""
    cur = execute("INSERT OR IGNORE INTO flags (key, created_at) VALUES (?, ?)", (key, now()))
    return cur.rowcount == 1


def has_flag(key: str) -> bool:
    return bool(query("SELECT 1 FROM flags WHERE key = ?", (key,)))


# --------------------------------------------------------------------------- #
# Conversation wipe + retention
# --------------------------------------------------------------------------- #
def wipe_conversation(user_id: str) -> None:
    """Delete the agent history and quiz progress of one user, atomically."""
    with transaction() as c:
        c.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM quiz_state WHERE user_id = ?", (user_id,))


def purge(retention_days: float, processed_days: float = 7) -> list[str]:
    """Delete closed or idle conversations older than ``retention_days`` (every table that
    holds that user) and processed message ids older than ``processed_days``.
    Returns the purged user ids so callers can drop them from memory too."""
    t = now()
    cutoff = t - retention_days * 86400
    users = []
    for row in query("SELECT user_id, state_json FROM sessions"):
        try:
            s = json.loads(row["state_json"])
        except ValueError:
            s = {}
        last = max(s.get("last_activity") or 0, s.get("closed_at") or 0)
        if last < cutoff:
            users.append(row["user_id"])
    # Conversations with no session row (e.g. imported thread links only).
    for row in query("SELECT user_id FROM conversations WHERE updated_at < ? AND user_id NOT IN "
                     "(SELECT user_id FROM sessions)", (cutoff,)):
        users.append(row["user_id"])
    day_cutoff = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    with transaction() as c:
        for uid in users:
            for table in ("messages", "quiz_state", "quiz_results", "sessions", "slack_threads",
                          "conversations"):
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM processed_messages WHERE received_at < ?", (t - processed_days * 86400,))
        c.execute("DELETE FROM usage WHERE day < ?", (day_cutoff,))
        c.execute("DELETE FROM flags WHERE created_at < ?", (cutoff,))
        # Metrics: same RETENTION_DAYS for events, samples and model calls.
        iso_cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
        for table in ("events", "question_samples", "llm_calls", "outbound_messages"):
            c.execute(f"DELETE FROM {table} WHERE ts < ?", (iso_cutoff,))
        c.execute("DELETE FROM mcp_tokens WHERE expires_at < ?", (t,))
    if users:
        logger.info("Retention: purged %d conversation(s) older than %s days", len(users), retention_days)
    return users


def count(table: str) -> int:
    return query(f"SELECT COUNT(*) FROM {table}")[0][0]
