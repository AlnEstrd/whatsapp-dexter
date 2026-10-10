"""Live eval run against the real models. Never part of the normal suite.

    ANTHROPIC_API_KEY=sk-ant-... RUN_LIVE_EVALS=1 pytest -m eval_live -s

Prints a table (case, pass/fail, reason) and writes evals/results/<timestamp>.json.
"""
import json
import os
import time

import pytest

from evals.harness import RESULTS_DIR, load_cases, run_case, table

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # read before the fixtures blank it
LIVE = bool(API_KEY) and os.environ.get("RUN_LIVE_EVALS") == "1"

pytestmark = [pytest.mark.eval_live,
              pytest.mark.skipif(not LIVE, reason="set ANTHROPIC_API_KEY and RUN_LIVE_EVALS=1")]


def test_eval_live(make_client):
    cases = load_cases()
    only = os.environ.get("EVAL_CASES")  # optional comma-separated ids
    if only:
        cases = [c for c in cases if c["id"] in set(only.split(","))]
    results = [run_case(c, make_client, live=True, api_key=API_KEY) for c in cases]
    print("\n" + table(results))
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({
        "model": os.environ.get("CLAUDE_MODEL", "default"),
        "passed": sum(r.passed for r in results), "total": len(results),
        "results": [r.as_dict() for r in results]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"results: {out}")
    failed = [r.id for r in results if not r.passed]
    assert not failed, f"{len(failed)} live eval case(s) failed: {failed}"
