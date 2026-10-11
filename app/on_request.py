"""On-request information: things Dexter knows but only tells people who ask for them.

Entries live in ``app/kb/on_request/*.md`` (shipped with the code) and, optionally,
in ``data/on_request/*.md`` on the server volume (kept out of git; same id -> the
volume wins). See app/kb/on_request/README.md for the file format.

The rule that makes this work: **the model can't suggest what it can't see.** Entries
are never in the system prompt, the core KB, the search_sources index or the menu.
They are revealed in two ways only:

- fast path (main.py): the user's message matches an entry's ``triggers`` -> the
  entry's ``## Respuesta`` is sent as-is, no model call;
- agent path: the model calls the generic ``lookup_private_info(query)`` tool, whose
  description names no topic; the server runs the same matcher on the query.

Matching is deterministic and only looks at the triggers (never the content), so a
loosely related question can't pull an entry out. Once a user received an entry, it is
"unlocked" for that conversation: its ``## Detalle`` joins *their* agent context so
follow-ups work, until the chat closes.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import get_settings

logger = logging.getLogger("on_request")

DEFAULT_DIR = Path(__file__).parent / "kb" / "on_request"
DEFAULT_MIN_SCORE = 0.75
MAX_RESPONSE_CHARS = 4000  # WhatsApp text limit is 4096; a rendered Respuesta must fit in one message
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
KNOWN_PLACEHOLDERS = {"status_line", "status"}

# Words that carry no meaning for "is this an explicit ask" (on top of the KB stopwords).
_FILLER = {"oye", "hey", "porfa", "porfavor", "favor", "please", "pls", "dime", "cuentame",
           "platicame", "tell", "me", "quiero", "saber", "want", "know", "puedes", "podrias", "could",
           "can", "you", "would", "like"}


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    id: str
    triggers: list[str]
    respuesta: str
    detalle: str = ""
    response_en: str = ""          # optional "## Response" section for English askers
    lang: list[str] = field(default_factory=lambda: ["es"])
    min_score: float = DEFAULT_MIN_SCORE
    notify_slack: bool = True
    pdf: bool = False
    topic: str = ""                # one line for the off-topic guard once unlocked
    title: str = ""                # PDF title
    source: str = ""               # file it came from (logs)


def _parse_scalar(raw: str):
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML subset (no extra dependency): ``key: value``, ``key: [a, b]`` and
    ``key:`` followed by ``  - item`` lines. Raises ValueError on anything else."""
    if not text.startswith("---"):
        raise ValueError("missing frontmatter (the file must start with ---)")
    end = re.search(r"^---\s*$", text[3:], re.M)
    if not end:
        raise ValueError("frontmatter is not closed with ---")
    block, body = text[3:3 + end.start()], text[3 + end.end():]
    meta: dict = {}
    current: Optional[str] = None
    for n, line in enumerate(block.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item:
            if current is None or not isinstance(meta.get(current), list):
                raise ValueError(f"frontmatter line {n}: list item without a key")
            meta[current].append(_parse_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not kv:
            raise ValueError(f"frontmatter line {n}: expected 'key: value'")
        key, value = kv.group(1), kv.group(2).strip()
        if value == "":
            meta[key], current = [], key
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [_parse_scalar(x) for x in value[1:-1].split(",") if x.strip()]
            current = None
        else:
            meta[key], current = _parse_scalar(value), None
    return meta, body


def _sections(body: str) -> dict[str, str]:
    """Split the body at ``## Heading`` lines (### and deeper stay inside a section)."""
    out: dict[str, str] = {}
    name, buf = None, []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            if name:
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1).strip().lower(), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf).strip()
    return out


