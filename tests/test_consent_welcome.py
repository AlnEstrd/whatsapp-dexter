"""The consent welcome (app/flows/welcome.py, app/leads/consent.py): copy, who gets it, what the first
message's bundle is, the buttons (Meta taps, Twilio 1/2 and labels), tacit answers, opt-out, and the
flags. No network: Twilio, Meta and Slack are mocked, the models are fakes."""
import httpx
import pytest
import respx

from app import db, sessions
from app.analytics import events
from app.config import Settings
from app.flows import menu, welcome
from app.leads import core
from tests.test_channels import META_SEND, SLACK, TWILIO_SEND, tw_msg, twilio_post
from tests.test_close_intent import CLAUDE, USER, _text, bodies, install
from tests.test_flows import meta_payload, meta_sends

OFF_TOPIC = "¿Cuál es la capital de Francia y cuánto mide el Everest?"
CONSENT = {**CLAUDE, "LEADS_ENABLED": "true", "CONSENT_WELCOME_ENABLED": "true",
           "EMAIL_CAPTURE_ENABLED": "false", "USER_MSG_PER_MINUTE": "100"}
VERSION = welcome.CONSENT_TEXT_VERSION


@pytest.fixture
def routed(monkeypatch):
    """Every routed reply bundle, in order (main._route is called once per inbound message)."""
    from app import main
    seen = []
    real = main._route
    monkeypatch.setattr(main, "_route", lambda msg, name: (seen.append(name), real(msg, name)))
    return seen


@pytest.fixture
def wa():
    """Twilio and Slack capture (Meta is in the `meta` fixture)."""
    with respx.mock(assert_all_called=False) as router:
        slack = router.post(SLACK).mock(return_value=httpx.Response(200))
        tw = router.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
        yield type("Wa", (), {"twilio": tw, "slack": slack})


@pytest.fixture
def meta():
    with respx.mock(assert_all_called=False) as router:
        out = router.post(META_SEND).mock(return_value=httpx.Response(200, json={}))
        router.post(SLACK).mock(return_value=httpx.Response(200))
        yield out


def say_meta(c, mid, text=None, reply_id=None, title=""):
    c.post("/webhooks/meta", json=meta_payload(mid, text, reply_id=reply_id, title=title or text or "x"))


def lead(uid=USER):
    return core.by_user(uid)


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #
def test_the_spanish_copy_is_verbatim_with_the_name():
    assert welcome.consent_text("es", "Ana López") == (
        "¡Hola, Ana! Soy Dexter, jefe del laboratorio de Estrada AI Lab. Aquí destilo lo que estamos probando: "
        "la plática, los slides, un diagnóstico de madurez en IA para tu empresa y más.\n\n"
        "Alan podría escribirte después para ver cómo te fue y si te puede ayudar. Si prefieres que no, "
        "toca *No, gracias* o escribe *BAJA* cuando quieras. Aviso de privacidad: alanestrada.space/privacy")


def test_the_english_copy_and_buttons():
    text = welcome.consent_text("en", "Ana")
    assert text.startswith("Hi, Ana! I'm Dexter, lab chief at Estrada AI Lab.")
    assert "tap *No, thanks* or type *STOP* anytime. Privacy notice: alanestrada.space/privacy" in text
    assert welcome.BUTTONS["en"] == ("Sounds good", "No, thanks")
    assert welcome.BUTTONS["es"] == ("Perfecto", "No, gracias")


@pytest.mark.parametrize("profile,greeting", [
    ("Ana López", "¡Hola, Ana!"), ("María", "¡Hola, María!"), ("🙂 Ana", "¡Hola!"), ("Ana2", "¡Hola!"),
    ("Juan Carlos Rodríguez Hernández", "¡Hola!"), (None, "¡Hola!"), ("", "¡Hola!"),
])
def test_the_greeting_uses_a_first_name_only_when_it_looks_like_one(profile, greeting):
    assert welcome.consent_text("es", profile).startswith(greeting)


def test_the_consent_message_has_its_two_buttons_with_the_version_in_the_id():
    out = welcome.consent_out("es", "Ana")
    assert [o.title for o in out.options] == ["Perfecto", "No, gracias"]
    assert [o.id for o in out.options] == [f"consent:yes:{VERSION}", f"consent:no:{VERSION}"]
    assert out.buttons and out.text.endswith("1. Perfecto\n2. No, gracias")


