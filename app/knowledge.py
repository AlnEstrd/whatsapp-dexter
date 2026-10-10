"""Knowledge base for the talk companion bot.

- ``app/kb/core/*.md``   small, curated docs (talk, knowledge base, about Alan).
  Loaded whole into the system prompt and prompt-cached, so every answer can
  use them without a retrieval step.
- ``app/kb/sources/*``   long source documents (reports, papers). Too big for
  the prompt: split into chunks and searched with a tiny BM25 index through
  the ``search_sources`` tool.

No vector DB on purpose: the corpus is small and keyword search over
Spanish/English text is good enough for a talk companion.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KB_DIR = Path(__file__).parent / "kb"
CHUNK_WORDS = 220
CHUNK_OVERLAP = 40

_STOP = set(
    """a al algo algunas algunos ante antes como con contra cual cuando de del desde donde dos el
    ella ellas ellos en entre era es esa ese eso esta este esto estos fue ha hay la las le les lo los
    mas me mi muy no nos o os otra otro para pero poco por que quien se ser si sin sobre su sus tambien
    te tiene tu un una uno unos y ya the of and to in a is for on that with as are by be this it from
    or an at was were which can has have not their more these than its also such into we our""".split()
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _norm(text)) if t not in _STOP and len(t) > 1]


@dataclass
class Chunk:
    source: str
    index: int
    text: str


def core_text() -> str:
    """All core docs concatenated, each under a header naming the file."""
    parts = []
    for path in sorted((KB_DIR / "core").glob("*.md")):
        parts.append(f"<document name=\"{path.stem}\">\n{path.read_text(encoding='utf-8').strip()}\n</document>")
    return "\n\n".join(parts)


def voice_text() -> str:
    """Dexter's persona and writing voice (app/kb/voz.md); a style guide, not a source of facts."""
    path = KB_DIR / "voz.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _clean(raw: str) -> str:
    raw = raw.replace("\f", "\n")
    raw = re.sub(r"-\n(\w)", r"\1", raw)          # de-hyphenate line breaks
    return re.sub(r"\s+", " ", raw).strip()


def _chunks_for(path: Path) -> list[Chunk]:
    words = _clean(path.read_text(encoding="utf-8", errors="ignore")).split(" ")
    out, i, n = [], 0, 0
    step = CHUNK_WORDS - CHUNK_OVERLAP
    while i < len(words):
        out.append(Chunk(path.stem, n, " ".join(words[i:i + CHUNK_WORDS])))
        i += step
        n += 1
    return out


class _Index:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.tfs = [Counter(tokenize(c.text)) for c in chunks]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = 4) -> list[tuple[float, Chunk]]:
        q = tokenize(query)
        if not q or not self.chunks:
            return []
        k1, b = 1.5, 0.75
        scored = []
        for tf, length, chunk in zip(self.tfs, self.lens, self.chunks):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * length / (self.avg or 1)))
            if s > 0:
                scored.append((s, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]


@lru_cache
def _index() -> _Index:
    chunks: list[Chunk] = []
    for path in sorted((KB_DIR / "sources").glob("*")):
        if path.suffix in {".txt", ".md"}:
            chunks.extend(_chunks_for(path))
    return _Index(chunks)


def list_sources() -> list[str]:
    return sorted({c.source for c in _index().chunks})


def search_sources(query: str, k: int = 4) -> str:
    """Tool output: top passages with their source names."""
    hits = _index().search(query, k)
    if not hits:
        return "No matching passages in the source documents."
    return "\n\n".join(f"[{c.source} #{c.index}]\n{c.text}" for _, c in hits)