def parse_entry(text: str, source: str = "") -> Optional[Entry]:
    """An Entry, None if ``enabled: false``. Raises ValueError when the file is invalid."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # comments are notes for Alan, never sent
    meta, body = parse_frontmatter(text)
    if meta.get("enabled", True) is False:
        return None
    eid = str(meta.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", eid):
        raise ValueError("id must be a slug (a-z, 0-9, - or _)")
    triggers = [str(t).strip() for t in (meta.get("triggers") or []) if str(t).strip()]
    if not triggers:
        raise ValueError("at least one trigger is required")
    sec = _sections(body)
    respuesta = sec.get("respuesta", "")
    if not respuesta:
        raise ValueError("missing '## Respuesta' section")
    for name in ("respuesta", "response"):
        if len(sec.get(name, "")) > MAX_RESPONSE_CHARS:
            raise ValueError(f"'## {name.title()}' is longer than {MAX_RESPONSE_CHARS} characters")
    unknown = set(PLACEHOLDER_RE.findall(body)) - KNOWN_PLACEHOLDERS
    if unknown:
        logger.warning("on-request %s: unknown placeholders %s (they render empty)", eid, sorted(unknown))
    langs = meta.get("lang") or ["es"]
    try:
        min_score = float(meta.get("min_score", DEFAULT_MIN_SCORE))
    except (TypeError, ValueError):
        raise ValueError("min_score must be a number") from None
    return Entry(
        id=eid, triggers=triggers, respuesta=respuesta, detalle=sec.get("detalle", ""),
        response_en=sec.get("response", ""), lang=[str(x) for x in langs] if isinstance(langs, list) else [str(langs)],
        min_score=min(1.0, max(0.3, min_score)), notify_slack=meta.get("notify_slack", True) is not False,
        pdf=meta.get("pdf", False) is True, topic=str(meta.get("topic") or ""),
        title=str(meta.get("title") or eid), source=source,
    )


_entries: Optional[dict[str, Entry]] = None


def enabled() -> bool:
    return bool(get_settings().on_request_enabled)


def _dirs() -> list[Path]:
    s = get_settings()
    main_dir = Path(s.on_request_dir) if s.on_request_dir else DEFAULT_DIR
    dirs = [main_dir]
    if s.on_request_extra_dir:
        dirs.append(Path(s.on_request_extra_dir))
    return dirs


def load() -> dict[str, Entry]:
    """Read and validate every entry (cached). A bad file is logged and skipped, never fatal."""
    global _entries
    if _entries is not None:
        return _entries
    found: dict[str, Entry] = {}
    if enabled():
        for d in _dirs():  # later dirs (the volume) override earlier ones on the same id
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.md")):
                if path.name.lower() == "readme.md":
                    continue
                try:
                    entry = parse_entry(path.read_text(encoding="utf-8"), str(path))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on-request entry %s skipped: %s", path, exc)
                    continue
                if entry is not None:
                    found[entry.id] = entry
        logger.info("on-request entries loaded: %d", len(found))
    _entries = found
    return found


def reset() -> None:  # tests / reload
    global _entries
    _entries = None


def get(entry_id: str) -> Optional[Entry]:
    return load().get(entry_id)


# --------------------------------------------------------------------------- #
# Matching (deterministic, triggers only)
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """lowercase, no accents, no punctuation or emoji, single spaces."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _content_tokens(text: str) -> set[str]:
    from .knowledge import tokenize  # same tokenizer as the BM25 search (accents, stopwords)
    return {_stem(t) for t in tokenize(text) if t not in _FILLER}


def score(text: str, trigger: str) -> float:
    """1.0 when the trigger phrase appears whole in the message. Otherwise how much of the
    trigger's meaningful words the message has (recall), discounted when the message is mostly
    about something else (precision < 0.5). Triggers with fewer than two meaningful words only
    match as a whole phrase. Short chit-chat ("¿cómo estás?") never reaches a
    two-word trigger ("cómo estás hecho") because both words are required."""
    t, trig = normalize(text), normalize(trigger)
    if not t or not trig:
        return 0.0
    if f" {trig} " in f" {t} ":
        return 1.0
    tt, qt = _content_tokens(trigger), _content_tokens(text)
    if len(tt) < 2 or not qt:
        return 0.0  # one-word triggers ("cómo te hicieron") only match as a whole phrase
    hit = len(tt & qt)
    recall, precision = hit / len(tt), hit / len(qt)
    return recall * min(1.0, precision * 2)


