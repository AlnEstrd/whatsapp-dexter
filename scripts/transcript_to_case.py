#!/usr/bin/env python3
"""Turn a real conversation into a draft eval case (evals/cases/*.json).

Sources:
  Slack thread   a Slack export file (a JSON list of messages, as in a channel export) or a
                 ``conversations.replies`` response ({"messages": [...]}). The bridge mirrors
                 the customer as "👤 ..." and the bot as "🤖 ..." (":bust_in_silhouette:" /
                 ":robot_face:" in exports); everything else is ignored.
  SQLite         a stored conversation: ``--db data/bot.sqlite3 --user 5233...`` (the plain-text
                 transcript, falling back to the agent history).

By default the *last* customer message is the one scored and the earlier ones become
``prior``; pick another with ``--turn N`` (1 = first customer message). The bot's real reply
is kept under ``observed`` so you can turn it into expectations. Review the draft before
committing: remove names, numbers and anything personal, then fill in ``expect``.

Examples:
  python scripts/transcript_to_case.py --slack thread.json --thread 1700000000.000100
  python scripts/transcript_to_case.py --db data/bot.sqlite3 --user 523312345678 --turn 3 \\
      --out evals/cases/90_regressions.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

USER_MARKS = ("👤", ":bust_in_silhouette:")
BOT_MARKS = ("🤖", ":robot_face:")


def _unescape(text: str) -> str:
    return (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def turns_from_slack(path: str, thread_ts: str | None = None) -> list[tuple[str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    messages = data.get("messages", []) if isinstance(data, dict) else data
    if thread_ts:
        messages = [m for m in messages if m.get("thread_ts") == thread_ts or m.get("ts") == thread_ts]
    messages = sorted(messages, key=lambda m: float(m.get("ts", 0)))
    turns = []
    for m in messages:
        text = _unescape(m.get("text", "")).strip()
        for marks, role in ((USER_MARKS, "user"), (BOT_MARKS, "bot")):
            mark = next((x for x in marks if text.startswith(x)), None)
            if mark:
                body = re.sub(r"\s*📎.*$", "", text[len(mark):].strip()) if role == "user" else text[len(mark):].strip()
                turns.append((role, body))
    return turns


def turns_from_db(path: str, user_id: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only: safe on a live DB
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT transcript_json FROM conversations WHERE user_id = ?", (user_id,)).fetchone()
    turns = [(r, t) for r, t in json.loads(row["transcript_json"])] if row else []
    if not turns:  # transcript cleared: rebuild from the agent history (text parts only)
        for m in conn.execute("SELECT role, content_json FROM messages WHERE user_id = ? ORDER BY id", (user_id,)):
            content = json.loads(m["content_json"])
            if isinstance(content, str):
                turns.append(("user" if m["role"] == "user" else "bot", content))
            elif m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                if text:
                    turns.append(("bot", text))
    conn.close()
    return [("user" if r == "user" else "bot", t) for r, t in turns if r in ("user", "bot")]


def make_case(turns: list[tuple[str, str]], turn: int | None = None, case_id: str | None = None) -> dict:
    from app.flows.menu import detect_lang

    user_idx = [i for i, (r, _) in enumerate(turns) if r == "user"]
    if not user_idx:
        raise SystemExit("no customer messages found")
    pick = user_idx[(turn - 1) if turn else -1]
    message = turns[pick][1]
    prior = [t for r, t in turns[:pick] if r == "user"]
    observed = []
    for r, t in turns[pick + 1:]:
        if r == "user":
            break
        observed.append(t)
    lang = detect_lang(" ".join(prior + [message]))
    slug = re.sub(r"[^a-z0-9]+", "-", message.lower())[:40].strip("-") or "turn"
    return {
        "id": case_id or f"regression-{slug}",
        "lang": lang,
        "prior": prior,
        "message": message,
        "expect": {"route": "agent", "must_contain": [], "must_not_contain": [], "language": lang},
        "observed": observed,
        "notes": "DRAFT from a real conversation: review, anonymize and fill in expect before committing.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--slack", help="Slack export / conversations.replies JSON file")
    src.add_argument("--db", help="SQLite file (data/bot.sqlite3)")
    ap.add_argument("--thread", help="thread_ts to pick from a Slack channel export")
    ap.add_argument("--user", help="user_id (with --db)")
    ap.add_argument("--turn", type=int, help="which customer message to score (1-based; default last)")
    ap.add_argument("--id", help="case id")
    ap.add_argument("--out", help="append the case to this cases file instead of printing it")
    a = ap.parse_args()
    if a.db and not a.user:
        ap.error("--db needs --user")
    turns = turns_from_slack(a.slack, a.thread) if a.slack else turns_from_db(a.db, a.user)
    case = make_case(turns, a.turn, a.id)
    if a.out:
        out = Path(a.out)
        cases = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
        cases.append(case)
        out.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"appended {case['id']} to {out}")
    else:
        print(json.dumps(case, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
