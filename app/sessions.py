"""Conversation lifecycle: open/close, idle warning, restart recovery, off-topic count.

Each user's session record is written through to SQLite (``sessions`` table,
app/db.py), like the agent history and quiz progress, so a restart (deploy,
crash, ``--reload``) picks the chat up where it was. The ``boot`` id still
tells us which sessions came from a previous process: if their state could not
be recovered, the user gets the restart apology instead of a bot that silently
"forgot" them (``RESTART_NOTICE``).

A session is *open* from the user's first message until it is closed by:
    user   - says goodbye: a whole-message closer (adiós, sería todo, that's all, ...)
             or, with CLOSE_ON_INTENT, the agent's end_conversation tool
    bot    - idle: IDLE_WARN_MINUTES without activity -> warning,
             then IDLE_CLOSE_MINUTES more -> closed
    admin  - ``close`` in the Slack thread (or POST /admin/close)
Every close sends the user a closing message.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

from . import db

logger = logging.getLogger("sessions")

BOOT_ID = uuid.uuid4().hex[:12]   # changes on every process start

_sessions: dict[str, dict[str, Any]] = {}

CLOSE_WORDS = {
    "adios", "adiós", "bye", "goodbye", "good bye", "chao", "chau", "hasta luego", "nos vemos",
    "terminar", "cerrar", "cerrar chat", "fin", "finalizar", "salir", "exit", "end", "quit",
    "gracias adios", "gracias adiós", "gracias bye", "thanks bye", "ok gracias adios",
}

# "I'm done" phrases (CLOSE_ON_INTENT). Matched after stripping punctuation, with or
# without a thanks around them. Bare "gracias" / "thanks" is NOT a closer: people say
# it mid-conversation. Free-form goodbyes go through the agent's end_conversation tool.
DONE_PHRASES = {
    "seria todo", "sería todo", "es todo", "eso es todo", "eso seria todo", "eso sería todo",
    "por ahora es todo", "seria todo por ahora", "sería todo por ahora", "nada mas", "nada más",
    "ya es todo", "listo es todo", "eso fue todo", "ya termine", "ya terminé",
    "thats all", "that's all", "thats it", "that's it", "that is all", "nothing else",
    "im done", "i'm done", "no thats all", "no that's all", "that will be all",
    "ya quedo", "ya quedó", "con eso", "con eso es todo", "listo gracias", "hasta pronto",
    "im all set", "i'm all set", "im good thanks", "i'm good thanks", "no thanks", "no thank you",
}
# A goodbye inside a longer message ("no, eso sería todo por hoy, gracias Dexter"). Only keeps
# such messages away from the off-topic guard; the agent decides whether to close.
_CLOSE_HINTS = ("seria todo", "sería todo", "eso es todo", "es todo por hoy", "ya quedo", "ya quedó",
                "nada mas por ahora", "nada más por ahora", "con eso es todo", "thats all",
                "that's all", "nothing else", "im done", "i'm done", "adios", "adiós", "bye",
                "goodbye", "chao", "hasta luego", "hasta pronto", "nos vemos")
_REENGAGE = {
    "es": "Cuando quieras, escribe *hola* para empezar de nuevo.",
    "en": "Type *hi* anytime to start again.",
}
_THANKS = r"(?:muchas |mil )?gracias|thanks?(?: you)?|thank you|ok|okay|va|vale|listo|perfecto|perfect|great|no"
_DONE_RE = re.compile(rf"^(?:(?:{_THANKS}) )*(?P<core>.+?)(?: (?:{_THANKS}))*(?: por todo| for everything)?$")

TEXT = {
    "close_user": {
        "es": "¡Gracias por platicar! 👋 Cerré la conversación. " + _REENGAGE["es"],
        "en": "Thanks for chatting! 👋 I've closed the conversation. " + _REENGAGE["en"],
    },
    "close_bot": {
        "es": "Cerré la conversación por inactividad. Si quieres retomarla, escribe *hola* y empezamos de nuevo.",
        "en": "I closed the conversation due to inactivity. Type *hi* to start again.",
    },
    "close_admin": {
        "es": "Alan cerró esta conversación. ¡Gracias por tu tiempo! Si necesitas algo más, escribe *hola*.",
        "en": "Alan closed this conversation. Thanks for your time! If you need anything else, type *hi*.",
    },
    "idle_warn": {
        "es": "¿Sigues ahí? 🙂 Si no hay actividad en los próximos {m} minutos, cerraré la conversación.",
        "en": "Still there? 🙂 If there's no activity in the next {m} minutes, I'll close the conversation.",
    },
    "restart": {
        "es": ("⚠️ Una disculpa de parte de Alan: el asistente se reinició y se perdió el historial de "
               "nuestra conversación{quiz}. Puedes seguir escribiéndome desde aquí, o escribe *menú* "
               "para ver las opciones."),
        "en": ("⚠️ Apologies from Alan: the assistant restarted and our conversation history was lost"
               "{quiz}. You can keep writing to me here, or type *menu* to see the options."),
    },
    "restart_quiz": {"es": " (incluido el diagnóstico que llevabas a la mitad)",
                     "en": " (including the assessment you were in the middle of)"},
    "offtopic_warn": {
        "es": ("Solo puedo ayudarte con la plática de Gobernanza de IA, la adopción de IA en empresas, "
               "el diagnóstico de madurez o información sobre Alan. ⚠️ Aviso {n} de {max}: si seguimos "
               "fuera de tema, pasaré la conversación a Alan."),
        "en": ("I can only help with the AI Governance talk, AI adoption in companies, the maturity "
               "assessment or info about Alan. ⚠️ Warning {n} of {max}: if we stay off topic, I'll hand "
               "the conversation to Alan."),
    },
    "offtopic_escalate": {
        "es": "Como seguimos fuera del tema de la plática, paso la conversación a Alan; él te responderá por aquí.",
        "en": "Since we're still off the talk's topic, I'm handing the conversation to Alan; he'll reply here.",
    },
}


def text(key: str, lang: Optional[str], **kw: Any) -> str:
    return TEXT[key][lang if lang in ("es", "en") else "es"].format(**kw)


def _norm(msg_text: str) -> str:
    t = re.sub(r"[!¡.?¿,;:🙂🙏👋😊]", "", (msg_text or "").strip().lower().replace("’", "'"))
    return re.sub(r"\s+", " ", t).strip()


def is_close_request(msg_text: str) -> bool:
    return _norm(msg_text) in CLOSE_WORDS


def is_done_intent(msg_text: str) -> bool:
    """'Sería todo, gracias', 'That's all, thanks' -> True; bare 'gracias' -> False."""
    t = _norm(msg_text)
    if not t or len(t.split()) > 8 or (msg_text or "").strip().endswith("?"):  # "¿es todo?" asks
        return False
    if t in DONE_PHRASES:
        return True
    m = _DONE_RE.match(t)
    return bool(m and m.group("core") in DONE_PHRASES)


