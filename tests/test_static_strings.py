"""Lint: every static user-facing string is already WhatsApp text (the sanitizer changes nothing) and every
interactive message fits Meta's limits, in ES and EN. A new string that breaks a rule fails here, not on a phone.

Covered: menu, welcome and consent copy, the quiz (questions, options, results), the lead forms, farewells,
limit and guard notices, the handoff and error copy, and the on-request entries (Respuesta / Contacto).
Placeholders are filled with a sample value first. Leading and trailing spaces are not part of the check:
one fragment (the restart's " (incluido…)") is meant to start with a space.
"""
import pytest

from app import agent, escalation, limits, main, on_request, sessions
from app import format_whatsapp as fw
from app.flows import menu, quiz, welcome

LANGS = ("es", "en")
# Meta's limits (format_whatsapp constants): list rows, reply buttons, bodies.
LIST_ROW_TITLE, LIST_ROW_DESCRIPTION, LIST_BUTTON = fw.LIST_ROW_TITLE, fw.LIST_ROW_DESCRIPTION, fw.LIST_BUTTON
REPLY_BUTTON, INTERACTIVE_BODY = fw.REPLY_BUTTON, fw.INTERACTIVE_BODY


class _Sample(dict):
    def __missing__(self, key):
        return "x"


def _fill(text: str) -> str:
    return text.format_map(_Sample())


