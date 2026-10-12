"""Word lists for the first-message and goodbye rules, kept in one place.

Everything is matched on ``normalize()``: lowercase, no accents, punctuation and emoji
removed. The menu (app/flows/menu.py) uses the greeting and info lists; the lifecycle
(app/sessions.py, app/main.py) uses the closer and acknowledgement lists.
"""
from __future__ import annotations

import re
import unicodedata

_NOT_WORD = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", _NOT_WORD.sub(" ", t)).strip()


# Openers that can come before a request: "hola, quisiera más información".
GREETING_PREFIXES = ("buenos dias", "buenas tardes", "buenas noches", "buen dia", "buenas",
                     "hola", "hi", "hello", "hey", "saludos")

# Opening requests for information. The welcome menu answers them; no model call.
INFO_REQUESTS = {
    "quiero mas informacion", "quisiera mas informacion", "mas informacion", "mas info", "info",
    "informacion", "que es esto", "de que se trata", "que haces", "que puedes hacer", "como funciona",
    "ayuda", "i want more info", "more info", "what is this", "what can you do", "help",
}

# Composite closers: every word is a thanks, farewell or time-of-day word, and at least one is a
# marker. "gracias, buen dia" closes; "gracias" alone does not; "buen dia, ¿qué es el nivel L2?" does not.
CLOSER_WORDS = {
    "gracias", "muchas", "mil", "buen", "buena", "buenas", "dia", "dias", "tarde", "tardes", "noche",
    "noches", "saludos", "bonito", "excelente", "feliz", "que", "tengas", "tenga", "igualmente",
    "hasta", "luego", "pronto", "nos", "vemos", "adios", "bye", "chao", "thanks", "thank", "you",
    "have", "a", "good", "nice", "great", "day", "night", "evening", "see", "ya", "later",
}
CLOSER_MARKERS = {
    "dia", "dias", "tarde", "tardes", "noche", "noches", "adios", "bye", "chao", "luego", "pronto",
    "vemos", "day", "night", "evening", "later",
}

# Bare acknowledgements and negatives: how people answer "¿Algo más…?". Emoji are checked raw,
# because normalize() drops them.
ACK_WORDS = {
    "gracias", "muchas gracias", "mil gracias", "ok", "okay", "vale", "va", "perfecto", "perfect",
    "listo", "thanks", "thank you", "great", "genial", "excelente",
}
NEGATIVE_WORDS = {"no", "no gracias", "no thanks", "no thank you", "nada", "nada mas", "nope", "es todo"}
EMOJI_ACKS = {"👍", "👍🏻", "👌", "🙏", "✅"}


def is_bare_ack(text: str) -> bool:
    return (text or "").strip() in EMOJI_ACKS or normalize(text) in ACK_WORDS


def is_negative(text: str) -> bool:
    return normalize(text) in NEGATIVE_WORDS