def test_reply_ids_and_typed_answers_are_read_deterministically():
    assert welcome.parse_reply(f"consent:yes:{VERSION}") == (True, VERSION)
    assert welcome.parse_reply("consent:no") == (False, welcome.LEGACY_VERSION)   # old three-moment buttons
    assert welcome.parse_reply("menu:talk") == (None, None)
    assert welcome.typed_choice("Perfecto!", numeric=False) is True
    assert welcome.typed_choice("No, gracias", numeric=False) is False
    assert welcome.typed_choice("Sounds good", numeric=False) is True
    assert welcome.typed_choice("1", numeric=False) is None      # numbers only on channels without buttons
    assert welcome.typed_choice("1", numeric=True) is True
    assert welcome.typed_choice("2", numeric=True) is False
    assert welcome.typed_choice("ok", numeric=True) is None


# --------------------------------------------------------------------------- #
# Who gets it, and the first message's bundle (Twilio: numbered options)
# --------------------------------------------------------------------------- #
def test_a_greeting_as_the_first_message_gets_the_consent_welcome_then_the_menu(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, gl = install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    b = bodies(wa.twilio)
    assert len(b) == 2
    assert b[0].startswith("¡Hola, Alan!") and "Soy Dexter" in b[0] and "1. Perfecto\n2. No, gracias" in b[0]
    assert menu.TEXT["hint"]["es"] in b[1]                       # the menu list follows
    assert llm.calls == [] and gl.calls == [] and routed == ["menu"]
    row = lead()
    assert row["consent_type"] == "tacit_notice" and row["consent_text_version"] == VERSION
    assert row["followup_status"] == "pending" and row["lang"] == "es"


def test_an_english_first_message_gets_the_english_welcome(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hi, what is this?", sid="S1"))
    b = bodies(wa.twilio)
    assert b[0].startswith("Hi, Alan!") and "I'm Dexter" in b[0] and "2. No, thanks" in b[0]
    assert menu.TEXT["hint"]["en"] in b[1]
    assert lead()["lang"] == "en"


def test_an_info_request_as_the_first_message_is_consent_welcome_and_menu_with_no_warning_and_no_model(
        make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, gl = install(lambda *_: _text("nunca debe llegar al modelo"))
    twilio_post(c, tw_msg("Hola, quisiera más información", sid="S1"))
    b = bodies(wa.twilio)
    assert len(b) == 2 and b[0].startswith("¡Hola, Alan!") and menu.TEXT["hint"]["es"] in b[1]
    assert not any("Aviso 1 de" in x for x in b)  # the warning, not the privacy notice
    assert llm.calls == [] and gl.calls == [] and routed == ["menu"]
    assert sessions.get(USER)["offtopic"] == 0


def test_a_real_question_as_the_first_message_is_consent_welcome_and_the_answer(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, _ = install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    b = bodies(wa.twilio)
    assert len(b) == 2 and b[0].startswith("¡Hola") and "BCG" in b[1]
    assert menu.TEXT["hint"]["es"] not in " ".join(b)            # the menu waits for the next reply
    assert routed == ["agent"] and len(llm.calls) == 1


def test_the_menu_follows_the_first_reply_after_a_question_the_welcome_was_ignored_for(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("¿Y en ventas?", sid="S2"))           # typed instead of tapping: tacit, then the menu
    b = bodies(wa.twilio)
    assert menu.TEXT["hint"]["es"] in b[-1] and routed == ["agent", "agent"]
    assert lead()["consent_type"] == "tacit_notice"
    twilio_post(c, tw_msg("¿Y el costo?", sid="S3"))            # the menu is sent once per conversation
    assert sum(menu.TEXT["hint"]["es"] in x for x in bodies(wa.twilio)) == 1


def test_every_inbound_message_gets_exactly_one_routed_bundle_with_the_welcome(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("Respuesta."), guard_says="OUT")
    script = [("Hola", ["menu"]), (OFF_TOPIC, ["offtopic_warning"]), ("gracias, buen día", ["close"]),
              ("hola", ["menu"])]
    for i, (text, expected) in enumerate(script):
        routed.clear()
        twilio_post(c, tw_msg(text, sid=f"S{i}"))
        assert routed == expected, text


def test_a_first_message_the_guard_rejects_gets_the_welcome_and_no_warning(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, gl = install(lambda *_: _text("unused"), guard_says="OUT")
    twilio_post(c, tw_msg(OFF_TOPIC, sid="S1"))
    b = bodies(wa.twilio)
    assert b[0].startswith("¡Hola") and menu.TEXT["hint"]["es"] in b[1]
    assert not any("Aviso 1 de" in x for x in b)  # the warning, not the privacy notice
    assert routed == ["menu"] and llm.calls == [] and len(gl.calls) == 1   # the guard still ran
    assert sessions.get(USER)["offtopic"] == 0


def test_a_returning_user_with_the_current_version_gets_the_plain_welcome(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))                    # consent welcome + menu
    twilio_post(c, tw_msg("1", sid="S2"))                       # Perfecto: explicit, the menu was already out
    twilio_post(c, tw_msg("gracias, buen día", sid="S3"))       # closes the chat
    n = len(bodies(wa.twilio))
    twilio_post(c, tw_msg("hola", sid="S4"))                    # a new conversation
    later = bodies(wa.twilio)[n:]
    assert len(later) == 1 and "Estrada AI Lab" in later[0] and "Perfecto" not in later[0]
    assert lead()["consent_type"] == "explicit"


def test_a_changed_copy_version_shows_the_welcome_again_in_the_next_conversation(make_client, wa, monkeypatch):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    twilio_post(c, tw_msg("gracias, buen día", sid="S2"))
    monkeypatch.setattr(welcome, "CONSENT_TEXT_VERSION", "2026-12-01")
    n = len(bodies(wa.twilio))
    twilio_post(c, tw_msg("hola", sid="S3"))
    again = bodies(wa.twilio)[n:]
    assert again[0].startswith("¡Hola, Alan!") and "1. Perfecto" in again[0]
    assert lead()["consent_text_version"] == "2026-12-01"


def test_the_flag_off_keeps_the_plain_welcome_and_writes_no_consent_fields(make_client, wa):
    c = make_client(**{**CONSENT, "CONSENT_WELCOME_ENABLED": "false"})
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    b = bodies(wa.twilio)
    assert len(b) == 1 and "Estrada AI Lab" in b[0] and "Perfecto" not in b[0]
    row = lead()
    assert row is None or (row["consent_type"] is None and row["followup_status"] is None)


def test_the_settings_default_to_on():
    s = Settings(_env_file=None)
    assert s.consent_welcome_enabled is True and s.email_capture_enabled is True


# --------------------------------------------------------------------------- #
# Buttons: Meta taps (reply ids), Twilio 1/2 and labels
# --------------------------------------------------------------------------- #
def test_meta_sends_the_welcome_as_reply_buttons_then_the_menu_and_a_tap_is_recorded_without_a_model(
        make_client, meta, routed):
    c = make_client(**{**CONSENT, "FLOWS_ENABLED": "true"})
    llm, gl = install(lambda *_: _text("unused"))
    say_meta(c, "w1", "Hola")
    sends = meta_sends(meta)
    buttons = [s for s in sends if s.get("interactive", {}).get("type") == "button"]
    assert buttons and [b["reply"]["title"] for b in buttons[0]["interactive"]["action"]["buttons"]] == \
        ["Perfecto", "No, gracias"]
    assert sends[-1]["interactive"]["type"] == "list"            # the menu list after the welcome
    say_meta(c, "w2", reply_id=f"consent:yes:{VERSION}", title="Perfecto")
    assert lead()["consent_type"] == "explicit" and lead()["followup_status"] == "pending"
    assert llm.calls == [] and gl.calls == []                    # zero model calls for the tap
    assert routed == ["menu", "consent"]
    assert len(meta_sends(meta)) == len(sends) + 1               # the menu was out already: just the ack


def test_a_no_tap_after_a_question_gets_the_confirmation_and_then_the_menu(make_client, meta, routed):
    c = make_client(**{**CONSENT, "FLOWS_ENABLED": "true"})
    llm, gl = install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    say_meta(c, "w1", "¿Qué es la regla 10-20-70?")
    n = len(meta_sends(meta))
    say_meta(c, "w2", reply_id=f"consent:no:{VERSION}", title="No, gracias")
    sent = meta_sends(meta)[n:]
    texts = [s["text"]["body"] for s in sent if s["type"] == "text"]
    assert texts == ["Entendido, no te escribiremos después. Sigue explorando cuando quieras 🙂"]
    assert sent[-1]["interactive"]["type"] == "list"             # then the menu, once
    assert lead()["consent_type"] == "declined" and lead()["followup_status"] == "not_needed"
    assert len(llm.calls) == 1 and len(gl.calls) == 1           # the question: one guard check, one answer


def test_a_late_tap_on_an_old_welcome_is_still_recorded(make_client, meta, routed):
    c = make_client(**{**CONSENT, "FLOWS_ENABLED": "true"})
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    say_meta(c, "w1", "Hola")                                    # the welcome with its buttons
    say_meta(c, "w2", "¿Qué es la regla 10-20-70?")              # typed instead: tacit
    assert lead()["consent_type"] == "tacit_notice"
    say_meta(c, "w3", reply_id=f"consent:no:{VERSION}", title="No, gracias")   # the old button, later
    assert lead()["consent_type"] == "declined" and lead()["followup_status"] == "not_needed"


def test_twilio_1_and_2_answer_the_welcome_right_after_it(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("2", sid="S2"))                        # No, gracias (the numbered button)
    assert lead()["consent_type"] == "declined"
    assert "Entendido, no te escribiremos después" in bodies(wa.twilio)[-1]
    assert routed == ["menu", "consent"]


def test_twilio_label_text_answers_the_welcome_too(make_client, wa):
    c = make_client(**CONSENT)
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("Perfecto", sid="S2"))                  # the label, typed on the second message
    assert lead()["consent_type"] == "explicit"


def test_a_number_after_the_welcome_has_been_ignored_is_the_menu_again(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("¿Y en ventas?", sid="S2"))            # ignored the buttons: tacit
    routed.clear()
    twilio_post(c, tw_msg("1", sid="S3"))                        # now "1" is the talk menu, not Perfecto
    assert routed == ["menu"] and lead()["consent_type"] == "tacit_notice"


def test_a_tap_after_opting_out_does_not_lift_it(make_client, meta, routed):
    c = make_client(**{**CONSENT, "FLOWS_ENABLED": "true"})
    install(lambda *_: _text("unused"))
    say_meta(c, "w1", "Hola")
    say_meta(c, "w2", "BAJA")
    say_meta(c, "w3", reply_id=f"consent:yes:{VERSION}", title="Perfecto")
    row = lead()
    assert row["consent_type"] == "opted_out" and row["opted_out_at"] and row["followup_status"] == "not_needed"


# --------------------------------------------------------------------------- #
# Tacit notice
# --------------------------------------------------------------------------- #
def test_typing_instead_of_tapping_is_a_tacit_notice_and_the_buttons_are_not_shown_again(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S1"))
    twilio_post(c, tw_msg("¿Y en ventas?", sid="S2"))
    row = lead()
    assert row["consent_type"] == "tacit_notice" and row["followup_status"] == "pending"
    assert sum("Perfecto" in x for x in bodies(wa.twilio)) == 1  # shown once, in the first bundle
    assert routed == ["agent", "agent"]


# --------------------------------------------------------------------------- #
# Opt-out (BAJA / STOP): zero tokens, any mode, one confirmation, Slack note
# --------------------------------------------------------------------------- #
def test_baja_in_the_bot_records_the_opt_out_confirms_once_and_notes_slack(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, gl = install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    n = len(bodies(wa.twilio))
    twilio_post(c, tw_msg("BAJA", sid="S2"))
    assert bodies(wa.twilio)[n:] == ["Listo, te di de baja. No te volveremos a escribir."]
    assert llm.calls == [] and gl.calls == []
    row = lead()
    assert row["consent_type"] == "opted_out" and row["opted_out_at"] and row["followup_status"] == "not_needed"
    assert any("Opt-out" in c.request.content.decode() for c in wa.slack.calls)


def test_stop_in_the_quiz_opts_out_and_ends_the_quiz_with_no_model(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, gl = install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("diagnóstico", sid="S1"))
    twilio_post(c, tw_msg("STOP", sid="S2"))
    assert lead()["consent_type"] == "opted_out"
    from app.flows import quiz
    assert not quiz.active(USER)
    assert llm.calls == [] and gl.calls == []


def test_baja_in_human_mode_also_warns_alan_in_the_thread(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    db.conn()
    from app import store
    store.start_handoff(USER)
    twilio_post(c, tw_msg("STOP", sid="S2"))
    assert lead()["consent_type"] == "opted_out"
    assert any("modo humano" in c.request.content.decode() for c in wa.slack.calls)


def test_the_user_can_keep_chatting_after_opting_out(make_client, wa, routed):
    c = make_client(**CONSENT)
    llm, _ = install(lambda *_: _text("La regla 10-20-70 viene de BCG."))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    twilio_post(c, tw_msg("BAJA", sid="S2"))
    routed.clear()
    twilio_post(c, tw_msg("¿Qué es la regla 10-20-70?", sid="S3"))
    assert routed == ["agent"] and len(llm.calls) == 1
    assert lead()["consent_type"] == "opted_out"


def test_no_consent_welcome_for_an_opted_out_user_who_comes_back(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("BAJA", sid="S1"))
    n = len(bodies(wa.twilio))
    twilio_post(c, tw_msg("Hola", sid="S2"))
    back = bodies(wa.twilio)[n:]
    assert len(back) == 1 and "Perfecto" not in back[0]


# --------------------------------------------------------------------------- #
# Events never carry consent text or contact details
# --------------------------------------------------------------------------- #
def test_consent_events_are_recorded_and_carry_no_contact_data(make_client, wa, routed):
    c = make_client(**CONSENT)
    install(lambda *_: _text("unused"))
    twilio_post(c, tw_msg("Hola", sid="S1"))
    twilio_post(c, tw_msg("1", sid="S2"))
    events.drain()
    types = [r["type"] for r in db.query("SELECT type FROM events")]
    assert "consent_shown" in types and "consent_choice" in types
    choice = db.query("SELECT data_json FROM events WHERE type = 'consent_choice'")[0]["data_json"]
    assert '"choice":"explicit"' in choice
