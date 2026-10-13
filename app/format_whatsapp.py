"""WhatsApp formatting: the one sanitizer for text the bot writes, and the limits it must respect.

Models write Markdown; WhatsApp shows the characters as typed. ``to_whatsapp`` rewrites Markdown
(tables, ``#`` headers, ``**bold**``, ``[text](url)``, HTML, images, rules) into WhatsApp syntax
(``*bold*``, ``_italic_``, ``~strike~``, triple-backtick blocks, ``• `` bullets), counts what it
fixed, and is idempotent: ``f(f(x)) == f(x)``. Text without Markdown comes back unchanged.

Blocks. A text is a run of blocks separated by blank lines; a fenced code block is never split.
A block is ``code``, ``list`` (any bullet or numbered line; a converted table is a list), ``header``
(one bold line) or ``prose``. The length trim and the 4,096-character splitter only cut between
blocks: never inside a list or a code block, and never right after a header.

``WA_FORMAT_SANITIZE=false`` gives back the conversion from before (``legacy_to_whatsapp``), with no
table rules, no block-aware trim and no splitter, so the text is sent exactly as it was.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import get_settings

logger = logging.getLogger("format")

COUNTER_KEYS = ("tables", "headers", "links", "bold", "other")
META_LIMIT = 4096            # Cloud API text body (TwilioChannel.text_limit is 1600)
# Interactive limits (Meta Cloud API). Enforced by fit() in the channel, never by cutting mid-word.
LIST_ROW_TITLE, LIST_ROW_DESCRIPTION, LIST_BUTTON = 24, 72, 20
REPLY_BUTTON, INTERACTIVE_BODY = 20, 1024
LIST_MAX_ROWS, REPLY_MAX_BUTTONS = 10, 3
TABLE_MAX_ROWS = 8           # more rows than this: the first 8, then "… y N más"


def enabled() -> bool:
    return get_settings().wa_format_sanitize


def _counts() -> dict[str, int]:
    return dict.fromkeys(COUNTER_KEYS, 0)


# --------------------------------------------------------------------------- #
# Line and block structure
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^[ \t]*```[\w+.-]*[ \t]*$")          # a fence line: ``` and an optional language
_LIST_LINE = re.compile(r"^[ \t]*(?:[•\-*+]|\d+[.)])[ \t]")
_HEADER_LINE = re.compile(r"^\*[^*\n]+\*:?$")                 # one bold line, e.g. "*Ejemplo: x*"
_SENTENCE = re.compile(r"(?<=[.!?…])(?=\s|$)|(?<=\S)(?=\n)")  # where a prose block may be cut


def _segments(lines: list[str]) -> list[tuple[bool, list[str]]]:
    """(is_code, lines): a fenced block is kept whole; an unclosed fence runs to the end."""
    segs: list[tuple[bool, list[str]]] = []
    cur: list[str] = []
    code = False
    for line in lines:
        if _FENCE.match(line):
            if not code:
                if cur:
                    segs.append((False, cur))
                cur, code = [line], True
            else:
                cur.append(line)
                segs.append((True, cur))
                cur, code = [], False
        else:
            cur.append(line)
    if cur:
        segs.append((code, cur))
    return segs


@dataclass(frozen=True)
class Block:
    kind: str    # "code" | "list" | "header" | "prose"
    start: int   # offsets into the text
    end: int


def _kind(seg: list[str], is_code: bool) -> str:
    if is_code:
        return "code"
    if any(_LIST_LINE.match(line) for line in seg):
        return "list"
    if len(seg) == 1 and _HEADER_LINE.match(seg[0].strip()):
        return "header"
    return "prose"


def blocks(text: str) -> list[Block]:
    """The blocks of a text (see the module docstring). Offsets are exact: text[b.start:b.end]."""
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    out: list[Block] = []
    buf: list[int] = []
    code = buf_code = False

    def flush() -> None:
        if buf:
            seg = [lines[k] for k in buf]
            out.append(Block(_kind(seg, buf_code), starts[buf[0]], starts[buf[-1]] + len(lines[buf[-1]])))
            buf.clear()

    for k, line in enumerate(lines):
        if _FENCE.match(line):
            if not code:
                flush()
                code = buf_code = True
                buf.append(k)
            else:
                buf.append(k)
                code = False
                flush()
                buf_code = False
        elif code:
            buf.append(k)
        elif not line.strip():
            flush()
        else:
            buf.append(k)
    flush()
    return out


def paragraphs(text: str) -> list[str]:
    """The blocks as strings (a code block keeps its blank lines), in order."""
    return [part for b in blocks(text) if (part := text[b.start:b.end].strip())]


def is_header(paragraph: str) -> bool:
    p = paragraph.strip()
    return "\n" not in p and bool(_HEADER_LINE.match(p))


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
_IMG = re.compile(r"([ \t]*)!\[[^\]\n]*\]\([^)\n]*\)([ \t]*)")  # an image: gone, one space kept between words
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?|mailto):[^)\s]+)\)")
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>\n]*)?/?>")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.M)
_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_QUOTE = re.compile(r"^[ \t]*>[ \t]?(?=\S)", re.M)  # a quote line with text: "> " (WhatsApp)
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_NESTED = re.compile(r"^[ \t]+(?=•|\d+[.)][ \t])", re.M)
_BOLD3 = re.compile(r"\*\*\*(?=\S)([^\n]+?)(?<=\S)\*\*\*")
_BOLD2 = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_UNDER = re.compile(r"__(?=\S)([^\n]+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")
_STRAY = re.compile(r"\*{2,}|~~+")
_CODE = re.compile(r"(?<![`\w])`(?!`)([^`\n]+?)`(?![`\w])")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_SPLIT = re.compile(r"(?<!\\)\|")
# A marker "opens" after a space or punctuation and "closes" before one; any other * or _ is not a marker.
_OPENS = frozenset(" \t([{\"'“‘¡¿-—–")
_CLOSES = frozenset(" \t.,;:!?)]}\"'”’…-—–")


def _sub(rx: re.Pattern, repl, s: str, c: dict[str, int], key: str) -> str:
    """Replace every match, counting only the matches that really changed."""
    def one(m: re.Match) -> str:
        new = repl(m) if callable(repl) else m.expand(repl)
        if new != m.group(0):
            c[key] += 1
        return new
    return rx.sub(one, s)


def _link(m: re.Match) -> str:
    text, url = m.group(1).strip(), m.group(2)
    return url if text == url else f"{text}: {url}"


def _image(m: re.Match) -> str:
    return " " if (m.group(1) or m.group(2)) else ""


def _heading(m: re.Match) -> str:
    title = m.group(1).strip().strip("*_").strip().rstrip(":").strip()
    return f"*{title}*" if title else ""


def _balance_char(line: str, ch: str) -> tuple[str, int]:
    """Drop the markers of ``ch`` that do not pair up on this line (a span never crosses a line)."""
    stack: list[int] = []
    drop: set[int] = set()
    for i, x in enumerate(line):
        if x != ch:
            continue
        prev = line[i - 1] if i else " "
        nxt = line[i + 1] if i + 1 < len(line) else " "
        closes = not prev.isspace() and (nxt.isspace() or nxt in _CLOSES)
        opens = (prev.isspace() or prev in _OPENS) and not nxt.isspace()
        if closes and stack:
            stack.pop()
        elif opens:
            stack.append(i)
        elif closes:
            drop.add(i)
    drop.update(stack)
    if not drop:
        return line, 0
    return "".join(x for i, x in enumerate(line) if i not in drop), len(drop)


def _inline_style(s: str, c: dict[str, int]) -> str:
    s = _sub(_BOLD3, r"*\1*", s, c, "bold")
    s = _sub(_BOLD2, r"*\1*", s, c, "bold")
    s = _sub(_UNDER, r"*\1*", s, c, "bold")
    s = _sub(_STRIKE, r"~\1~", s, c, "other")
    s = _sub(_STRAY, "", s, c, "other")
    s = _sub(_CODE, r"```\1```", s, c, "other")
    lines = []
    for line in s.split("\n"):
        for ch in "*_":
            line, n = _balance_char(line, ch)
            c["other"] += n
        lines.append(line)
    return "\n".join(lines)


def _cell(s: str, c: dict[str, int]) -> str:
    s = _sub(_BR, " ", s, c, "other")
    s = _sub(_TAG, "", s, c, "other")
    s = _sub(_IMG, _image, s, c, "other")
    s = _sub(_LINK, _link, s, c, "links")
    return _inline_style(s, c).strip()


def _block(s: str, c: dict[str, int]) -> str:
    """Prose: line rules (headers, rules, quotes, bullets) and the inline rules."""
    s = _sub(_BR, "\n", s, c, "other")
    s = _sub(_TAG, "", s, c, "other")
    s = _sub(_IMG, _image, s, c, "other")
    s = _sub(_LINK, _link, s, c, "links")
    s = _sub(_HEADING, _heading, s, c, "headers")
    s = _sub(_HR, "", s, c, "other")
    s = _sub(_QUOTE, "> ", s, c, "other")
    s = _sub(_BULLET, "• ", s, c, "other")
    s = _sub(_NESTED, "", s, c, "other")
    s = _inline_style(s, c)
    return "\n".join(line.rstrip() for line in s.split("\n"))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def _is_table_start(line: str, nxt: str) -> bool:
    return bool(line.strip()) and "|" in line and "|" in nxt and bool(_SEPARATOR.match(nxt))


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [part.replace("\\|", "|").strip() for part in _SPLIT.split(s)]


def _more(n: int, lang: str) -> str:
    return f"… y {n} más" if lang == "es" else f"… and {n} more"


def _table(head: list[str], rows: list[list[str]], lang: str, c: dict[str, int]) -> list[str]:
    """A table as WhatsApp text, with no pipes. Two columns: one bullet per row, ``• *a*: b``.
    One column: a bullet per row. Three or more: one block per row, the first non-empty cell as a
    bold title, then ``• header: value`` for the rest (blocks separated by a blank line)."""
    c["tables"] += 1
    head = [_cell(h, c) for h in head]
    rows = [[_cell(v, c) for v in r] for r in rows]
    shown, hidden = rows[:TABLE_MAX_ROWS], len(rows) - TABLE_MAX_ROWS

    def at(r: list[str], i: int) -> str:
        return r[i] if i < len(r) else ""

    out: list[str] = []
    if len(head) == 1:
        out = [f"• {at(r, 0)}" for r in shown if at(r, 0)]
    elif len(head) == 2:
        for r in shown:
            a, b = at(r, 0), at(r, 1)
            if a and b:
                out.append(f"• *{a}*: {b}")
            elif a or b:
                out.append(f"• *{a}*" if a else f"• {b}")
    else:
        for r in shown:
            vals = [(i, v) for i, v in enumerate(r) if v]
            if not vals:
                continue
            if out:
                out.append("")
            out.append(f"*{vals[0][1]}*")
            for i, v in vals[1:]:
                label = head[i] if i < len(head) else ""
                out.append(f"• {label}: {v}" if label else f"• {v}")
    if hidden > 0:
        if out and len(head) > 2:
            out.append("")
        out.append(_more(hidden, lang))
    return out


def _chunks(seg: list[str], lang: str, c: dict[str, int]) -> list[tuple[bool, list[str]]]:
    """Split prose lines into (is_table, lines): a table is any header row + separator row + rows."""
    chunks: list[tuple[bool, list[str]]] = []
    buf: list[str] = []
    i = 0
    while i < len(seg):
        if i + 1 < len(seg) and _is_table_start(seg[i], seg[i + 1]):
            j = i + 2
            while j < len(seg) and seg[j].strip() and "|" in seg[j]:
                j += 1
            if buf:
                chunks.append((False, buf))
                buf = []
            chunks.append((True, _table(_cells(seg[i]), [_cells(r) for r in seg[i + 2:j]], lang, c)))
            i = j
        else:
            buf.append(seg[i])
            i += 1
    if buf:
        chunks.append((False, buf))
    return chunks


def _prose(seg: list[str], lang: str, c: dict[str, int]) -> list[str]:
    out: list[str] = []
    after_table = False
    for is_table, chunk in _chunks(seg, lang, c):
        if is_table:
            if out and out[-1].strip():
                out.append("")          # a table never sits glued to the paragraph before it
            out.extend(chunk)
            after_table = True
        else:
            lines = _block("\n".join(chunk), c).split("\n")
            if after_table and lines and lines[0].strip():
                out.append("")
            out.extend(lines)
            after_table = False
    return out


def _code_lines(seg: list[str], c: dict[str, int]) -> list[tuple[str, bool]]:
    """A fenced block: the fences become plain ``` (no language tag in WhatsApp); contents untouched."""
    if seg[0].strip() != "```":
        c["other"] += 1
    closed = len(seg) > 1 and bool(_FENCE.match(seg[-1]))
    body = seg[1:-1] if closed else seg[1:]
    out = [("```", True)] + [(line, True) for line in body]
    if closed:
        if seg[-1].strip() != "```":
            c["other"] += 1
        out.append(("```", True))
    return out


