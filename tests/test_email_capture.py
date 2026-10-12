"""Email capture (app/leads/consent.py): asked once per conversation after the quiz report or in the farewell
of a user-initiated close; a valid address is stored and upgrades consent; one retry for an invalid one;
Omitir/skip works; an address typed after the farewell is captured without reopening the menu; the address
never reaches the logs or the events. Twilio (text) keeps the chat readable; the rules are channel-neutral."""
import json
import logging

from app import db, leads, sessions, store
from app.analytics import events
from app.flows import welcome
from app.leads import core
from tests.test_channels import tw_msg, twilio_post
from tests.test_close_intent import USER, _text, bodies, install
from tests.test_consent_welcome import CONSENT, routed, wa  # noqa: F401  (fixtures, injected by pytest)

EMAIL = {**CONSENT, "EMAIL_CAPTURE_ENABLED": "true"}
ASK = "¿Te lo mando también por correo"
FAREWELL_LINE = "Antes de irte:"
THANKS = "¡Gracias! Lo anoté."
SKIP = "Va, sin problema. Sigo aquí para lo que necesites."
RETRY = "Ese correo no se ve bien"
ADDRESS = "ana.lopez@ejemplo.com"


class Chat:
    """One WhatsApp chat on Twilio: say() sends a message and returns the replies it produced."""

    def __init__(self, client, wa):
        self.client, self.wa, self.n, self.seen = client, wa, 0, 0

    def say(self, text):
        self.n += 1
        twilio_post(self.client, tw_msg(text, sid=f"EM{self.n}"))
        new = bodies(self.wa.twilio)[self.seen:]
        self.seen += len(new)
        return new


def finish_quiz(chat):
    """Start the quiz, answer all nine questions, skip the three profile questions: the last reply is the
    email ask (the report itself is not sent on Twilio)."""
    chat.say("diagnóstico")
    for _ in range(9):
        chat.say("3")
    chat.say("omitir")   # role
    chat.say("omitir")   # company size
    return chat.say("omitir")   # name / company: then the email ask


