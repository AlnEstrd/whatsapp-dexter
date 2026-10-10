"""Conversation lifecycle: open/close, idle warning, restart recovery, off-topic count.

Agent history and quiz progress live in process memory, so a restart (deploy,
crash, ``--reload``) wipes them. This module keeps a small per-user record on
disk (SESSIONS_FILE) so that after a restart we can tell recent users their
chat was lost, instead of the bot silently "forgetting" them.

A session is *open* from the user's first message until it is closed by:
    user   - says goodbye (adiós, bye, terminar, ...)
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

logger = logging.getLogger("sessions")

BOOT_ID = uuid.uuid4().hex[:12]   # changes on every process start

_sessions: dict[str, dict[str, Any]] = {}
_path: str = ""

CLOSE_WORDS = {
    "adios", "adiós", "bye", "goodbye", "good bye", "chao", "chau", "hasta luego", "nos vemos",
    "terminar", "cerrar", "cerrar chat", "fin", "finalizar", "salir", "exit", "end", "quit",
    "gracias adios", "gracias adiós", "gracias bye", "thanks bye", "ok gracias adios",
}

TEXT = {
    "close_user": {
        "es": "¡Gracias por platicar! 👋 Cerré la conversación. Cuando quieras, escribe *hola* para empezar de nuevo.",
        "en": "Thanks for chatting! 👋 I've closed the conversation. Type *hi* anytime to start again.",
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


def is_close_request(msg_text: str) -> bool:
    t = re.sub(r"[!¡.?¿,]", "", (msg_text or "").strip().lower())
    t = re.sub(r"\s+", " ", t)
    return t in CLOSE_WORDS


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def load(path: str) -> int:
    """Load the registry written by a previous process. Returns sessions loaded."""
    global _path
    _path = path
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            _sessions.update(json.load(fh))
        return len(_sessions)
    except Exception:  # noqa: BLE001
        logger.exception("Could not load sessions from %s", path)
        return 0


def save() -> None:
    if not _path:
        return
    try:
        d = os.path.dirname(_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_sessions, fh)
        os.replace(tmp, _path)
    except Exception:  # noqa: BLE001
        logger.exception("Could not save sessions")


def reset() -> None:  # tests
    global _path
    _sessions.clear()
    _path = ""


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
        s = {"state": "open", "opened_at": t, "offtopic": 0, "context": False, "quiz": False}
        _sessions[user_id] = s
    s.update(channel_name=channel_name, reply_to=reply_to, last_activity=t, warned=False,
             boot=BOOT_ID)
    if profile_name:
        s["profile_name"] = profile_name
    save()
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
    save()


def set_lang(user_id: str, lang: str) -> None:
    s = _sessions.get(user_id)
    if s and s.get("lang") != lang:
        s["lang"] = lang
        save()


def bump_offtopic(user_id: str) -> int:
    s = _sessions.get(user_id)
    if not s:
        return 1
    s["offtopic"] = s.get("offtopic", 0) + 1
    save()
    return s["offtopic"]


def mark_closed(user_id: str, by: str) -> Optional[dict[str, Any]]:
    s = _sessions.get(user_id)
    if not s or s.get("state") != "open":
        return None
    s.update(state="closed", closed_by=by, closed_at=now(), offtopic=0, context=False,
             quiz=False, warned=False)
    save()
    return s


def mark_warned(user_id: str) -> None:
    s = _sessions.get(user_id)
    if s:
        s["warned"] = True
        s["warned_at"] = now()
        save()


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
        save()