def _assemble(lines: list[tuple[str, bool]]) -> str:
    """Collapse blank runs to one blank line, and strip the whole reply (as the conversion always did)."""
    kept: list[tuple[str, bool]] = []
    for line, is_code in lines:
        blank = not is_code and not line.strip()
        if blank and (not kept or (not kept[-1][1] and not kept[-1][0].strip())):
            continue
        kept.append((line, is_code))
    while kept and not kept[-1][1] and not kept[-1][0].strip():
        kept.pop()
    return "\n".join(line for line, _ in kept).strip()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
MAX_PASSES = 12  # a removed marker can change its neighbours, so the rules run again until nothing changes


def _pass(text: str, lang: str) -> tuple[str, dict[str, int]]:
    c = _counts()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[str, bool]] = []
    for is_code, seg in _segments(lines):
        if is_code:
            out.extend(_code_lines(seg, c))
        else:
            out.extend((line, False) for line in _prose(seg, lang, c))
    return _assemble(out), c


def to_whatsapp(text: str, lang: str = "es") -> tuple[str, dict[str, int]]:
    """Markdown -> WhatsApp text, and what was fixed (COUNTER_KEYS). Pure: no settings, no model.

    ``lang`` only picks the "… y N más" wording of a long table. Idempotent: the result is a fixed
    point of the rules, so running it again changes nothing (and counts nothing)."""
    c = _counts()
    if not text:
        return text or "", c
    out = text
    for _ in range(MAX_PASSES):
        nxt, counted = _pass(out, lang)
        for key in COUNTER_KEYS:
            c[key] += counted[key]
        if nxt == out:
            break
        out = nxt
    return out, c


