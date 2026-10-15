"""The talk's knowledge file is material to answer from, not the speaker's script."""
from app import knowledge

SCRIPT_MARKERS = ("[pausa", "pausa de", "Qué decir", "Cómo decirlo", "Puente:", "Objetivo:")


def test_talk_file_carries_no_speaker_script_markers():
    talk = (knowledge.KB_DIR / "core" / "platica.md").read_text(encoding="utf-8")
    assert [m for m in SCRIPT_MARKERS if m in talk] == []
    assert "no la recites" in talk.lower()  # the rule Dexter reads: rephrase, don't recite


def test_core_knowledge_keeps_the_talk_and_the_original_references():
    core = knowledge.core_text()
    assert 'name="platica"' in core and 'name="base-de-conocimiento"' in core
    assert "## Fuentes" in core  # the sources of the base stay in the prompt
