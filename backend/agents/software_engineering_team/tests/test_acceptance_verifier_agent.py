"""Tests for AcceptanceVerifierAgent (routed through the shared review engine)."""

from __future__ import annotations

from acceptance_verifier_agent import AcceptanceVerifierAgent
from acceptance_verifier_agent.agent import derive_per_criterion
from acceptance_verifier_agent.models import (
    AcceptanceVerifierInput,
    AcceptanceVerifierOutput,
)
from code_review_agent.models import CodeReviewIssue

from llm_service.clients.dummy import DummyLLMClient


def _input(**overrides: object) -> AcceptanceVerifierInput:
    base = {
        "code": "def add(a, b):\n    return a + b",
        "task_description": "Implement add(a, b)",
        "acceptance_criteria": [
            "add(1, 2) returns 3",
            "add(0, 0) returns 0",
        ],
        "language": "python",
    }
    base.update(overrides)
    return AcceptanceVerifierInput(**base)  # type: ignore[arg-type]


class _IssueStubClient(DummyLLMClient):
    """Returns one canned engine response (chunk review) for every call.

    A criterion-tagged issue carries no ``file_path`` so the engine's
    false-positive filter is skipped (it only re-checks code-location findings),
    making the adapter's per-criterion derivation deterministic.
    """

    def __init__(self, response):
        super().__init__()
        self._response = response

    def complete_json(self, prompt, **kwargs):
        return self._response


def test_acceptance_verifier_default_run_returns_output() -> None:
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, AcceptanceVerifierOutput)
    # Dummy stub reports no issues, so every criterion is satisfied.
    assert result.all_satisfied is True
    assert len(result.per_criterion) == 2
    assert all(c.satisfied for c in result.per_criterion)


def test_acceptance_verifier_short_circuits_on_empty_criteria() -> None:
    """No criteria → no engine call, always all_satisfied with empty list."""

    class _TripWireClient(DummyLLMClient):
        def complete_json(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("engine must not be called when criteria is empty")

        def chat_json_round(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("engine must not be called when criteria is empty")

    agent = AcceptanceVerifierAgent(_TripWireClient())
    result = agent.run(_input(acceptance_criteria=[]))
    assert isinstance(result, AcceptanceVerifierOutput)
    assert result.all_satisfied is True
    assert result.per_criterion == []
    assert "no criteria" in result.summary.lower()


def test_acceptance_verifier_marks_unmet_criterion_from_tagged_issue() -> None:
    """An engine issue tagged with a criterion marks exactly that criterion
    unsatisfied; the rest stay satisfied and all_satisfied is False."""

    stub = _IssueStubClient(
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "add(0, 0) returns 0",
                    "file_path": "",
                    "description": "No code path returns 0 for add(0, 0).",
                    "suggestion": "Handle the zero case.",
                }
            ],
            "summary": "One criterion unmet",
        }
    )
    agent = AcceptanceVerifierAgent(stub)
    result = agent.run(_input())
    assert result.all_satisfied is False
    assert len(result.per_criterion) == 2
    by_criterion = {c.criterion: c for c in result.per_criterion}
    assert by_criterion["add(1, 2) returns 3"].satisfied is True
    assert by_criterion["add(0, 0) returns 0"].satisfied is False
    assert "returns 0" in by_criterion["add(0, 0) returns 0"].evidence


def test_acceptance_verifier_failure_returns_fallback() -> None:
    """A review-engine failure degrades to all_satisfied=False, never raises."""

    class _BoomClient(DummyLLMClient):
        def complete_json(self, *a, **kw):  # type: ignore[override]
            raise RuntimeError("engine down")

        def chat_json_round(self, *a, **kw):  # type: ignore[override]
            raise RuntimeError("engine down")

    agent = AcceptanceVerifierAgent(_BoomClient())
    result = agent.run(_input())
    assert result.all_satisfied is False
    assert result.per_criterion == []
    assert "failed" in result.summary.lower()


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``AcceptanceVerifierAgent`` instance must
    handle many sequential ``run()`` calls."""
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    for i in range(4):
        result = agent.run(_input(task_description=f"Task {i}"))
        assert isinstance(result, AcceptanceVerifierOutput), (
            f"run {i} did not return AcceptanceVerifierOutput"
        )
        assert result.all_satisfied is True, f"run {i} failed: {result.summary}"


# ---------------------------------------------------------------------------
# derive_per_criterion (pure)
# ---------------------------------------------------------------------------


def test_derive_per_criterion_all_satisfied_when_no_issues() -> None:
    out = derive_per_criterion(["a", "b"], [])
    assert [c.satisfied for c in out] == [True, True]
    assert all(c.evidence == "Satisfied" for c in out)


def test_derive_per_criterion_matches_by_category_and_substring() -> None:
    issues = [
        CodeReviewIssue(severity="high", category="b", description="b is unmet"),
        CodeReviewIssue(severity="high", category="other", description="mentions c here"),
    ]
    out = derive_per_criterion(["a", "b", "c"], issues)
    by = {c.criterion: c for c in out}
    assert by["a"].satisfied is True
    assert by["b"].satisfied is False and by["b"].evidence == "b is unmet"
    # matched by substring in the description rather than an exact category tag
    assert by["c"].satisfied is False


def test_derive_per_criterion_all_unmet() -> None:
    issues = [CodeReviewIssue(severity="high", category="x", description="d")]
    out = derive_per_criterion(["x"], issues)
    assert out[0].satisfied is False


def test_derive_per_criterion_blank_criterion_never_matches() -> None:
    # A blank criterion must not spuriously match an empty category.
    issues = [CodeReviewIssue(severity="high", category="", description="")]
    out = derive_per_criterion([""], issues)
    assert out[0].satisfied is True
