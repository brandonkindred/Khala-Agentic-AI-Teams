"""Tests for AcceptanceVerifierAgent (routed through the shared review engine)."""

from __future__ import annotations

import pytest
from acceptance_verifier_agent import AcceptanceVerifierAgent
from acceptance_verifier_agent.agent import derive_per_criterion
from acceptance_verifier_agent.models import (
    AcceptanceVerifierInput,
    AcceptanceVerifierOutput,
)
from code_review_agent import CodeReviewUnavailableError
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


def test_acceptance_verifier_requires_client() -> None:
    with pytest.raises(AssertionError):
        AcceptanceVerifierAgent(None)


def test_acceptance_verifier_short_circuits_on_empty_code() -> None:
    """Criteria but no code → every criterion unsatisfied, no engine call."""

    class _TripWireClient(DummyLLMClient):
        def complete_json(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("engine must not be called when code is empty")

        def chat_json_round(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("engine must not be called when code is empty")

    agent = AcceptanceVerifierAgent(_TripWireClient())
    result = agent.run(_input(code="   "))
    assert result.all_satisfied is False
    assert len(result.per_criterion) == 2
    assert all(not c.satisfied for c in result.per_criterion)
    assert all(c.evidence == "No code provided" for c in result.per_criterion)


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
                    "category": "spec-compliance",
                    "file_path": "",
                    "description": "add(0, 0) returns 0 :: No code path returns 0 for add(0, 0).",
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
    # Evidence is the text after the " :: " delimiter, not the criterion prefix.
    assert by_criterion["add(0, 0) returns 0"].evidence == "No code path returns 0 for add(0, 0)."


def test_acceptance_two_unmet_criteria_same_file_both_reported() -> None:
    """Two unmet criteria sharing a file/blank line survive coordinator dedupe
    because their descriptions differ by the verbatim-criterion prefix."""
    stub = _IssueStubClient(
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "spec-compliance",
                    "file_path": "",
                    "description": "add(1, 2) returns 3 :: no evidence found",
                },
                {
                    "severity": "high",
                    "category": "spec-compliance",
                    "file_path": "",
                    "description": "add(0, 0) returns 0 :: no evidence found",
                },
            ],
            "summary": "Both criteria unmet",
        }
    )
    result = AcceptanceVerifierAgent(stub).run(_input())
    assert result.all_satisfied is False
    assert all(not c.satisfied for c in result.per_criterion)


def test_acceptance_unattributed_finding_blocks() -> None:
    """A finding that maps to no criterion (e.g. the model dropped the criterion
    prefix) must not pass silently — the gate conservatively blocks."""
    stub = _IssueStubClient(
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "spec-compliance",
                    "file_path": "",
                    "description": "some finding without a criterion prefix",
                }
            ],
            "summary": "vague",
        }
    )
    result = AcceptanceVerifierAgent(stub).run(_input())
    # No criterion was attributed, so per_criterion all look satisfied...
    assert all(c.satisfied for c in result.per_criterion)
    # ...but the unattributed finding forces an overall block.
    assert result.all_satisfied is False
    assert "could not be attributed" in result.summary


