"""Deterministic WhatsApp steps for leads: consent question, post-quiz profiling and the
"Que Alan me contacte" form. Zero model tokens, except one optional Haiku call to split a
free-text "name and company" answer.

State lives in the session record (``sessions[uid]["lead_flow"]``), so closing the chat (or
idling out) drops a half-filled form and saves nothing. Functions return :class:`FlowResult`;
main.py sends the messages and performs the follow-up actions.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import sessions, store
from ..config import get_settings
from ..flows import quiz
from ..flows.types import Option, Outgoing
from . import core

logger = logging.getLogger("leads.flows")

# --------------------------------------------------------------------------- #
# Consent text: the one place it lives. Bump the version whenever the wording changes.
# --------------------------------------------------------------------------- #
CONSENT_TEXT_VERSION = "2026-10-v1"
CONTACT_FORM_VERSION = "wa-form-2026-10-v1"   # the form's confirmation is the explicit consent
WEB_FORM_VERSION = "web-form-2026-10-v1"
PRIVACY_URL = "https://alanestrada.space/privacy"

CONSENT_TEXT = {
    "es": ("¿Te podemos escribir después de la plática para mandarte recursos y ver si Alan te puede "
           "ayudar con IA en tu empresa? Te escribiría Dexter o Alan directamente, por WhatsApp o correo. "
           "Puedes darte de baja cuando quieras escribiendo *BAJA*. Aviso de privacidad: " + PRIVACY_URL),
    "en": ("Can we write to you after the talk to send you resources and see whether Alan can help you "
           "with AI at your company? Dexter or Alan himself would write, by WhatsApp or email. You can "
           "unsubscribe anytime by typing *STOP*. Privacy notice: " + PRIVACY_URL),
}
CONSENT_BUTTONS = {"es": ("Sí, escríbeme", "No, gracias"), "en": ("Yes, write to me", "No, thanks")}


def consent_version() -> str:
    return get_settings().consent_text_version or CONSENT_TEXT_VERSION


TEXT = {
    "consent_yes": {"es": "¡Gracias! Anotado. Si cambias de opinión, escribe *BAJA* cuando quieras.",
                    "en": "Thanks! Noted. If you change your mind, type *STOP* anytime."},
    "consent_no": {"es": "Va, sin problema. Sigo aquí para lo que necesites.",
                   "en": "No problem. I'm still here for anything you need."},
    "opted_out": {"es": "Listo, ya no te escribiremos para seguimiento. Puedes seguir usando a Dexter cuando quieras.",
                  "en": "Done, we won't write to you for follow-up anymore. You can keep using Dexter anytime."},
    "role_q": {"es": "Una más, opcional: ¿cuál es tu rol?", "en": "One more, optional: what's your role?"},
    "size_q": {"es": "¿Y cuántas personas trabajan en tu empresa?", "en": "And how many people work at your company?"},
    "name_q": {"es": "¿Pongo tu nombre y empresa en el reporte? Escríbelos en un mensaje (p. ej. *Ana López, Acme*) o toca *Omitir*.",
               "en": "Shall I put your name and company on the report? Type them in one message (e.g. *Ana López, Acme*) or tap *Skip*."},
    "report_named": {"es": "Listo, aquí va tu reporte con tu nombre.", "en": "Done, here's your report with your name."},
    "choose": {"es": "¿Cómo prefieres?", "en": "How would you like it?"},
    "c_channel": {"es": "📇 Va, Alan te contacta después. ¿Por dónde prefieres?",
                  "en": "📇 Sure, Alan will contact you later. How do you prefer?"},
    "c_time": {"es": "¿A qué hora te queda mejor?", "en": "What time works best?"},
    "c_topic": {"es": "¿De qué te gustaría hablar?", "en": "What would you like to talk about?"},
    "c_name": {"es": "¿Con quién hablaría Alan? Escribe tu nombre y empresa en un mensaje (p. ej. *Ana López, Acme*) o toca *Omitir*.",
               "en": "Who would Alan talk to? Type your name and company in one message (e.g. *Ana López, Acme*) or tap *Skip*."},
    "c_email": {"es": "¿A qué correo te escribe Alan?", "en": "Which email should Alan write to?"},
    "c_email_retry": {"es": "Ese correo no parece válido. ¿Me lo escribes de nuevo?",
                      "en": "That email doesn't look valid. Could you type it again?"},
    "c_email_fallback": {"es": "No pude validar el correo; Alan te contactará por WhatsApp.",
                         "en": "I couldn't validate the email; Alan will contact you on WhatsApp."},
    "c_confirm": {"es": "📇 *Tu solicitud*\n{summary}\n\nAl confirmar, Alan te contactará por {channel}. Si cambias de opinión, escribe *BAJA*.",
                  "en": "📇 *Your request*\n{summary}\n\nWhen you confirm, Alan will contact you by {channel}. If you change your mind, type *STOP*."},
    "c_done": {"es": "¡Listo! Alan te contactará por {channel}{when}. Mientras, puedes seguir preguntándome lo que quieras.",
               "en": "Done! Alan will contact you by {channel}{when}. Meanwhile, keep asking me anything."},
    "c_invalid": {"es": "Elige una opción de la lista (o escribe *cancelar*).",
                  "en": "Pick an option from the list (or type *cancel*)."},
    "c_cancelled": {"es": "Listo, cancelé la solicitud; no guardé nada. Escribe *menú* para ver las opciones.",
                    "en": "OK, I cancelled the request and saved nothing. Type *menu* to see the options."},
    "button": {"es": "Elegir", "en": "Choose"},
}

ROLE_OPTS = {
    "es": [("owner_exec", "Dueño(a) / Dir. general", "Dueño(a) o dirección general"),
           ("director", "Dirección de área", ""), ("manager", "Gerencia", ""), ("individual", "Otro", ""),
           ("skip", "Omitir", "")],
    "en": [("owner_exec", "Owner / CEO", "Owner or general management"),
           ("director", "Department head", ""), ("manager", "Manager", ""), ("individual", "Other", ""),
           ("skip", "Skip", "")],
}
SIZE_OPTS = {
    "es": [("1-10", "1–10", ""), ("11-50", "11–50", ""), ("51-250", "51–250", ""), ("251+", "Más de 250", ""),
           ("skip", "Omitir", "")],
    "en": [("1-10", "1–10", ""), ("11-50", "11–50", ""), ("51-250", "51–250", ""), ("251+", "More than 250", ""),
           ("skip", "Skip", "")],
}
CHOICE_OPTS = {
    "es": [("now", "Hablar con Alan ahora", "Te responde una persona por este chat"),
           ("later", "Que Alan me contacte", "Después, por WhatsApp, llamada o correo")],
    "en": [("now", "Talk to Alan now", "A person replies in this chat"),
           ("later", "Have Alan contact me", "Later, by WhatsApp, phone or email")],
}
CHANNEL_OPTS = {
    "es": [("whatsapp", "WhatsApp", ""), ("call", "Llamada", ""), ("email", "Correo", "")],
    "en": [("whatsapp", "WhatsApp", ""), ("call", "Phone call", ""), ("email", "Email", "")],
}
TIME_OPTS = {
    "es": [("morning", "Mañana", ""), ("afternoon", "Tarde", ""), ("any", "Cualquier hora", ""), ("skip", "Omitir", "")],
    "en": [("morning", "Morning", ""), ("afternoon", "Afternoon", ""), ("any", "Any time", ""), ("skip", "Skip", "")],
}
TOPIC_OPTS = {
    "es": [("implement", "Implementar IA", "Implementar IA en mi empresa"),
           ("governance", "Gobernanza de IA", ""), ("talk", "Sobre la plática", ""), ("other", "Otro", ""),
           ("skip", "Omitir", "")],
    "en": [("implement", "Implementing AI", "Implementing AI at my company"),
           ("governance", "AI governance", ""), ("talk", "About the talk", ""), ("other", "Other", ""),
           ("skip", "Skip", "")],
}
SKIP_OPT = {"es": ("skip", "Omitir", ""), "en": ("skip", "Skip", "")}
CONFIRM_OPTS = {"es": [("yes", "Confirmar", ""), ("fix", "Corregir", "")],
                "en": [("yes", "Confirm", ""), ("fix", "Edit", "")]}

SIZE_MIDPOINT = {"1-10": 5, "11-50": 30, "51-250": 150, "251+": 300}


def label(opts: dict, key: Optional[str], lang: str) -> str:
    for k, t, _ in opts[lang]:
        if k == key:
            return t
    return "—"


# --------------------------------------------------------------------------- #
# Text normalisation and keywords
# --------------------------------------------------------------------------- #
def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s@.+-]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .")


OPT_OUT = {"baja", "stop", "unsubscribe", "no me escriban", "no me contacten", "darme de baja",
           "dar de baja", "dame de baja"}
YES = {"si", "yes", "claro", "si escribeme", "escribeme", "1", "ok", "va", "dale", "por supuesto", "sure",
       "yes please", "si claro", "claro que si", "adelante", "si por favor", "yes write to me", "sip", "si gracias"}
NO = {"no", "2", "no gracias", "no thanks", "no thank you", "nel", "mejor no", "no por ahora", "not now"}
SKIP = {"omitir", "skip", "saltar", "paso", "prefiero no", "ninguno"}
CANCEL = {"cancelar", "cancel", "menu", "salir del formulario"}
CONTACT_KEYWORDS = {"contactenme", "contactame", "que me contacten", "que me contacte alan", "que me llame",
                    "que me llamen", "que alan me llame", "que alan me contacte", "que alan me contacte despues",
                    "call me", "contact me", "please contact me", "have alan contact me"}


def is_opt_out(text: str) -> bool:
    return norm(text) in OPT_OUT


def is_contact_request(text: str) -> bool:
    return norm(text) in CONTACT_KEYWORDS


def parse_yes_no(text: str, reply_id: Optional[str]) -> Optional[bool]:
    if reply_id in ("consent:yes", "consent:no"):
        return reply_id == "consent:yes"
    t = norm(text)
    if t in YES:
        return True
    if t in NO:
        return False
    return None


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
@dataclass
class FlowResult:
    outs: list[Outgoing] = field(default_factory=list)
    report_name: Optional[str] = None     # regenerate the assessment report for this name
    ask_consent: Optional[str] = None     # moment, when the flow ends and consent may be asked
    contact: Optional[dict] = None        # confirmed contact request
    handoff: bool = False                 # "Hablar con Alan ahora"


def current(uid: str) -> Optional[dict]:
    s = sessions.get(uid)
    return (s or {}).get("lead_flow") if s and s.get("state") == "open" else None


def _set(uid: str, flow: Optional[dict]) -> None:
    s = sessions.get(uid)
    if s is None:
        return
    if flow is None:
        s.pop("lead_flow", None)
    else:
        s["lead_flow"] = flow
    sessions.save(uid)


def clear(uid: str) -> None:
    _set(uid, None)


def _options(lang: str, opts: dict, prefix: str) -> list[Option]:
    return [Option(f"{prefix}:{k}", t[:24], d[:72]) for k, t, d in opts[lang]]


def _numbered(text: str, lang: str, opts: dict) -> str:
    return text + "\n\n" + "\n".join(f"{i}. {t}" for i, (_, t, _) in enumerate(opts[lang], start=1))


def _list_out(text: str, lang: str, opts: dict, prefix: str) -> Outgoing:
    return Outgoing(text=_numbered(text, lang, opts), options=_options(lang, opts, prefix),
                    button=TEXT["button"][lang], interactive_text=text)


def _buttons_out(text: str, lang: str, opts: dict, prefix: str) -> Outgoing:
    return Outgoing(text=_numbered(text, lang, opts), options=_options(lang, opts, prefix),
                    buttons=True, interactive_text=text)


def _pick(text: str, reply_id: Optional[str], lang: str, opts: dict, prefix: str) -> Optional[str]:
    """Option key from a tapped row / button, a typed number or a typed title."""
    if reply_id and reply_id.startswith(prefix + ":"):
        key = reply_id[len(prefix) + 1:]
        return key if any(k == key for k, _, _ in opts[lang]) else None
    t = norm(text)
    keys = [k for k, _, _ in opts[lang]]
    if t.isdigit() and 1 <= int(t) <= len(keys):
        return keys[int(t) - 1]
    for k, title, _ in opts[lang]:
        if t == norm(title):
            return k
    if t in SKIP and "skip" in keys:
        return "skip"
    return None


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #
def consent_out(lang: str) -> Outgoing:
    yes, no = CONSENT_BUTTONS[lang]
    text = CONSENT_TEXT[lang]
    return Outgoing(text=f"{text}\n\n1. {yes}\n2. {no}",
                    options=[Option("consent:yes", yes), Option("consent:no", no)],
                    buttons=True, interactive_text=text)


def ask_consent(uid: str, lang: str, moment: str) -> Optional[Outgoing]:
    """The consent question, if every rule allows it (once per conversation, never mid-quiz,
    never in human mode, max 2 asks, 30 days after a no, never after opt-out)."""
    s = sessions.get(uid)
    if s is None or s.get("state") != "open" or s.get("consent_asked"):
        return None
    if quiz.active(uid) or store.in_handoff(uid):
        return None
    f = current(uid)
    if f and f.get("kind") != "consent":
        return None  # another form is in progress
    lead = core.by_user(uid)
    if not core.consent_askable(lead):
        return None
    core.mark_asked(lead["lead_id"], moment)
    s["consent_asked"] = True
    s["lead_flow"] = {"kind": "consent", "moment": moment, "lang": lang}
    sessions.save(uid)
    return consent_out(lang if lang in ("es", "en") else "es")


def answer_consent(uid: str, text: str, reply_id: Optional[str]) -> tuple[bool, Optional[str]]:
    """(handled, ack text). Not handled -> the message goes on as usual (and the question
    counts as unanswered: it is not asked again in this conversation)."""
    f = current(uid)
    if not f or f.get("kind") != "consent":
        return False, None
    ans = parse_yes_no(text, reply_id)
    clear(uid)
    if ans is None:
        return False, None
    lead = core.by_user(uid)
    if lead is None:
        return False, None
    core.record_consent(lead["lead_id"], ans, consent_version(), "button" if reply_id else "text")
    core.recompute(lead["lead_id"])
    lang = f.get("lang") or "es"
    return True, TEXT["consent_yes" if ans else "consent_no"][lang]


# --------------------------------------------------------------------------- #
# Post-quiz profiling (role, size, name/company), each step skippable
# --------------------------------------------------------------------------- #
def _profile_next(uid: str, f: dict) -> FlowResult:
    lead = core.by_user(uid) or {}
    lang = f["lang"]
    for step in f["todo"]:
        f["todo"] = f["todo"][1:]
        if step == "role" and (lead.get("role_level") or "unknown") == "unknown":
            f["step"] = "role"
            _set(uid, f)
            return FlowResult([_list_out(TEXT["role_q"][lang], lang, ROLE_OPTS, "profile:role")])
        if step == "size" and (lead.get("company_size") or "unknown") == "unknown":
            f["step"] = "size"
            _set(uid, f)
            return FlowResult([_list_out(TEXT["size_q"][lang], lang, SIZE_OPTS, "profile:size")])
        if step == "name" and not (lead.get("name") or lead.get("company")):
            f["step"] = "name"
            _set(uid, f)
            opt = SKIP_OPT[lang]
            return FlowResult([Outgoing(text=TEXT["name_q"][lang], options=[Option(f"profile:name:{opt[0]}", opt[1])],
                                        buttons=True)])
    clear(uid)
    return FlowResult(ask_consent="quiz")


def start_profile(uid: str, lang: str) -> FlowResult:
    """After the quiz result and report: the optional questions still unanswered, then consent."""
    f = {"kind": "profile", "lang": lang if lang in ("es", "en") else "es", "todo": ["role", "size", "name"]}
    return _profile_next(uid, f)


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    return t.endswith("?") or t.startswith("¿") or len(t.split()) > 12


async def _profile_step(uid: str, f: dict, text: str, reply_id: Optional[str]) -> Optional[FlowResult]:
    lang, step = f["lang"], f.get("step")
    lead = core.by_user(uid)
    if lead is None:
        clear(uid)
        return None
    if step in ("role", "size"):
        opts, prefix = (ROLE_OPTS, "profile:role") if step == "role" else (SIZE_OPTS, "profile:size")
        key = _pick(text, reply_id, lang, opts, prefix)
        if key is None:
            clear(uid)  # they moved on: handle their message normally, no more questions
            return None
        if key != "skip":
            core.set_profile(lead["lead_id"], source="quiz", **{"role_level" if step == "role" else "company_size": key})
            core.recompute(lead["lead_id"])
        return _profile_next(uid, f)
    if step == "name":
        if reply_id == "profile:name:skip" or norm(text) in SKIP:
            return _profile_next(uid, f)
        if _looks_like_question(text) or reply_id:
            clear(uid)
            return None
        name, company = await extract_name_company(uid, text)
        if name or company:
            core.set_profile(lead["lead_id"], source="quiz", name=name, company=company)
            core.recompute(lead["lead_id"])
        res = _profile_next(uid, f)
        if name or company:
            res.report_name = report_name(core.by_user(uid))
            res.outs.insert(0, Outgoing(text=TEXT["report_named"][lang]))
        return res
    clear(uid)
    return None


def report_name(lead: Optional[dict]) -> Optional[str]:
    """'Ana López, Acme' for the report's "Preparado para" line (None when neither is known)."""
    if not lead:
        return None
    parts = [p for p in (lead.get("name"), lead.get("company")) if p]
    return ", ".join(parts) or None


