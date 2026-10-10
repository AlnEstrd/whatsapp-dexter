"""scripts/transcript_to_case.py: real conversations -> draft eval cases."""
import json
import subprocess
import sys
from pathlib import Path

import httpx
import respx

from evals.harness import validate

from .test_channels import TWILIO_SEND, tw_msg, twilio_post

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "transcript_to_case.py"


def run(*args):
    out = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_slack_export_to_case(tmp_path):
    export = [
        {"ts": "1700000000.000100", "text": "💬 New chat — Ana (+523312345678) · meta"},
        {"ts": "1700000001.0", "thread_ts": "1700000000.000100", "text": "👤 hola"},
        {"ts": "1700000002.0", "thread_ts": "1700000000.000100", "text": "🤖 👋 ¡Hola! ¿Qué quieres hacer?"},
        {"ts": "1700000003.0", "thread_ts": "1700000000.000100", "text": ":bust_in_silhouette: ¿qué es la regla 10-20-70?"},
        {"ts": "1700000004.0", "thread_ts": "1700000000.000100", "text": ":robot_face: Es 10% algoritmos &amp; más"},
        {"ts": "1700000005.0", "thread_ts": "1700000099.000100", "text": "👤 otro hilo"},
    ]
    f = tmp_path / "thread.json"
    f.write_text(json.dumps(export))
    case = run("--slack", str(f), "--thread", "1700000000.000100")
    assert case["prior"] == ["hola"] and case["message"] == "¿qué es la regla 10-20-70?"
    assert case["observed"] == ["Es 10% algoritmos & más"] and case["lang"] == "es"
    assert validate(case) == []
    first = run("--slack", str(f), "--thread", "1700000000.000100", "--turn", "1", "--id", "x")
    assert first["message"] == "hola" and first["prior"] == [] and first["id"] == "x"


@respx.mock
def test_sqlite_conversation_to_case(make_client, tmp_path):
    respx.post(TWILIO_SEND).mock(return_value=httpx.Response(201))
    c = make_client()
    twilio_post(c, tw_msg("hola", sid="S1"))
    twilio_post(c, tw_msg("What is the 10-20-70 rule?", sid="S2"))
    out = tmp_path / "cases.json"
    subprocess.run([sys.executable, str(SCRIPT), "--db", str(tmp_path / "data" / "bot.sqlite3"),
                    "--user", "523312345678", "--out", str(out)], check=True, capture_output=True)
    case = json.loads(out.read_text())[0]
    assert case["message"] == "What is the 10-20-70 rule?" and case["prior"] == ["hola"]
    assert case["observed"] == ["echo: What is the 10-20-70 rule?"] and case["lang"] == "en"