def sanitize(text: str, lang: str = "es") -> tuple[str, dict[str, int]]:
    """Bot text on its way out. With WA_FORMAT_SANITIZE on: ``to_whatsapp``. Off: the conversion from
    before. Never raises: if the sanitizer fails, the text goes out as written."""
    if not enabled():
        return legacy_to_whatsapp(text), _counts()
    try:
        return to_whatsapp(text, lang)
    except Exception:  # noqa: BLE001  (a formatting bug must never block a reply)
        logger.exception("WhatsApp formatting failed; sending the text as written")
        return text or "", _counts()


def for_wire(text: str) -> str:
    """The last step before a channel sends bot text. Idempotent with the earlier passes; skipped when off."""
    return sanitize(text)[0] if enabled() else text


def fit(text: str, limit: int) -> str:
    """``text`` if it fits ``limit`` characters; else cut at the last whole word that fits, plus "…"."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    m = re.search(r"\s\S*$", head)
    if m and m.start() > 0:
        head = head[: m.start()]
    return head.rstrip(" ,;:-–—") + "…"


def _split_lines(lines: list[str], room: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        pieces = [line[i:i + room] for i in range(0, len(line), room)] or [""]
        for piece in pieces:
            add = len(piece) + (1 if cur else 0)
            if cur and size + add > room:
                chunks.append(cur)
                cur, size, add = [], 0, len(piece)
            cur.append(piece)
            size += add
    if cur:
        chunks.append(cur)
    return chunks


def _split_block(piece: str, is_code: bool, limit: int) -> list[str]:
    lines = piece.split("\n")
    if is_code:
        inner = lines[1:-1] if len(lines) > 1 and _FENCE.match(lines[-1]) else lines[1:]
        parts = ["```\n" + "\n".join(p) + "\n```" for p in _split_lines(inner, limit - 8)]
        return parts or ["```\n```"]
    return ["\n".join(p) for p in _split_lines(lines, limit)]


def pack(text: str, limit: int = META_LIMIT) -> list[str]:
    """Split text into messages of at most ``limit`` characters, cutting only between blocks. A block
    alone over the limit is split on its lines (a code block is re-fenced on each part)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for b in blocks(text):
        piece = text[b.start:b.end]
        if len(piece) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(_split_block(piece, b.kind == "code", limit))
        elif cur and len(cur) + 2 + len(piece) > limit:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts


