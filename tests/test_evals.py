"""Offline eval set (evals/cases/*.json): routing, tool dispatch, deterministic replies.

The Anthropic API is mocked with respx, so this runs in the normal suite with no network.
The same cases run against the real models with ``pytest -m eval_live`` (tests/test_evals_live.py).
"""
import pytest

from evals.harness import load_cases, run_case, validate

CASES = load_cases()


def test_eval_set_is_well_formed():
    assert len(CASES) >= 40
    bad = {c.get("id"): validate(c) for c in CASES if validate(c)}
    assert not bad


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_offline(case, make_client):
    result = run_case(case, make_client, live=False)
    assert result.passed, f"{result.reason} | route={result.route} tools={result.tools} replies={result.replies}"
