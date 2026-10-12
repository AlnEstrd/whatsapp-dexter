"""Eval harness: run one case through the real webhook -> handle_inbound path and score it.

A case (``evals/cases/*.json``, a list of objects):

    {
      "id": "slides-lamina-8",          unique
      "lang": "es",                     language of the user
      "prior": ["hola"],                optional earlier user turns (replies ignored)
      "message": "lámina 8",            the user message being scored
      "env": {"LEADS_ENABLED": "true"}  optional settings for this case (e.g. turn leads on)
      "media": {"type": "image", "caption": "..."}       optional inbound file (Meta)
      "mock": {"guard": "IN" | "OUT",                    offline only: guard verdict and the
               "prior_guard": "OUT",                     (verdict for the prior turns, default IN)
               "agent": [{"tool": "send_slides", "input": {"slide": "8"}}, {"text": "Va."}],
               "prior_agent": ["Es la regla de BCG. ¿Algo más en lo que te pueda ayudar?"]},
                                                         scripted agent steps for this message, and
                                                         the texts the agent gives to the prior turns
      "expect": {
        "max_words": 150,                                  optional: all replies of the turn together
        "route": "menu" | "quiz" | "agent" | "close" | "handoff" | "offtopic_warning" | "file_reply"
                 (or a list of acceptable routes),
        "must_contain": ["..."], "must_not_contain": ["..."],   case-insensitive substrings
        "tool_called": "end_conversation",                      optional
        "language": "es" | "en",                                language of the reply
        "topic": "roles_ownership",                             topic label of the message
                                                                (user_msg event, app/analytics/topics.py)
        "state": {"quiz_active": true, "handoff": true, "closed": true, "file_sent": true,
                  "consent": true, "consent_asked": true, "opted_out": true}   (the last three: leads)
      }
    }

Offline (default): the Anthropic API is mocked with respx, so the run checks routing, tool
dispatch, state and the *deterministic* replies (text checks are skipped for model-written
replies, i.e. route "agent"). Live: the real models answer and every expectation is checked.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import respx

CASES_DIR = Path(__file__).parent / "cases"
RESULTS_DIR = Path(__file__).parent / "results"
# file_reply = the bot answered with a file (slides, report); file_note = a static reply about an
# inbound file (too big, wait for the quiz...); limited = a usage cap; human = Alan is in charge.
ROUTES = {"menu", "quiz", "agent", "close", "handoff", "offtopic_warning", "file_reply",
          "file_note", "limited", "human", "unsupported", "on_request", "consent", "opt_out", "lead_form",
          "email"}

GRAPH = "https://graph.facebook.com/v20.0"
META_SEND = f"{GRAPH}/111/messages"
META_UPLOAD = f"{GRAPH}/111/media"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=eval"
SLACK = "https://hooks.slack.test/T/B/X"
ANTHROPIC = "https://api.anthropic.com/v1/messages"
WA_ID = "5213312345678"
USER = "523312345678"
DEFAULT_TEXT = {"es": "Respuesta simulada.", "en": "Mocked answer."}


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        for case in json.loads(f.read_text(encoding="utf-8")):
            case["_file"] = f.name
            cases.append(case)
    ids = [c["id"] for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate case ids: {dupes}"
    return cases


def validate(case: dict) -> list[str]:
    errs = []
    for key in ("id", "lang", "message", "expect"):
        if key not in case:
            errs.append(f"missing {key}")
    exp = case.get("expect", {})
    routes = exp.get("route")
    routes = routes if isinstance(routes, list) else [routes]
    if not routes or any(r not in ROUTES for r in routes):
        errs.append(f"bad route {exp.get('route')!r}")
    if exp.get("language") not in (None, "es", "en"):
        errs.append("language must be es|en")
    if exp.get("topic") is not None:
        from app.analytics import topics
        if exp["topic"] not in topics.TOPICS:
            errs.append(f"unknown topic {exp['topic']!r}")
    if exp.get("max_words") is not None and not isinstance(exp["max_words"], int):
        errs.append("max_words must be an integer")
    return errs


def tiny_png(w: int = 8, h: int = 8) -> bytes:
    """A valid grey PNG (no third-party imaging library needed)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def meta_payload(mid: str, text: str = "", media: Optional[dict] = None) -> dict:
    if media:
        mtype = media.get("type", "image")
        body = {"id": "MEDIA1", "mime_type": "image/png" if mtype == "image" else "application/pdf"}
        if media.get("caption"):
            body["caption"] = media["caption"]
        msg = {"from": WA_ID, "id": mid, "type": mtype, mtype: body}
    else:
        msg = {"from": WA_ID, "id": mid, "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": WA_ID, "profile": {"name": "Eval User"}}], "messages": [msg]}}]}]}