# --------------------------------------------------------------------------- #
# Name + company from one free-text line
# --------------------------------------------------------------------------- #
_LEAD_IN = re.compile(r"^(?:hola[, ]+)?(?:soy|me llamo|mi nombre es|i'?m|i am|my name is|this is)\s+", re.I)
_SPLIT_STRONG = re.compile(r"\s*(?:,|;| - | – | / )\s*")
_SPLIT_WORD = re.compile(r"\s+(?:de la empresa|from|at|de|en)\s+", re.I)


def parse_name_company(text: str) -> tuple[Optional[str], Optional[str]]:
    t = re.sub(r"\s+", " ", (text or "").strip().strip(".!"))
    t = _LEAD_IN.sub("", t)
    if not t or len(t) > 120:
        return None, None
    parts = _SPLIT_STRONG.split(t, maxsplit=1)
    if len(parts) == 1:
        parts = _SPLIT_WORD.split(t, maxsplit=1)
    name = parts[0].strip() or None
    company = parts[1].strip() if len(parts) > 1 else None
    if company:
        company = re.sub(r"^(?:de|en|la empresa|empresa|company)\s+", "", company, flags=re.I).strip() or None
    return (name[:80] if name else None), (company[:80] if company else None)


EXTRACT_PROMPT = ("Extract the person's name and their company from this WhatsApp message. Reply with "
                  'JSON only: {"name": string|null, "company": string|null}. Never invent values.')