def test_the_email_ask_comes_after_the_report_once_per_conversation(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    last = finish_quiz(chat)
    assert len(last) == 1 and last[0].startswith(ASK) and last[0].endswith("u *Omitir*.")
    assert not any(ASK in x for x in chat.say("¿Cuál es el nivel L1?"))  # not asked again in this chat
    assert core.by_user(USER)["email"] is None


def test_no_email_ask_during_the_quiz_questions(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    chat.say("diagnóstico")
    for _ in range(9):
        assert not any(ASK in x for x in chat.say("3"))


def test_a_valid_email_is_stored_consent_becomes_explicit_and_slack_gets_a_silent_note(
        make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    finish_quiz(chat)
    assert chat.say(ADDRESS) == [THANKS]                         # the thanks only: no menu
    row = core.by_user(USER)
    assert row["email"] == ADDRESS and row["consent_type"] == "explicit" and row["email_consent_ts"]
    assert row["email_text_version"] == welcome.CONSENT_TEXT_VERSION
    texts = [json.loads(x.request.content)["text"] for x in wa.slack.calls]
    assert any(t.endswith("📧 Email captured") for t in texts)   # the note, and never the address
    assert not any(ADDRESS in t for t in texts)


def test_an_invalid_email_gets_one_retry_then_is_dropped_silently(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    finish_quiz(chat)
    assert RETRY in chat.say("ana@gmail")[0]                     # one retry
    assert chat.say("ana@@ejemplo")[0] == SKIP                   # invalid again: the ask is dropped
    assert core.by_user(USER)["email"] is None
    replies = chat.say("hola")                                   # the next message is an ordinary one
    assert replies and not any(ASK in x or RETRY in x or SKIP in x for x in replies)
    assert routed[-1] == "menu"


def test_omitir_and_skip_are_accepted_and_the_ask_is_not_repeated(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    chat = Chat(c, wa)
    chat.say("Hola")
    finish_quiz(chat)
    assert chat.say("Omitir") == [SKIP]
    assert core.by_user(USER)["email"] is None
    assert not any(ASK in x for x in chat.say("¿Qué es la regla 10-20-70?"))


def test_the_farewell_asks_for_an_email_and_an_address_typed_after_it_is_captured_without_reopening(
        make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    closing = chat.say("gracias, buen día")[0]
    assert "Cerré la conversación" in closing and closing.endswith(
        "Antes de irte: ¿me dejas tu correo para mandarte recursos y avisarte de la próxima plática? (u *Omitir*)")
    assert not sessions.is_open(USER)
    assert chat.say(ADDRESS) == [THANKS]                         # still closed: no menu, no new conversation
    assert not sessions.is_open(USER)
    assert core.by_user(USER)["email"] == ADDRESS


def test_the_farewell_ask_is_skipped_when_the_quiz_already_asked(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    finish_quiz(chat)                                            # the report ask already happened
    closing = chat.say("gracias, buen día")[0]
    assert FAREWELL_LINE not in closing


def test_a_declined_lead_is_never_asked_for_an_email(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    assert chat.say("2") and core.by_user(USER)["consent_type"] == "declined"
    last = finish_quiz(chat)
    assert not any(ASK in x for x in last)
    assert FAREWELL_LINE not in chat.say("gracias, buen día")[0]


def test_an_address_already_stored_is_not_asked_for_again(make_client, wa, routed):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    lead = core.by_user(USER)
    core.update(lead["lead_id"], email="ya@ejemplo.com")
    assert not any(ASK in x for x in finish_quiz(chat))


def test_the_email_flag_off_means_no_email_ask(make_client, wa, routed):  # noqa: F811
    c = make_client(**{**EMAIL, "EMAIL_CAPTURE_ENABLED": "false"})
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    assert not any(ASK in x for x in finish_quiz(chat))
    assert FAREWELL_LINE not in chat.say("gracias, buen día")[0]


def test_the_address_never_reaches_the_logs_or_the_events(make_client, wa, routed, caplog):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    finish_quiz(chat)
    with caplog.at_level(logging.DEBUG):
        chat.say(ADDRESS)
    events.drain()
    assert ADDRESS not in caplog.text
    assert not any(ADDRESS in (r["data_json"] or "") for r in db.query("SELECT data_json FROM events"))
    assert any(r["type"] == "email_captured" for r in db.query("SELECT type FROM events"))
    assert '"email_captured":true' in db.query("SELECT data_json FROM events WHERE type = 'email_captured'")[0][
        "data_json"]


def test_the_agent_volunteering_an_email_stores_it_the_same_way(make_client, wa, routed):  # noqa: F811
    make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    leads.consent.show_welcome(USER, "es", "Ana")
    leads.update_profile(USER, {"email": "Ana@Ejemplo.com", "company": "Acme"})
    row = core.by_user(USER)
    assert row["email"] == "ana@ejemplo.com" and row["consent_type"] == "explicit" and row["email_consent_ts"]
    assert row["company"] == "Acme"


def test_no_email_is_asked_in_human_mode(make_client, wa, routed):  # noqa: F811
    make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    leads.consent.show_welcome(USER, "es", "Ana")
    store.start_handoff(USER)
    assert not leads.consent.email_due(USER)


def test_a_chat_closed_for_more_than_fifteen_minutes_no_longer_takes_an_email(make_client, wa, routed, monkeypatch):  # noqa: F811
    c = make_client(**EMAIL)
    install(lambda *_: _text("unused"))
    chat = Chat(c, wa)
    chat.say("Hola")
    chat.say("gracias, buen día")
    real = sessions.now
    monkeypatch.setattr(sessions, "now", lambda: real() + 16 * 60)
    replies = chat.say(ADDRESS)
    assert replies and THANKS not in replies[0]                  # routed as a new message, not captured
    assert core.by_user(USER)["email"] is None