def _api_message(content: list[dict], stop: str) -> dict:
    return {"id": "msg_eval", "type": "message", "role": "assistant", "model": "mock", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


class MockModels:
    """Answers the Anthropic API: guard verdicts and the case's scripted agent steps."""

    def __init__(self, case: dict) -> None:
        self.case = case
        self.final = False
        self.steps: list[dict] = list(case.get("mock", {}).get("agent", []))
        self.prior_steps: list[str] = list(case.get("mock", {}).get("prior_agent", []))
        self.agent_requests: list[dict] = []
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "tools" not in body:  # the guard classifier
            mock = self.case.get("mock", {})
            verdict = mock.get("guard", "IN") if self.final else mock.get("prior_guard", "IN")
            return httpx.Response(200, json=_api_message([{"type": "text", "text": verdict}], "end_turn"))
        self.agent_requests.append(body)
        default = {"text": DEFAULT_TEXT.get(self.case.get("lang"), DEFAULT_TEXT["es"])}
        if self.final:
            step = self.steps.pop(0) if self.steps else default
        else:
            step = {"text": self.prior_steps.pop(0)} if self.prior_steps else default
        content = [{"type": "text", "text": step["text"]}] if step.get("text") else []
        if step.get("tool"):
            self.n += 1
            content.append({"type": "tool_use", "id": f"toolu_{self.n}", "name": step["tool"],
                            "input": step.get("input", {})})
            return httpx.Response(200, json=_api_message(content, "tool_use"))
        return httpx.Response(200, json=_api_message(content, "end_turn"))


@dataclass
class Result:
    id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    route: Optional[str] = None
    topic: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) or "ok"

    def as_dict(self) -> dict:
        return {"id": self.id, "passed": self.passed, "reason": self.reason, "route": self.route,
                "topic": self.topic, "tools": self.tools, "replies": self.replies}


def _replies(calls: list) -> tuple[list[str], bool]:
    out, file_sent = [], False
    for c in calls:
        if not str(c.request.url).endswith("/messages"):
            continue
        m = json.loads(c.request.content)
        t = m.get("type")
        if t == "text":
            out.append(m["text"]["body"])
        elif t == "interactive":
            out.append(m["interactive"]["body"]["text"])
        elif t in ("image", "document"):
            file_sent = True
            out.append(f"[{t}]")
    return out, file_sent


def run_case(case: dict, make_client: Callable[..., Any], live: bool = False,
             api_key: str = "sk-offline") -> Result:
    from app import main

    errs = validate(case)
    if errs:
        return Result(case.get("id", "?"), False, errs)

    pauses = main.BUBBLE_PAUSE_MIN, main.BUBBLE_PAUSE_MAX
    main.BUBBLE_PAUSE_MIN = main.BUBBLE_PAUSE_MAX = 0.0  # no "typing" pauses in evals
    try:
        return _run(case, make_client, live, api_key)
    finally:
        main.BUBBLE_PAUSE_MIN, main.BUBBLE_PAUSE_MAX = pauses