# --------------------------------------------------------------------------- #
# The conversion from before (WA_FORMAT_SANITIZE=false). Kept verbatim.
# --------------------------------------------------------------------------- #
_L_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_L_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_L_BOLD_ITALIC = re.compile(r"\*{3}(?=\S)([^\n]+?)(?<=\S)\*{3}")
_L_BOLD = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_L_BOLD_UNDER = re.compile(r"__(?=\S)([^\n]+?)(?<=\S)__")
_L_STRIKE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")
_L_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_L_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_L_STRAY = re.compile(r"\*{2,}")
_L_BLANKS = re.compile(r"\n{3,}")


def _l_heading(m: re.Match) -> str:
    title = m.group(1).strip("* ").strip()
    return f"*{title}*" if title else ""


def _l_convert(text: str) -> str:
    text = _L_HR.sub("", text)
    text = _L_HEADING.sub(_l_heading, text)
    text = _L_BOLD_ITALIC.sub(r"*\1*", text)
    text = _L_BOLD.sub(r"*\1*", text)
    text = _L_BOLD_UNDER.sub(r"*\1*", text)
    text = _L_STRIKE.sub(r"~\1~", text)
    text = _L_BULLET.sub("• ", text)
    text = _L_LINK.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)}: {m.group(2)}", text)
    return _L_STRAY.sub("", text)


def legacy_to_whatsapp(text: str) -> str:
    if not text:
        return text or ""
    parts = text.split("```")
    # even indexes are outside code fences; odd ones are code and stay as written
    parts = [_l_convert(p) if i % 2 == 0 else p for i, p in enumerate(parts)]
    return _L_BLANKS.sub("\n\n", "```".join(parts)).strip()