def sounds_like_closing(msg_text: str) -> bool:
    t = f" {_norm(msg_text)} "
    return any(f" {h} " in t for h in _CLOSE_HINTS)


def farewell_text(farewell: Optional[str], lang: Optional[str]) -> str:
    """Agent farewell + the standard re-engage line (the default close_user text if none)."""
    f = re.sub(r"\s+", " ", farewell or "").strip()[:200]
    if not f:
        return text("close_user", lang)
    return f"{f} {_REENGAGE[lang if lang in ('es', 'en') else 'es']}"


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Persistence (SQLite ``sessions`` table; the dict is the working copy)
# --------------------------------------------------------------------------- #
def save(user_id: str) -> None:
    sess = _sessions.get(user_id)
    if sess is None:
        return
    db.safe("save session", lambda: db.execute(
        "INSERT OR REPLACE INTO sessions (user_id, state_json, updated_at) VALUES (?, ?, ?)",
        (user_id, db.dumps(sess), db.now())))


def import_legacy(path: str) -> int:
    """Import ``data/sessions.json`` (older versions) once, then rename it to ``*.imported``.
    Skipped if the table already has rows. Returns sessions imported."""
    if not path or not os.path.exists(path) or not db.enabled():
        return 0
    if db.count("sessions"):
        logger.info("Session file %s ignored: table already populated", path)
        return 0
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for uid, sess in data.items():
        _sessions[uid] = sess
        save(uid)
    os.replace(path, f"{path}.imported")
    logger.info("Imported %d session(s) from the JSON file", len(data))
    return len(data)


def load_all() -> int:
    """Read every session back into memory. Raises on DB errors."""
    if not db.enabled():
        return 0
    for row in db.query("SELECT user_id, state_json FROM sessions"):
        _sessions[row["user_id"]] = json.loads(row["state_json"])
    return len(_sessions)


