"""WhatsApp formatting (app/format_whatsapp.py): the rules, idempotence, atomic blocks, fit(), and the
flag that restores the conversion from before. Pure: no network, no channel."""
import random

import pytest

from app import format_whatsapp as fw
from app import formatting

# The reply the bot sent on 2026-10-12 (production screenshot): a table WhatsApp showed raw.
EVIDENCE = (
    "Así se ve en compras:\n\n"
    "| Actividad | Quién ejecuta | Quién responde |\n"
    "|---|---|---|\n"
    "| Analizar cotizaciones y comparar proveedores | IA asistida | Gerente de compras |\n"
    "| Generar órdenes de compra recurrentes | Agente (automático) | Gerente de compras |\n\n"
    "¿Quieres ver otro rol?"
)


def test_the_evidence_table_becomes_readable_blocks_with_no_pipes_or_rules():
    out, counts = fw.to_whatsapp(EVIDENCE)
    assert "|" not in out and "---" not in out
    assert "*Analizar cotizaciones y comparar proveedores*\n• Quién ejecuta: IA asistida\n• Quién responde: Gerente de compras" in out
    assert "*Generar órdenes de compra recurrentes*\n• Quién ejecuta: Agente (automático)" in out
    assert out.startswith("Así se ve en compras:\n\n") and out.endswith("¿Quieres ver otro rol?")
    assert counts["tables"] == 1
    assert fw.to_whatsapp(out) == (out, dict.fromkeys(fw.COUNTER_KEYS, 0))


@pytest.mark.parametrize("raw, expected", [
    # two columns: one bullet per row
    ("| Rol | Tarea |\n|---|---|\n| Gerente | aprueba |\n| Dueño | firma |",
     "• *Gerente*: aprueba\n• *Dueño*: firma"),
    # ragged rows: a short row keeps what it has, a long row's extra cell has no label
    ("| A | B | C |\n|---|---|---|\n| x | 1 | 2 | extra |\n| q |",
     "*x*\n• B: 1\n• C: 2\n• extra\n\n*q*"),
    # empty cells are dropped; an empty first cell takes the next value as the title
    ("| Rol | Tarea | Quién |\n|---|---|---|\n|  | aprueba |  |\n| Dueño |  | Alan |",
     "*aprueba*\n\n*Dueño*\n• Quién: Alan"),
    # escaped pipes stay inside their cell
    ("| Campo | Valor |\n|---|---|\n| Fórmula | a \\| b |",
     "• *Fórmula*: a | b"),
    # a table is not cut from the paragraph before it
    ("Intro sin línea en blanco:\n| A | B |\n|---|---|\n| 1 | 2 |\nDespués.",
     "Intro sin línea en blanco:\n\n• *1*: 2\n\nDespués."),
])
def test_table_shapes(raw, expected):
    out, counts = fw.to_whatsapp(raw)
    assert out == expected
    assert counts["tables"] == 1 and "|" not in out.replace("a | b", "")


def test_more_than_eight_rows_keeps_eight_and_says_how_many_are_left():
    rows = "".join(f"| r{i} | {i} |\n" for i in range(10)).rstrip("\n")
    out, _ = fw.to_whatsapp("| n | v |\n|---|---|\n" + rows)
    assert out.count("• *") == 8 and out.endswith("… y 2 más")
    en, _ = fw.to_whatsapp("| n | v |\n|---|---|\n" + rows, lang="en")
    assert en.endswith("… and 2 more")


def test_a_table_inside_a_code_fence_is_left_alone():
    raw = "Mira:\n```\n| a | b |\n|---|---|\n**sin tocar**\n```"
    assert fw.to_whatsapp(raw)[0] == raw


@pytest.mark.parametrize("raw, expected", [
    ("# Título:\ntexto", "*Título*\ntexto"),
    ("## **Doble** ##", "*Doble*"),
    ("###### Fin", "*Fin*"),
    ("#1 prioridad", "#1 prioridad"),
    ("**a** y __b__ y ***c***", "*a* y *b* y *c*"),
    ("~~d~~ y _e_ y *f*", "~d~ y _e_ y *f*"),
    ("Mira [la fuente](https://bcg.com/x) y [https://a.mx](https://a.mx)",
     "Mira la fuente: https://bcg.com/x y https://a.mx"),
    ("uno ![img](http://x/y.png) dos", "uno dos"),
    ("uno\n\n---\n\n***\n\ndos", "uno\n\ndos"),
    ("línea<br>dos <b>negrita</b>", "línea\ndos negrita"),
    ("- uno\n  - hijo\n    - nieto\n* dos\n+ tres", "• uno\n• hijo\n• nieto\n• dos\n• tres"),
    ("1. a\n   2. b", "1. a\n2. b"),
    ("> cita", "> cita"),
    ("usa `ls -la` aquí", "usa ```ls -la``` aquí"),
    ("```python\nx = 1 ** 2\n```", "```\nx = 1 ** 2\n```"),
    ("sobran ** asteriscos", "sobran  asteriscos"),
    ("abierto *sin cerrar", "abierto sin cerrar"),
    ("3 * 4 = 12", "3 * 4 = 12"),
    ("~3 min y ~10%", "~3 min y ~10%"),
    ("snake_case_name y a_b", "snake_case_name y a_b"),
    ("https://a.mx/x_y_z", "https://a.mx/x_y_z"),
])
def test_markdown_becomes_whatsapp(raw, expected):
    assert fw.to_whatsapp(raw)[0] == expected


