"""Markdown -> WhatsApp sanitizer, Dexter's voice document and the prompt wiring."""
import pytest

from app import agent, knowledge, sessions
from app.flows import menu, quiz
from app.formatting import to_whatsapp


@pytest.mark.parametrize("raw, expected", [
    ("**Automatizar** no es rediseñar", "*Automatizar* no es rediseñar"),
    ("__clave__ y **otra**", "*clave* y *otra*"),
    ("***fuerte***", "*fuerte*"),
    ("~~viejo~~", "~viejo~"),
    ("# Título\ntexto", "*Título*\ntexto"),
    ("## **Título**", "*Título*"),
    ("#1 prioridad", "#1 prioridad"),
    ("- uno\n* dos\n+ tres", "• uno\n• dos\n• tres"),
    ("- **Dato:** 70%", "• *Dato:* 70%"),
    ("Mira [la fuente](https://bcg.com/x)", "Mira la fuente: https://bcg.com/x"),
    ("[https://a.mx](https://a.mx)", "https://a.mx"),
    ("a\n\n\n\nb", "a\n\nb"),
    ("---\ntexto", "texto"),
    ("sobran ** asteriscos", "sobran  asteriscos"),
    ("3 * 4 = 12", "3 * 4 = 12"),
    ("", ""),
])
def test_to_whatsapp(raw, expected):
    assert to_whatsapp(raw) == expected


def test_idempotent_and_keeps_correct_whatsapp_text():
    ok = "*Negrita* con _cursiva_, ~tachado~ y\n• viñeta\n1. número"
    assert to_whatsapp(ok) == ok
    messy = "# T\n**a** __b__\n- x\n[l](https://e.mx)"
    assert to_whatsapp(to_whatsapp(messy)) == to_whatsapp(messy)


def test_code_fences_are_untouched():
    raw = "**Antes**\n```\n**no tocar** - x\n```\n**Después**"
    assert to_whatsapp(raw) == "*Antes*\n```\n**no tocar** - x\n```\n*Después*"


def test_static_bot_texts_need_no_conversion():
    """The hand-written texts already use WhatsApp syntax, so the sanitizer must not alter them."""
    def walk(d):
        for v in d.values():
            if isinstance(v, dict):
                yield from walk(v)
            elif isinstance(v, str):
                yield v
    for table in (menu.TEXT, quiz.T, sessions.TEXT):
        for s in walk(table):
            assert to_whatsapp(s) == s.strip(), s


def test_voice_document_is_in_the_prompt():
    voice = knowledge.voice_text()
    assert "Dexter" in voice and "Estrada Lab" in voice and "destilar" in voice
    blocks = agent.system_blocks()
    texts = [b["text"] for b in blocks]
    assert any(t.startswith("<voice>") and "Dexter" in t for t in texts)
    assert "Dexter" in agent.INSTRUCTIONS and "ONE asterisk" in agent.INSTRUCTIONS
    # the cached block (core docs) stays last so the whole prefix is cached
    assert "cache_control" in blocks[-1] and "<voice>" not in blocks[-1]["text"]


def test_welcome_introduces_dexter():
    for lang in ("es", "en"):
        assert "Dexter" in menu.welcome(lang).text


async def test_reply_is_sanitized_before_sending(make_client):
    import respx
    import httpx
    from tests.test_channels import TWILIO_SEND, tw_msg, twilio_post  # noqa: F401
    from app import main

    sent = []

    class Chan:
        name = "fake"

        async def send_text(self, to, body):
            sent.append(body)

    class Msg:
        user_id, reply_to, message_id = "5215500000000", "x", ""

    make_client()
    assert await main.send_reply(Chan(), Msg(), "**Hola** equipo\n\n- uno") is True
    assert sent == ["*Hola* equipo\n\n• uno"]
