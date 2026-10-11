"""Live status for on-request entries: real facts only, fail-soft, nothing private."""
import json
import re

import httpx
import respx

from app import status

from .test_channels import SLACK

PRIVATE = [re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),   # IPv4
           re.compile(r"\d{10,}"),                       # phone numbers, WABA / phone-number ids
           re.compile(r"(?i)token|secret|password|ssh")]


def _build_info(monkeypatch, tmp_path, **data):
    p = tmp_path / "BUILD_INFO.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(status, "BUILD_INFO", p)


def _clean(text):
    assert "{{" not in text and "}}" not in text
    for rx in PRIVATE:
        assert not rx.search(text), (rx.pattern, text)


def test_fields_from_build_info(make_client, monkeypatch, tmp_path):
    make_client()
    _build_info(monkeypatch, tmp_path, sha="0123456789abcdef", built_at="2026-10-10T18:00:00Z",
                tests_passed=412, eval_cases=48, source="ci")
    line, block = status.render_line("es"), status.render_block("es")
    assert "versión 0123456" in line and "10 de octubre" in line and "412 pruebas en verde" in line
    assert "48 casos de evaluación" in block and "CI/CD" in block.split("⏳")[0]
    en = status.render_line("en")
    assert "version 0123456" in en and "Oct 10" in en and "412 tests passing" in en
    _clean(line + block + en)


def test_missing_build_info_is_omitted(make_client, monkeypatch, tmp_path):
    make_client()
    monkeypatch.setattr(status, "BUILD_INFO", tmp_path / "nope.json")
    line = status.render_line("es")
    assert line.startswith("🟢 Estado ahora: en línea desde hace") and "versión" not in line
    assert "CI/CD" in status.render_block("es").split("⏳")[1]   # not from CI -> pending


def test_bad_sha_never_shown(make_client, monkeypatch, tmp_path):
    make_client()
    _build_info(monkeypatch, tmp_path, sha="10.0.0.1; rm -rf", tests_passed="many")
    line = status.render_line("es")
    assert "versión" not in line and "pruebas" not in line
    _clean(line)


def test_features_follow_config(make_client):
    make_client(FLOWS_ENABLED="true", LIMITS_ENABLED="false", CLOSE_ON_INTENT="true")
    f = dict(status.features())
    assert f["talk"] and f["close"] and f["persistence"] and not f["limits"]
    block = status.render_block("es")
    on, off = block.split("⏳")[0], block.split("⏳")[1]
    assert "topes de uso" in off and "topes de uso" not in on
    assert "memoria persistente" in on
    make_client(DB_PATH="")
    assert "memoria persistente" in status.render_block("es").split("⏳")[1]


def test_total_failure_renders_unavailable(make_client, monkeypatch):
    make_client()

    def boom():
        raise RuntimeError("db exploded at 10.1.2.3")
    monkeypatch.setattr(status, "snapshot", boom)
    assert status.render_line("es") == "Estado no disponible en este momento."
    assert status.render_block("en") == "Status unavailable right now."


def test_failing_field_is_dropped(make_client, monkeypatch):
    make_client()
    monkeypatch.setattr(status, "_meta_state", lambda: 1 / 0)
    block = status.render_block("es")
    assert "Meta:" not in block and "Activo" in block


def test_meta_events_stored_and_sanitized(make_client):
    make_client()
    status.record_meta_event("account_review_update", {"decision": "APPROVED"})
    status.record_meta_event("phone_number_name_update", {
        "display_phone_number": "523312345678", "decision": "DEFERRED", "requested_verified_name": "Estrada AI Lab"})
    status.record_meta_event("business_capability_update", {"max_daily_conversations_per_business": "TIER_1K"})
    status.record_meta_event("account_update", {"event": "<script>", "waba_info": {"waba_id": "1234567890123"}})
    block = status.render_block("es")
    assert "Meta: cuenta aprobada, nombre visible en revisión, límite de mensajería TIER_1K" in block
    assert "Estrada AI Lab" not in block and "script" not in block
    _clean(block)


def test_verified_account_wins(make_client):
    make_client()
    status.record_meta_event("account_review_update", {"decision": "REJECTED"})
    status.record_meta_event("account_update", {"event": "VERIFIED_ACCOUNT"})
    assert "negocio verificado" in status.render_block("es")


def test_env_fallback_until_a_webhook_arrives(make_client):
    make_client(META_VERIFICATION_STATUS="en revisión")
    assert "Meta: verificación: en revisión" in status.render_block("es")
    status.record_meta_event("account_review_update", {"decision": "APPROVED"})
    assert "cuenta aprobada" in status.render_block("es") and "verificación: en revisión" not in status.render_block("es")


def test_env_fallback_never_echoes_long_numbers(make_client):
    make_client(META_VERIFICATION_STATUS="call 5213312345678")
    assert "Meta" not in status.render_block("es")


@respx.mock
def test_meta_webhook_updates_status_and_survives_restart(make_client):
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client()
    c.post("/webhooks/meta", json={"entry": [{"changes": [
        {"field": "account_review_update", "value": {"decision": "APPROVED"}}]}]})
    status.reset()                                   # memory gone, SQLite still has it
    assert "cuenta aprobada" in status.render_block("es")


def test_counters(make_client):
    make_client()
    for _ in range(3):
        status.bump("conversations")
    status.bump("curious")
    assert status.count("conversations") == 3 and status.count("assessments") is None
    assert "3 conversaciones" in status.render_block("es") and "1 curiosos" in status.render_block("es")
    make_client(DB_PATH="")
    status.bump("curious")
    assert status.count("curious") == 1


@respx.mock
def test_new_conversation_and_assessment_are_counted(make_client):
    from .test_flows import meta_payload
    from app.flows import quiz
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(return_value=httpx.Response(200, json={}))
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(return_value=httpx.Response(200, json={"id": "M"}))
    respx.post(SLACK).mock(return_value=httpx.Response(200))
    c = make_client(FLOWS_ENABLED="true")
    c.post("/webhooks/meta", json=meta_payload("w0", reply_id="menu:quiz", title="x"))
    for i in range(len(quiz.QUESTIONS)):
        c.post("/webhooks/meta", json=meta_payload(f"w{i + 1}", reply_id=f"quiz:{i}:2", title="x"))
    assert status.count("conversations") == 1 and status.count("assessments") == 1
