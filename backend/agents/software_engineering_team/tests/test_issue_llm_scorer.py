"""Tests for ``github_source.issue_llm_scorer.score_issue_via_llm``.

Covers the LLM success path (a mocked reply parses into a ``ScoreBreakdown``),
that the prompt is built from title/body/labels via ``build_scoring_prompt``,
client resolution (injected ``llm_client`` vs. ``get_client(agent_key)``), and
that a malformed reply propagates a validation error rather than being
silently coerced or falling back to a heuristic. No live LLM/network calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm_service import LLMClient, LLMSchemaValidationError
from software_engineering_team.github_source.issue_llm_scorer import score_issue_via_llm
from software_engineering_team.github_source.issue_scoring import ScoreBreakdown
from software_engineering_team.shared import single_shot_review as ssr_mod

VALID_PAYLOAD = {
    "conceptual_score": 3,
    "conceptual_rationale": "Touches one well-understood module.",
    "loc_score": 5,
    "loc_rationale": "A few hundred lines across two files.",
    "code_complexity_score": 2,
    "code_complexity_rationale": "Straightforward branching, no new algorithms.",
    "aggregate_score": 3,
    "aggregate_rationale": "Overall a small, contained change.",
    "suggested_labels": ["good-first-issue"],
}


class _StubClient(LLMClient):
    """Minimal LLMClient stub recording every ``complete_json`` call."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        prompt,
        *,
        objective,
        temperature=0.0,
        system_prompt=None,
        tools=None,
        think=None,
        **kwargs,
    ):
        self.calls.append({"prompt": prompt, "objective": objective})
        return self._payload

    def complete(
        self, prompt, *, objective, temperature=0.7, max_tokens=None, system_prompt=None, **kwargs
    ):
        raise AssertionError("complete() should not be called by score_issue_via_llm")

    def get_max_context_tokens(self) -> int:
        return 8192


def _patch_generate_structured(monkeypatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {}

    def fake_generate_structured(prompt, *, schema, objective, system_prompt=None, **kwargs):
        recorded["prompt"] = prompt
        recorded["schema"] = schema
        recorded["objective"] = objective
        recorded["kwargs"] = kwargs
        return schema(**VALID_PAYLOAD)

    monkeypatch.setattr(ssr_mod, "generate_structured", fake_generate_structured)
    return recorded


# ---------------------------------------------------------------------------
# LLM success path
# ---------------------------------------------------------------------------


def test_success_path_returns_score_breakdown():
    client = _StubClient(VALID_PAYLOAD)

    result = score_issue_via_llm(
        "Add retry logic to the job poller",
        "The poller should retry transient failures with backoff.",
        ["backend", "reliability"],
        llm_client=client,
    )

    assert isinstance(result, ScoreBreakdown)
    assert result.conceptual_score == 3
    assert result.loc_score == 5
    assert result.code_complexity_score == 2
    assert result.aggregate_score == 3
    assert result.suggested_labels == ["good-first-issue"]


def test_prompt_includes_title_body_labels():
    client = _StubClient(VALID_PAYLOAD)

    score_issue_via_llm(
        "Add retry logic to the job poller",
        "The poller should retry transient failures with backoff.",
        ["backend", "reliability"],
        llm_client=client,
    )

    prompt = client.calls[0]["prompt"]
    assert "Add retry logic to the job poller" in prompt
    assert "The poller should retry transient failures with backoff." in prompt
    assert "backend, reliability" in prompt


def test_default_objective_used_when_not_overridden():
    client = _StubClient(VALID_PAYLOAD)

    score_issue_via_llm("Title", "Body", [], llm_client=client)

    assert client.calls[0]["objective"] == "score github issue Fibonacci complexity"


def test_custom_objective_is_forwarded():
    client = _StubClient(VALID_PAYLOAD)

    score_issue_via_llm("Title", "Body", [], llm_client=client, objective="custom objective")

    assert client.calls[0]["objective"] == "custom objective"


# ---------------------------------------------------------------------------
# Client resolution — delegated to ``generate_structured`` via
# ``run_single_shot_review`` (schema mode never resolves a client itself).
# ---------------------------------------------------------------------------


def test_injected_client_is_forwarded_to_generate_structured(monkeypatch):
    client = _StubClient(VALID_PAYLOAD)
    recorded = _patch_generate_structured(monkeypatch)

    score_issue_via_llm("Title", "Body", [], llm_client=client)

    assert recorded["kwargs"]["llm_client"] is client
    assert recorded["schema"] is ScoreBreakdown


def test_none_client_and_default_agent_key_forwarded_when_not_injected(monkeypatch):
    recorded = _patch_generate_structured(monkeypatch)

    score_issue_via_llm("Title", "Body", [])

    assert recorded["kwargs"]["llm_client"] is None
    assert recorded["kwargs"]["agent_key"] == "issue_grooming"


def test_custom_agent_key_is_forwarded(monkeypatch):
    recorded = _patch_generate_structured(monkeypatch)

    score_issue_via_llm("Title", "Body", [], agent_key="custom_key")

    assert recorded["kwargs"]["agent_key"] == "custom_key"


# ---------------------------------------------------------------------------
# No silent fallback on a malformed reply
# ---------------------------------------------------------------------------


def test_non_fibonacci_score_raises_instead_of_coercing():
    bad_payload = {**VALID_PAYLOAD, "aggregate_score": 4}
    client = _StubClient(bad_payload)

    with pytest.raises(LLMSchemaValidationError):
        score_issue_via_llm("Title", "Body", [], llm_client=client, correction_attempts=0)


def test_missing_required_field_raises_instead_of_coercing():
    bad_payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "conceptual_score"}
    client = _StubClient(bad_payload)

    with pytest.raises(LLMSchemaValidationError):
        score_issue_via_llm("Title", "Body", [], llm_client=client, correction_attempts=0)