async def extract_name_company(uid: str, text: str) -> tuple[Optional[str], Optional[str]]:
    """Deterministic split when the line has a separator; otherwise one small Haiku call
    (recorded in the usage table), falling back to the deterministic parse."""
    name, company = parse_name_company(text)
    s = get_settings()
    if company or not (s.anthropic_api_key and s.agent_mode != "echo"):
        return name, company
    try:
        from anthropic import AsyncAnthropic

        from .. import limits
        client = AsyncAnthropic(api_key=s.anthropic_api_key)
        resp = await client.messages.create(model=s.guard_model, max_tokens=60, system=EXTRACT_PROMPT,
                                            messages=[{"role": "user", "content": text[:300]}])
        await limits.record(uid, resp, "lead_extract")
        raw = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        n = core.clean_text(data.get("name"), 80)
        c = core.clean_text(data.get("company"), 80)
        return (n, c) if (n or c) else (name, company)
    except Exception:  # noqa: BLE001
        logger.warning("name/company extraction failed; using the deterministic parse")
        return name, company


# --------------------------------------------------------------------------- #
# "Que Alan me contacte" form
# --------------------------------------------------------------------------- #
def contact_choice(uid: str, lang: str) -> Outgoing:
    """Menu option 4: talk now (handoff) or be contacted later (the form)."""
    _set(uid, {"kind": "choice", "lang": lang})
    return _list_out(TEXT["choose"][lang], lang, CHOICE_OPTS, "contact:choice")