def match(text: str) -> Optional[Entry]:
    """The entry this message explicitly asks for, or None."""
    if not enabled() or not text or len(text) > 400:  # a long message is a question, not an ask
        return None
    best: tuple[float, Optional[Entry]] = (0.0, None)
    for entry in load().values():
        s = max(score(text, trig) for trig in entry.triggers)
        if s >= entry.min_score and s > best[0]:
            best = (s, entry)
    if best[1] is not None:
        logger.info("on-request match: %s (score %.2f)", best[1].id, best[0])
    return best[1]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(text: str, lang: str = "es") -> str:
    """Fill {{status_line}} / {{status}} with the live status; unknown placeholders -> ''.
    A literal {{...}} never reaches a user."""
    if "{{" not in text:
        return text
    from . import status

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        try:
            if name == "status_line":
                return status.render_line(lang)
            if name == "status":
                return status.render_block(lang)
        except Exception:  # noqa: BLE001
            logger.exception("status placeholder %s failed", name)
            return status.unavailable(lang)
        return ""

    out = PLACEHOLDER_RE.sub(_sub, text)
    out = re.sub(r"\{\{|\}\}", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def response_for(entry: Entry, lang: str) -> str:
    raw = entry.response_en if (lang == "en" and entry.response_en) else entry.respuesta
    out = render(raw, lang)
    if len(out) > MAX_RESPONSE_CHARS:  # live status made it too long: cut at a paragraph
        out = out[:MAX_RESPONSE_CHARS].rsplit("\n\n", 1)[0]
    return out


def detail_for(entry: Entry, lang: str) -> str:
    return render(entry.detalle, lang)


# --------------------------------------------------------------------------- #
# Per-conversation unlocks (kept in the session record: wiped on close, never shared)
# --------------------------------------------------------------------------- #
def unlocked(user_id: str) -> list[str]:
    from . import sessions
    s = sessions.get(user_id) or {}
    return [i for i in (s.get("unlocked") or []) if i in load()]


def unlock(user_id: str, entry_id: str) -> bool:
    """Mark an entry as given to this user. True the first time in this conversation."""
    from . import sessions
    s = sessions.get(user_id)
    if s is None:
        return False
    ids = list(s.get("unlocked") or [])
    if entry_id in ids:
        return False
    ids.append(entry_id)
    s["unlocked"] = ids
    sessions.save(user_id)
    return True


def context_block(user_id: str, lang: str) -> str:
    """Extra system text for a user who unlocked entries (empty otherwise)."""
    parts = []
    for eid in unlocked(user_id):
        e = load()[eid]
        parts.append(f"<unlocked_info id=\"{eid}\">\n{response_for(e, lang)}\n\n{detail_for(e, lang)}\n</unlocked_info>")
    if not parts:
        return ""
    return ("The user explicitly asked for the information below in this conversation, so for THIS "
            "user you may answer follow-up questions from it and suggest its sub-topics. Same voice "
            "and length rules as always. Never share secrets, IDs or anything not written here."
            + (" If they ask for it as a PDF, tell them to type *pdf*." if any(load()[i].pdf for i in unlocked(user_id)) else "")
            + "\n\n" + "\n\n".join(parts))


def guard_topics(user_id: str) -> list[str]:
    out = []
    for eid in unlocked(user_id):
        e = load()[eid]
        out.append(e.topic or e.title or eid)
    return out


PDF_WORDS = {"pdf", "expediente", "el expediente", "mandamelo en pdf", "en pdf", "send it as pdf",
             "as pdf", "as a pdf", "send me the pdf", "dossier", "the dossier", "mandame el pdf",
             "manda el pdf", "pasamelo en pdf"}


def pdf_request(user_id: str, text: str) -> Optional[Entry]:
    """'pdf' after an entry with ``pdf: true`` was unlocked -> that entry (latest first)."""
    if normalize(text) not in PDF_WORDS:
        return None
    for eid in reversed(unlocked(user_id)):
        e = load()[eid]
        if e.pdf:
            return e
    return None


def lookup(user_id: str, query: str, lang: str) -> dict:
    """The ``lookup_private_info`` tool. Matches the *query* the model passed; nothing else."""
    entry = match(query)
    if entry is None:
        return {"found": False}
    return {"found": True, "id": entry.id, "respuesta": response_for(entry, lang),
            "detalle": detail_for(entry, lang)}