class _RaisingEngine:
    """Stand-in for ``CodeReviewAgent`` whose ``run`` raises a given exception."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, _llm):
        return self

    def run(self, _input):
        raise self._exc


def test_acceptance_verifier_unavailable_returns_fallback(monkeypatch) -> None:
    """A CodeReviewUnavailableError degrades to all_satisfied=False, never raises."""
    monkeypatch.setattr(
        "acceptance_verifier_agent.agent.CodeReviewAgent",
        _RaisingEngine(CodeReviewUnavailableError("engine down")),
    )
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    result = agent.run(_input())
    assert result.all_satisfied is False
    assert result.per_criterion == []
    assert "failed" in result.summary.lower()


def test_acceptance_verifier_propagates_unexpected_error(monkeypatch) -> None:
    """A non-engine defect (e.g. TypeError) is not masked — it propagates."""
    monkeypatch.setattr(
        "acceptance_verifier_agent.agent.CodeReviewAgent",
        _RaisingEngine(TypeError("boom")),
    )
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    with pytest.raises(TypeError):
        agent.run(_input())


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


def test_derive_per_criterion_matches_by_description_prefix() -> None:
    issues = [
        CodeReviewIssue(severity="high", category="spec-compliance", description="b :: b is unmet"),
        # 'c' appears only in the free-form reason, not the criterion prefix —
        # it must NOT match (no broad substring/description scanning).
        CodeReviewIssue(
            severity="high", category="spec-compliance", description="other :: mentions c here"
        ),
    ]
    out = derive_per_criterion(["a", "b", "c"], issues)
    by = {c.criterion: c for c in out}
    assert by["a"].satisfied is True
    assert by["b"].satisfied is False and by["b"].evidence == "b is unmet"
    assert by["c"].satisfied is True


def test_derive_per_criterion_substring_collision_not_falsely_unmet() -> None:
    # One criterion is a substring of another; an issue tagged with the LONGER
    # criterion must not also mark the shorter one unmet.
    criteria = ["add(1,2) returns 3", "add(1,2) returns 3 and 4"]
    issues = [
        CodeReviewIssue(
            severity="high",
            category="spec-compliance",
            description="add(1,2) returns 3 and 4 :: the 'and 4' part is missing",
        )
    ]
    out = derive_per_criterion(criteria, issues)
    by = {c.criterion: c for c in out}
    assert by["add(1,2) returns 3"].satisfied is True
    assert by["add(1,2) returns 3 and 4"].satisfied is False


def test_derive_per_criterion_missing_delimiter_enum_category_not_attributed() -> None:
    # MEDIUM regression: a finding with no " :: " delimiter and category set to
    # the enum value "spec-compliance" must NOT be attributed to any criterion
    # (the old category fallback would have returned "spec-compliance" as a
    # phantom criterion, or otherwise mis-attributed).
    issues = [
        CodeReviewIssue(
            severity="high", category="spec-compliance", description="some vague finding"
        )
    ]
    out = derive_per_criterion(["add(0,0) returns 0"], issues)
    assert out[0].satisfied is True


def test_derive_per_criterion_uses_description_prefix_with_enum_category() -> None:
    # P1 regression: the model obeys the output-contract enum (category=
    # "spec-compliance") and tags the criterion in the description prefix. The
    # criterion must still be attributed (a category-only match would wrongly
    # report it satisfied).
    issues = [
        CodeReviewIssue(
            severity="high",
            category="spec-compliance",
            description="add(0,0) returns 0 :: no zero path",
        )
    ]
    out = derive_per_criterion(["add(0,0) returns 0", "other"], issues)
    by = {c.criterion: c for c in out}
    assert by["add(0,0) returns 0"].satisfied is False
    assert by["add(0,0) returns 0"].evidence == "no zero path"
    assert by["other"].satisfied is True


def test_derive_per_criterion_distinct_prefixes_for_same_reason() -> None:
    # P2 regression: two criteria whose findings share a file and reason but
    # differ by criterion prefix are both attributed (distinct descriptions keep
    # them from collapsing under the coordinator's dedupe).
    issues = [
        CodeReviewIssue(severity="high", category="spec-compliance", description="c1 :: no evidence"),
        CodeReviewIssue(severity="high", category="spec-compliance", description="c2 :: no evidence"),
    ]
    out = derive_per_criterion(["c1", "c2"], issues)
    assert all(not c.satisfied for c in out)


def test_derive_per_criterion_prefix_match_ignores_whitespace_and_case() -> None:
    issues = [
        CodeReviewIssue(severity="high", category="spec-compliance", description="  Returns  ZERO  :: d")
    ]
    out = derive_per_criterion(["returns zero"], issues)
    assert out[0].satisfied is False


def test_derive_per_criterion_all_unmet() -> None:
    issues = [CodeReviewIssue(severity="high", category="spec-compliance", description="x :: d")]
    out = derive_per_criterion(["x"], issues)
    assert out[0].satisfied is False


def test_derive_per_criterion_criterion_only_description_uses_full_text_as_evidence() -> None:
    # No " :: " delimiter: the whole description is the criterion, and the
    # evidence falls back to that text.
    issues = [CodeReviewIssue(severity="high", category="spec-compliance", description="x")]
    out = derive_per_criterion(["x"], issues)
    assert out[0].satisfied is False
    assert out[0].evidence == "x"


def test_derive_per_criterion_criterion_containing_delimiter() -> None:
    # MEDIUM regression: a criterion that itself contains " :: " must still be
    # attributed correctly (longest-match treats it as a literal prefix), so the
    # unmet criterion is caught rather than silently passing.
    criteria = ["config :: value works", "other"]
    issues = [
        CodeReviewIssue(
            severity="high",
            category="spec-compliance",
            description="config :: value works :: not parsed",
        )
    ]
    out = derive_per_criterion(criteria, issues)
    by = {c.criterion: c for c in out}
    assert by["config :: value works"].satisfied is False
    assert by["config :: value works"].evidence == "not parsed"
    assert by["other"].satisfied is True


def test_derive_per_criterion_empty_tail_evidence_is_unmet() -> None:
    # LOW regression: a delimiter with an empty tail must not echo the criterion
    # prefix as evidence; it reports "Unmet".
    issues = [CodeReviewIssue(severity="high", category="spec-compliance", description="x :: ")]
    out = derive_per_criterion(["x"], issues)
    assert out[0].satisfied is False
    assert out[0].evidence == "Unmet"


def test_derive_per_criterion_blank_criterion_never_matches() -> None:
    # A blank criterion must not spuriously match an empty category.
    issues = [CodeReviewIssue(severity="high", category="", description="")]
    out = derive_per_criterion([""], issues)
    assert out[0].satisfied is True