def _walk(label: str, obj, out: list) -> None:
    if isinstance(obj, str):
        out.append((label, _fill(obj)))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk(f"{label}.{k}", v, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(f"{label}[{i}]", v, out)


def static_strings() -> list[tuple[str, str]]:
    from app.leads import flows as lf
    out: list[tuple[str, str]] = []
    for name, obj in [
        ("menu.TEXT", menu.TEXT), ("menu.MENU_OPTIONS", menu.MENU_OPTIONS), ("menu.MENU_LINES", menu.MENU_LINES),
        ("welcome.GREET", welcome.GREET), ("welcome.CONSENT_BODY", welcome.CONSENT_BODY),
        ("welcome.BUTTONS", welcome.BUTTONS), ("welcome.COPY", welcome.COPY),
        ("quiz.T", quiz.T), ("quiz.LEVELS", quiz.LEVELS), ("quiz.TIPS", quiz.TIPS), ("quiz.QUESTIONS", quiz.QUESTIONS),
        ("sessions.TEXT", sessions.TEXT), ("sessions._REENGAGE", sessions._REENGAGE),
        ("limits.TEXT", limits.TEXT),
        ("leads.flows.TEXT", lf.TEXT), ("leads.flows.CONSENT_TEXT", lf.CONSENT_TEXT),
        ("leads.flows.CONSENT_BUTTONS", lf.CONSENT_BUTTONS), ("leads.flows.ROLE_OPTS", lf.ROLE_OPTS),
        ("leads.flows.SIZE_OPTS", lf.SIZE_OPTS), ("leads.flows.CHOICE_OPTS", lf.CHOICE_OPTS),
        ("leads.flows.CHANNEL_OPTS", lf.CHANNEL_OPTS), ("leads.flows.TIME_OPTS", lf.TIME_OPTS),
        ("leads.flows.TOPIC_OPTS", lf.TOPIC_OPTS), ("leads.flows.CONFIRM_OPTS", lf.CONFIRM_OPTS),
        ("escalation.HANDOFF_REPLY", escalation.HANDOFF_REPLY), ("agent.OFFER", agent.OFFER),
        ("main.NO_FILES", main.NO_FILES), ("main.DOC_FAILED", main.DOC_FAILED), ("main.NO_RESULT", main.NO_RESULT),
        ("main.UNKNOWN_SLIDE", main.UNKNOWN_SLIDE), ("main.LEAD_HANDOFF_REPLY", main.LEAD_HANDOFF_REPLY),
        ("main.MEDIA_NOTE", main.MEDIA_NOTE), ("main.VOICE_NOTE", main.VOICE_NOTE),
        ("main.CSAT_THANKS", main.CSAT_THANKS), ("main.FALLBACK_REPLY", main.FALLBACK_REPLY),
    ]:
        _walk(name, obj, out)
    # The on-request entries: what a user gets for Respuesta / Contacto (placeholders stubbed).
    import re
    for entry in on_request.load().values():
        for field in ("respuesta", "response_en", "cta", "cta_en"):
            raw = getattr(entry, field)
            if raw:
                out.append((f"on_request.{entry.id}.{field}", re.sub(r"\{\{\s*[a-z_]+\s*\}\}", "x", raw)))
    return out


def interactive_messages() -> list[tuple[str, object]]:
    from app.leads import flows as lf
    msgs: list[tuple[str, object]] = []
    for lang in LANGS:
        msgs.append((f"menu_list.{lang}", menu.menu_list(lang)))
        msgs.append((f"welcome.{lang}", menu.welcome(lang, "Ana")))
        msgs.append((f"consent.{lang}", welcome.consent_out(lang, "Alejandra")))
        msgs.append((f"consent_no_name.{lang}", welcome.consent_out(lang, None)))
        for i in range(len(quiz.QUESTIONS)):
            msgs.append((f"quiz{i}.{lang}", quiz._question(quiz.QuizState(lang=lang, index=i))))
        msgs.append((f"lead_consent.{lang}", lf.consent_out(lang)))
        # As the lead flows send them: these are lists, ...
        for name, opts in (("role", lf.ROLE_OPTS), ("size", lf.SIZE_OPTS), ("choice", lf.CHOICE_OPTS),
                           ("time", lf.TIME_OPTS), ("topic", lf.TOPIC_OPTS)):
            msgs.append((f"{name}.list.{lang}", lf._list_out("Elige una opción.", lang, opts, name)))
        # ... and these are reply buttons (three at most).
        for name, opts in (("channel", lf.CHANNEL_OPTS), ("confirm", lf.CONFIRM_OPTS)):
            msgs.append((f"{name}.buttons.{lang}", lf._buttons_out("Elige una opción.", lang, opts, name)))
    return msgs


STATIC = static_strings()
INTERACTIVE = interactive_messages()


def test_the_lint_covers_a_real_set_of_strings():
    # A guard against an empty walk: the copy is in the dozens, the entries in ES and EN.
    assert len(STATIC) > 150
    assert any(label.startswith("on_request.") for label, _ in STATIC)
    assert len(INTERACTIVE) > 40


@pytest.mark.parametrize("label, text", STATIC, ids=[label for label, _ in STATIC])
def test_static_string_is_already_whatsapp_text(label, text):
    out, counts = fw.to_whatsapp(text)
    assert out == text.strip(), f"{label}: would become {out!r}"
    assert not any(counts.values()), f"{label}: {counts}"


@pytest.mark.parametrize("label, msg", INTERACTIVE, ids=[label for label, _ in INTERACTIVE])
def test_interactive_message_fits_meta_limits(label, msg):
    options = msg.options or []
    if not options:
        return
    body = msg.interactive_text or msg.text
    assert len(body) <= INTERACTIVE_BODY, f"{label}: body {len(body)} > {INTERACTIVE_BODY}"
    assert fw.to_whatsapp(body)[0] == body, f"{label}: body is not WhatsApp text"
    if msg.buttons:
        assert len(options) <= fw.REPLY_MAX_BUTTONS, f"{label}: {len(options)} buttons"
        assert all(len(o.title) <= REPLY_BUTTON for o in options), f"{label}: a reply button title is too long"
    else:
        assert len(options) <= fw.LIST_MAX_ROWS, f"{label}: {len(options)} rows"
        assert all(len(o.title) <= LIST_ROW_TITLE for o in options), \
            f"{label}: a row title is over {LIST_ROW_TITLE}: {[o.title for o in options if len(o.title) > LIST_ROW_TITLE]}"
        assert all(len(o.description) <= LIST_ROW_DESCRIPTION for o in options), f"{label}: a row description is too long"
        assert len(msg.button) <= LIST_BUTTON, f"{label}: list button {msg.button!r} is over {LIST_BUTTON}"
    assert all(len(o.id) <= 200 for o in options), f"{label}: an id is over 200"