def _run(case: dict, make_client: Callable[..., Any], live: bool, api_key: str) -> Result:
    from app import agent, main
    from app.flows import menu

    models = MockModels(case)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        meta = router.post(META_SEND).mock(return_value=httpx.Response(200, json={"messages": [{"id": "x"}]}))
        router.post(META_UPLOAD).mock(return_value=httpx.Response(200, json={"id": "UPLOADED"}))
        router.post(SLACK).mock(return_value=httpx.Response(200))
        png = tiny_png()
        router.get(f"{GRAPH}/MEDIA1").mock(return_value=httpx.Response(
            200, json={"url": MEDIA_URL, "mime_type": "image/png", "file_size": len(png)}))
        router.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=png))
        if live:
            router.route(host="api.anthropic.com").pass_through()
        else:
            router.post(ANTHROPIC).mock(side_effect=models)

        tmp = tempfile.mkdtemp(prefix="eval-")  # every case starts from an empty DB
        run = uuid.uuid4().hex[:8]                # and unique message ids (webhook dedupe)
        try:
            client = make_client(FLOWS_ENABLED="true", AGENT_MODE="claude", ANTHROPIC_API_KEY=api_key,
                                 GUARD_ENABLED="true", USER_MSG_PER_MINUTE="100",
                                 DB_PATH=f"{tmp}/bot.sqlite3", **case.get("env", {}))
            for i, prior in enumerate(case.get("prior", [])):
                client.post("/webhooks/meta", json=meta_payload(f"wamid.{run}.p{i}", prior))
            n_before = len(meta.calls)
            models.final = True
            main.last_route.pop(USER, None)
            agent.tools_called.pop(USER, None)
            client.post("/webhooks/meta", json=meta_payload(f"wamid.{run}.final", case["message"], case.get("media")))
            replies, file_sent = _replies(list(meta.calls)[n_before:])
            state = _state(file_sent)
            topic = _last_topic()
        finally:
            from app import db
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)

    res = Result(case["id"], True, route=main.last_route.get(USER), topic=topic,
                 tools=list(agent.tools_called.get(USER, [])), replies=replies)
    exp = case["expect"]
    routes = exp["route"] if isinstance(exp["route"], list) else [exp["route"]]
    if res.route not in routes:
        res.reasons.append(f"route {res.route!r} not in {routes}")
    if exp.get("topic") and res.topic != exp["topic"]:
        res.reasons.append(f"topic {res.topic!r} != {exp['topic']!r}")
    if exp.get("tool_called") and exp["tool_called"] not in res.tools:
        res.reasons.append(f"tool {exp['tool_called']!r} not called (called: {res.tools or 'none'})")
    if exp.get("tool_not_called") and exp["tool_not_called"] in res.tools:
        res.reasons.append(f"tool {exp['tool_not_called']!r} should not be called")

    model_written = res.route == "agent"
    check_text = live or not model_written
    text = "\n".join(replies)
    low = text.lower()
    if check_text:
        for s in exp.get("must_contain", []):
            if s.lower() not in low:
                res.reasons.append(f"missing {s!r}")
        for s in exp.get("must_not_contain", []):
            if s.lower() in low:
                res.reasons.append(f"contains forbidden {s!r}")
        words = re.sub(r"\[(image|document)\]", "", text)
        if exp.get("language") and words.strip():
            got = menu.detect_lang(words, None)
            if got != exp["language"]:
                res.reasons.append(f"reply language {got}, expected {exp['language']}")
    if not replies and exp.get("route") != "close":
        res.reasons.append("no reply sent")
    if exp.get("max_words") is not None:
        n_words = len(re.sub(r"\[(image|document)\]", "", text).split())
        if n_words > exp["max_words"]:
            res.reasons.append(f"{n_words} words > max_words {exp['max_words']}")

    for key, want in exp.get("state", {}).items():
        if key not in state:
            res.reasons.append(f"unknown state check {key!r}")
        elif state[key] != bool(want):
            res.reasons.append(f"state {key} != {want}")
    if case.get("media") and not live and model_written:
        sent = any(b.get("type") in ("image", "document")
                   for r in models.agent_requests for m in r["messages"] if isinstance(m["content"], list)
                   for b in m["content"])
        if not sent:
            res.reasons.append("attachment did not reach the agent")
    res.passed = not res.reasons
    return res


def _last_topic() -> Optional[str]:
    """Topic of the scored message: the newest user_msg event (metrics log)."""
    from app import db
    from app.analytics import events
    events.drain()
    rows = db.query("SELECT data_json FROM events WHERE type = 'user_msg' AND user_id = ? "
                    "ORDER BY id DESC LIMIT 1", (USER,))
    return json.loads(rows[0][0]).get("topic") if rows else None


def _state(file_sent: bool) -> dict[str, bool]:
    from app import sessions, store
    from app.flows import quiz
    from app.leads import core, flows
    lead = core.by_user(USER) if core.enabled() else None
    pending = flows.current(USER) or {}
    return {"quiz_active": quiz.active(USER), "handoff": store.in_handoff(USER),
            "closed": not sessions.is_open(USER), "file_sent": file_sent,
            "consent": bool(lead and lead["consent_followup"]), "opted_out": bool(lead and lead["opted_out_at"]),
            "consent_asked": pending.get("kind") == "consent",
            "consent_explicit": bool(lead and lead["consent_type"] == "explicit"),
            "consent_tacit": bool(lead and lead["consent_type"] == "tacit_notice"),
            "consent_declined": bool(lead and lead["consent_type"] == "declined"),
            "email_captured": bool(lead and lead["email"]),
            "followup_pending": bool(lead and lead["followup_status"] == "pending")}


def table(results: list[Result]) -> str:
    w = max([len(r.id) for r in results] + [4])
    lines = [f"{'case'.ljust(w)}  result  reason", f"{'-' * w}  ------  ------"]
    for r in results:
        lines.append(f"{r.id.ljust(w)}  {'PASS' if r.passed else 'FAIL'}    {r.reason}")
    passed = sum(r.passed for r in results)
    lines.append(f"\n{passed}/{len(results)} passed")
    return "\n".join(lines)