def reset() -> None:  # tests (memory only)
    _sessions.clear()


def forget(user_id: str) -> None:
    _sessions.pop(user_id, None)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def get(user_id: str) -> Optional[dict[str, Any]]:
    return _sessions.get(user_id)


def all_sessions() -> dict[str, dict[str, Any]]:
    return _sessions


def is_open(user_id: str) -> bool:
    s = _sessions.get(user_id)
    return bool(s and s.get("state") == "open")


def touch_user(user_id: str, channel_name: str, reply_to: str, profile_name: Optional[str] = None) -> dict:
    """Inbound message. Opens a new session if there is none or it was closed."""
    s = _sessions.get(user_id)
    t = now()
    if not s or s.get("state") != "open":
        s = {"state": "open", "opened_at": t, "offtopic": 0, "context": False, "quiz": False, "unlocked": []}
        _sessions[user_id] = s
        from . import status
        status.bump("conversations")
    s.update(channel_name=channel_name, reply_to=reply_to, last_activity=t, warned=False,
             boot=BOOT_ID)
    if profile_name:
        s["profile_name"] = profile_name
    save(user_id)
    return s


def touch_bot(user_id: str, *, context: Optional[bool] = None, quiz: Optional[bool] = None,
              lang: Optional[str] = None) -> None:
    """Bot replied. ``context``: the agent now holds history worth apologizing for."""
    s = _sessions.get(user_id)
    if not s or s.get("state") != "open":
        return
    s["last_activity"] = now()
    s["boot"] = BOOT_ID
    if context is not None:
        s["context"] = s.get("context") or context
    if quiz is not None:
        s["quiz"] = quiz
    if lang:
        s["lang"] = lang
    save(user_id)


def set_lang(user_id: str, lang: str) -> None:
    s = _sessions.get(user_id)
    if s and s.get("lang") != lang:
        s["lang"] = lang
        save(user_id)


def bump_offtopic(user_id: str) -> int:
    s = _sessions.get(user_id)
    if not s:
        return 1
    s["offtopic"] = s.get("offtopic", 0) + 1
    save(user_id)
    return s["offtopic"]


def mark_closed(user_id: str, by: str, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
    s = _sessions.get(user_id)
    if not s or s.get("state") != "open":
        return None
    s.update(state="closed", closed_by=by, closed_at=now(), offtopic=0, context=False,
             quiz=False, warned=False, unlocked=[])
    if reason:
        s["closed_reason"] = reason
    save(user_id)
    return s


def mark_warned(user_id: str) -> None:
    s = _sessions.get(user_id)
    if s:
        s["warned"] = True
        s["warned_at"] = now()
        save(user_id)


def idle_actions(warn_after: float, close_after: float, skip: set[str]) -> list[tuple[str, str]]:
    """[(user_id, "warn" | "close")] for open sessions that went quiet.
    ``warn_after`` / ``close_after`` in seconds; ``skip``: users with a human in charge."""
    out = []
    t = now()
    for uid, s in _sessions.items():
        if s.get("state") != "open" or uid in skip or warn_after <= 0:
            continue
        idle = t - s.get("last_activity", t)
        if not s.get("warned"):
            if idle >= warn_after:
                out.append((uid, "warn"))
        elif close_after > 0 and t - s.get("warned_at", t) >= close_after:
            out.append((uid, "close"))
    return out


def lost_by_restart(max_age: float) -> list[str]:
    """Open sessions from a previous process that held context the restart wiped."""
    t = now()
    return [uid for uid, s in _sessions.items()
            if s.get("state") == "open" and s.get("boot") != BOOT_ID
            and (s.get("context") or s.get("quiz"))
            and t - s.get("last_activity", 0) <= max_age]


def needs_restart_notice(user_id: str) -> bool:
    s = _sessions.get(user_id)
    return bool(s and s.get("state") == "open" and s.get("boot") != BOOT_ID
                and (s.get("context") or s.get("quiz")))


def restart_notified(user_id: str) -> None:
    s = _sessions.get(user_id)
    if s:
        s.update(boot=BOOT_ID, context=False, quiz=False)
        save(user_id)


def mark_recovered(user_id: str) -> None:
    """State from a previous process was restored: adopt the session, no apology needed."""
    s = _sessions.get(user_id)
    if s:
        s["boot"] = BOOT_ID
        save(user_id)