def start_contact(uid: str, lang: str) -> FlowResult:
    lang = lang if lang in ("es", "en") else "es"
    f = {"kind": "contact", "lang": lang, "step": "channel", "data": {}, "email_tries": 0}
    _set(uid, f)
    return FlowResult([_buttons_out(TEXT["c_channel"][lang], lang, CHANNEL_OPTS, "contact:ch")])


def _contact_prompt(f: dict, lead: dict) -> Optional[Outgoing]:
    lang, step = f["lang"], f["step"]
    if step == "time":
        return _list_out(TEXT["c_time"][lang], lang, TIME_OPTS, "contact:time")
    if step == "topic":
        return _list_out(TEXT["c_topic"][lang], lang, TOPIC_OPTS, "contact:topic")
    if step == "name":
        opt = SKIP_OPT[lang]
        return Outgoing(text=TEXT["c_name"][lang], options=[Option(f"contact:name:{opt[0]}", opt[1])], buttons=True)
    if step == "email":
        return Outgoing(text=TEXT["c_email"][lang])
    if step == "confirm":
        d = f["data"]
        who = report_name(lead) or lead.get("wa_profile_name") or "—"
        lines = [f"• {'Canal' if lang == 'es' else 'Channel'}: {label(CHANNEL_OPTS, d.get('channel'), lang)}",
                 f"• {'Horario' if lang == 'es' else 'Time'}: {label(TIME_OPTS, d.get('best_time'), lang)}",
                 f"• {'Tema' if lang == 'es' else 'Topic'}: {label(TOPIC_OPTS, d.get('topic'), lang)}",
                 f"• {'Nombre' if lang == 'es' else 'Name'}: {who}"]
        if d.get("channel") == "email":
            lines.append(f"• Email: {d.get('email') or lead.get('email')}")
        text = TEXT["c_confirm"][lang].format(summary="\n".join(lines),
                                             channel=label(CHANNEL_OPTS, d.get("channel"), lang))
        return _buttons_out(text, lang, CONFIRM_OPTS, "contact:confirm")
    return None