def test_counters_say_what_was_fixed():
    _, c = fw.to_whatsapp("# T\n**a** [l](https://e.mx) ~~x~~ ![i](u.png)")
    assert c == {"tables": 0, "headers": 1, "links": 1, "bold": 1, "other": 2}


def test_plain_text_comes_back_byte_identical():
    samples = [
        "Hola, ¿cómo vas? Aquí va la respuesta.",
        "La regla 10-20-70 viene de BCG (2024): 70% a personas y procesos.",
        "Emojis 👋 y acentos: canción, ñandú, ¡sí!",
        "Una línea.\n\nOtra línea, sin nada de formato.",
        "Precio: 3 * 4 = 12 y ~3 min",
    ]
    for s in samples:
        assert fw.to_whatsapp(s) == (s, dict.fromkeys(fw.COUNTER_KEYS, 0))


def _fuzz_inputs(n=3000, seed=7):
    tokens = ["*", "**", "***", "_", "__", "~", "~~", "`", "```", "#", "## ", " ", "\n", "\n\n", "- ", "* ",
              "1. ", "> ", "| ", "|---|", "---", "[", "]", "(", ")", "![", "](", "https://a.mx/x_y", "a",
              "é", "👋", ":", ".", "<br>", "<b>", "</b>", "dos palabras", "¿", "…", "\\|", "> "]
    rng = random.Random(seed)
    return ["".join(rng.choice(tokens) for _ in range(rng.randint(1, 40))) for _ in range(n)]


@pytest.mark.parametrize("raw", [
    EVIDENCE, "# T\n**a** __b__ - x\n[l](https://e.mx)", "```\n**x**\n```\n*y*", "3 * 4 = 12",
    "| a | b |\n|---|---|\n| x | y |", "- uno\n  - hijo", "sobran ** asteriscos",
] + _fuzz_inputs(400))
def test_idempotent_and_a_fixed_point(raw):
    once, _ = fw.to_whatsapp(raw)
    twice, counts = fw.to_whatsapp(once)
    assert twice == once
    assert not any(counts.values())   # nothing left for a second pass to fix


def test_random_inputs_never_raise():
    for raw in _fuzz_inputs(1500, seed=11):
        out, counts = fw.to_whatsapp(raw)
        assert isinstance(out, str) and set(counts) == set(fw.COUNTER_KEYS)


# --------------------------------------------------------------------------- #
# Blocks, the atomic trim and the splitter
# --------------------------------------------------------------------------- #
def test_blocks_are_code_list_header_or_prose():
    text = "Intro.\n\n*Título*\n\n• uno\n• dos\n\n```\nlinea\n\notra\n```\n\nCierre."
    kinds = [b.kind for b in fw.blocks(text)]
    assert kinds == ["prose", "header", "list", "code", "prose"]
    code = [b for b in fw.blocks(text) if b.kind == "code"][0]
    assert text[code.start:code.end] == "```\nlinea\n\notra\n```"   # the blank line inside stays in the block


def test_trim_never_cuts_inside_a_code_block():
    text = "Antes de la prueba.\n\n```\nlinea uno. Otra frase aquí\n\nlinea dos\n```\n\n" + " ".join(["palabra"] * 30)
    out, trimmed = formatting.cap_words(text, 12, "OFFER")
    assert trimmed and out == "Antes de la prueba.\n\nOFFER"


def test_trim_never_cuts_inside_a_list_block():
    rows = "\n".join(f"• dato {i}. dato {i}. dato {i}." for i in range(20))
    text = "Lead corto.\n\n" + rows + "\n\n" + " ".join(["fin"] * 40)
    out, trimmed = formatting.cap_words(text, 30, "OFFER")
    assert trimmed and out == "Lead corto.\n\nOFFER"
    assert "• dato 19" not in out and "•" not in out.replace("OFFER", "")


def test_a_header_is_never_the_last_thing_before_the_offer():
    text = "Lead corto.\n\n*Título*\n\n" + " ".join(["palabra"] * 30)   # "Lead corto. *Título*" would fit
    out, trimmed = formatting.cap_words(text, 5, "OFFER")
    assert trimmed and out == "Lead corto.\n\nOFFER"


def test_a_table_is_never_cut_between_rows_by_the_trim():
    text = fw.to_whatsapp("| a | b |\n|---|---|\n| x | y |\n| z | w |\n| p | q |")[0]   # one list block
    assert formatting.cap_words(text, 5, "OFFER") == (text, False)   # nothing whole fits: the block stays whole


def test_pack_splits_long_text_between_blocks_only():
    big = "\n\n".join(f"*Bloque {i}*\n" + "\n".join(f"• dato {i}.{j} " + "palabra " * 5 for j in range(40))
                       for i in range(3))
    parts = fw.pack(big, 4096)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert all(p.startswith("*Bloque") for p in parts)   # every part starts at a block, never mid-line
    assert "\n".join(parts).count("• dato") == 3 * 40


def test_pack_re_fences_a_code_block_that_is_too_long():
    code = "```\n" + "\n".join(f"linea {i} " + "x" * 60 for i in range(200)) + "\n```"
    parts = fw.pack(code, 4096)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert all(p.startswith("```\n") and p.endswith("\n```") for p in parts)


def test_pack_leaves_a_short_text_alone():
    assert fw.pack("hola") == ["hola"]


# --------------------------------------------------------------------------- #
# fit(): the interactive limits, never a cut inside a word
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, limit, expected", [
    ("Explorar la plática de Gobernanza de IA", 24, "Explorar la plática de…"),
    ("Corto", 24, "Corto"),
    ("exactamente veinticuatro", 24, "exactamente veinticuatro"),
    ("exactamente veinticuatro!!", 24, "exactamente…"),
    ("supercalifragilisticoespialidoso largo", 10, "supercali…"),
    ("uno dos, tres", 10, "uno dos…"),
])
def test_fit_cuts_at_a_word_boundary(text, limit, expected):
    assert fw.fit(text, limit) == expected


def test_fit_never_goes_over_the_limit_and_never_cuts_mid_word():
    rng = random.Random(3)
    words = ["ñandú", "palabra", "a", "longuísimapalabra", "¿qué?", "x"]
    for _ in range(500):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 15)))
        limit = rng.choice([20, 24, 72, 1024])
        out = fw.fit(text, limit)
        assert len(out) <= limit
        if out != text:
            assert out.endswith("…")
            head = out[:-1]
            assert text.startswith(head)
            assert text[len(head)] == " " or " " not in text[: limit - 1]   # a word ends there, or none fits


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #
def test_the_sanitizer_is_on_by_default(make_client):
    make_client()
    from app.config import get_settings
    assert get_settings().wa_format_sanitize is True


def test_the_flag_off_gives_back_the_conversion_from_before(make_client):
    """Literal outputs of the conversion as it was (app/formatting.py before this change)."""
    make_client(WA_FORMAT_SANITIZE="false")
    expected = [
        "*Hola* equipo\n\n• uno",
        "| Actividad | Quién ejecuta | Quién responde |\n|---|---|---|\n| Analizar cotizaciones y comparar proveedores | IA asistida | Gerente de compras |\n| Generar órdenes de compra recurrentes | Agente (automático) | Gerente de compras |",
        "*Título*\n*a* *b* - x\nl: https://e.mx\n!img: http://x/y.png",
        "uno\n\ndos  tres",
        "usa `ls` aquí y ```\n**no** tocar\n```\n*fin*",
        "3 * 4 = 12 y ~3 min",
    ]
    samples = [
        "**Hola** equipo\n\n- uno",
        "| Actividad | Quién ejecuta | Quién responde |\n|---|---|---|\n| Analizar cotizaciones y comparar proveedores | IA asistida | Gerente de compras |\n| Generar órdenes de compra recurrentes | Agente (automático) | Gerente de compras |",
        "# Título\n**a** __b__ - x\n[l](https://e.mx)\n![img](http://x/y.png)",
        "uno\n\n\n\ndos ** tres",
        "usa `ls` aquí y ```\n**no** tocar\n```\n**fin**",
        "3 * 4 = 12 y ~3 min",
    ]
    assert [fw.sanitize(s)[0] for s in samples] == expected
    assert [formatting.to_whatsapp(s) for s in samples] == expected


def test_the_flag_off_keeps_the_blank_line_split_as_before(make_client):
    make_client(WA_FORMAT_SANITIZE="false")
    from app import main
    text = "Intro.\n\n*Ejemplo: Gerente*\n\nCuerpo."
    assert fw.enabled() is False
    assert main.split_bubbles(text) == ["Intro.", "*Ejemplo: Gerente*", "Cuerpo."]   # the header split, as before


def test_a_failing_sanitizer_never_blocks_the_reply(make_client, monkeypatch):
    make_client()
    from app import main

    def boom(*_a, **_k):
        raise RuntimeError("bug in the sanitizer")

    monkeypatch.setattr(fw, "to_whatsapp", boom)
    assert fw.sanitize("**a** | x |")[0] == "**a** | x |"      # the text goes out as written
    monkeypatch.setattr(fw, "blocks", boom)                      # the block-aware split and trim fail too
    assert main.split_bubbles("Uno.\n\nDos.") == ["Uno.", "Dos."]
    out, trimmed = formatting.cap_words("Uno dos tres cuatro cinco. " * 40, 10, "OFFER")
    assert trimmed and out.endswith("OFFER")