def _advance(f: dict, lead: dict) -> None:
    order = ["channel", "time", "topic", "name", "email", "confirm"]
    i = order.index(f["step"]) + 1
    while i < len(order):
        step = order[i]
        if step == "name" and (lead.get("name") and lead.get("company")):
            i += 1
            continue
        if step == "email" and (f["data"].get("channel") != "email" or lead.get("email")):
            i += 1
            continue
        break
    f["step"] = order[i]


async def _contact_step(uid: str, f: dict, text: str, reply_id: Optional[str]) -> Optional[FlowResult]:
    lang, step = f["lang"], f["step"]
    lead = core.by_user(uid)
    if lead is None:
        clear(uid)
        return None
    if norm(text) in CANCEL and not reply_id:
        clear(uid)
        return FlowResult([Outgoing(text=TEXT["c_cancelled"][lang])])
    d = f["data"]
    invalid = FlowResult([Outgoing(text=TEXT["c_invalid"][lang])])
    if step == "channel":
        key = _pick(text, reply_id, lang, CHANNEL_OPTS, "contact:ch")
        if key is None:
            invalid.outs.append(_buttons_out(TEXT["c_channel"][lang], lang, CHANNEL_OPTS, "contact:ch"))
            return invalid
        d["channel"] = key
    elif step in ("time", "topic"):
        opts, prefix = (TIME_OPTS, "contact:time") if step == "time" else (TOPIC_OPTS, "contact:topic")
        key = _pick(text, reply_id, lang, opts, prefix)
        if key is None:
            invalid.outs.append(_contact_prompt(f, lead))
            return invalid
        d["best_time" if step == "time" else "topic"] = None if key == "skip" else key
    elif step == "name":
        if not (reply_id == "contact:name:skip" or norm(text) in SKIP):
            name, company = await extract_name_company(uid, text)
            core.set_profile(lead["lead_id"], source="contact_form", name=name, company=company)
            lead = core.by_user(uid)
    elif step == "email":
        email = (text or "").strip().lower()
        if core.valid_email(email) and not ((o := core.by_email(email)) and o["lead_id"] != lead["lead_id"]):
            d["email"] = email
        else:
            f["email_tries"] = int(f.get("email_tries") or 0) + 1
            if f["email_tries"] < 2:
                _set(uid, f)
                return FlowResult([Outgoing(text=TEXT["c_email_retry"][lang])])
            d["channel"] = "whatsapp"
            f["step"] = "email"
            _advance(f, lead)
            _set(uid, f)
            return FlowResult([Outgoing(text=TEXT["c_email_fallback"][lang]), _contact_prompt(f, lead)])
    elif step == "confirm":
        key = _pick(text, reply_id, lang, CONFIRM_OPTS, "contact:confirm")
        if key == "fix":
            return start_contact(uid, lang)
        if key != "yes":
            invalid.outs.append(_contact_prompt(f, lead))
            return invalid
        clear(uid)
        if d.get("email"):
            core.set_profile(lead["lead_id"], source="contact_form", email=d["email"])
        when = {"morning": {"es": " por la mañana", "en": " in the morning"},
                "afternoon": {"es": " por la tarde", "en": " in the afternoon"}}.get(d.get("best_time") or "", {}).get(lang, "")
        done = TEXT["c_done"][lang].format(channel=label(CHANNEL_OPTS, d.get("channel"), lang), when=when)
        return FlowResult([Outgoing(text=done)], contact=dict(d))
    _advance(f, lead)
    _set(uid, f)
    return FlowResult([_contact_prompt(f, lead)])


async def step(uid: str, text: str, reply_id: Optional[str]) -> Optional[FlowResult]:
    """Feed a message to the form in progress. None = not ours (route it as usual)."""
    f = current(uid)
    if not f:
        return None
    kind = f.get("kind")
    if kind == "profile":
        return await _profile_step(uid, f, text, reply_id)
    if kind == "contact":
        return await _contact_step(uid, f, text, reply_id)
    if kind == "choice":
        key = _pick(text, reply_id, f["lang"], CHOICE_OPTS, "contact:choice")
        clear(uid)
        if key == "now":
            return FlowResult(handoff=True)
        if key == "later":
            return start_contact(uid, f["lang"])
        return None
    return None  # "consent" is answered by answer_consent before routing


def contact_summary(d: dict[str, Any]) -> str:
    return " · ".join(x for x in (label(CHANNEL_OPTS, d.get("channel"), "es"),
                                   label(TIME_OPTS, d.get("best_time"), "es") if d.get("best_time") else "",
                                   label(TOPIC_OPTS, d.get("topic"), "es") if d.get("topic") else "") if x)
