"""Tests for the best-effort systemic/cross-cutting findings synthesis pass.

``synthesize_systemic_findings`` must degrade to ``[]`` whenever there are too
few findings or no usable client, make at most one LLM call, resolve reported
``finding_indices`` back to concrete locations, and never raise regardless of
what the model returns.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from code_review_agent.models import CodeReviewIssue
from code_review_agent.systemic_synthesis import (
    MIN_FINDINGS_FOR_SYNTHESIS,
    _parse_systemic_findings,
    synthesize_systemic_findings,
)

from llm_service.clients.dummy import DummyLLMClient


def _issue(
    *,
    file_path: str = "a.py",
    line: Optional[int] = 1,
    category: str = "logic",
    description: str = "bug",
) -> CodeReviewIssue:
    return CodeReviewIssue(
        severity="high",
        category=category,
        file_path=file_path,
        line=line,
        description=description,
        suggestion="fix",
    )


class _Stub(DummyLLMClient):
    """A scripted dummy: subclassing defeats the unscripted-dummy no-op check."""

    def __init__(self, responder: Any) -> None:
        super().__init__()
        self._responder = responder
        self.calls: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        self.calls.append(prompt)
        return self._responder(prompt)


# --------------------------------------------------------------------------- #
# Short-circuits
# --------------------------------------------------------------------------- #


def test_empty_issues_returns_empty() -> None:
    assert synthesize_systemic_findings([]) == []


def test_single_issue_returns_empty_no_call() -> None:
    """A systemic pattern needs >= 2 findings by definition; no LLM call happens."""
    stub = _Stub(lambda _p: {"systemic_findings": []})
    out = synthesize_systemic_findings([_issue()], llm=stub)
    assert out == []
    assert stub.calls == []


def test_plain_dummy_makes_no_call() -> None:
    out = synthesize_systemic_findings([_issue(), _issue()], llm=DummyLLMClient())
    assert out == []


def test_missing_client_returns_empty() -> None:
    """No caller-supplied client (llm=None) degrades to []; the pass does not
    self-resolve a client — the caller owns model resolution."""
    out = synthesize_systemic_findings([_issue(), _issue()])
    assert out == []


def test_min_findings_constant_is_two() -> None:
    assert MIN_FINDINGS_FOR_SYNTHESIS == 2


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_synthesis_resolves_finding_indices_to_locations() -> None:
    def _responder(_prompt: str) -> Dict[str, Any]:
        return {
            "systemic_findings": [
                {
                    "title": "Missing validation repeated",
                    "description": "Three call sites skip input validation.",
                    "finding_indices": [0, 2],
                }
            ]
        }

    stub = _Stub(_responder)
    issues = [
        _issue(file_path="a.py", description="missing validation in f"),
        _issue(file_path="b.py", description="unrelated"),
        _issue(file_path="c.py", description="missing validation in g"),
    ]
    out = synthesize_systemic_findings(issues, llm=stub)
    assert len(stub.calls) == 1
    assert out == [
        {
            "title": "Missing validation repeated",
            "description": "Three call sites skip input validation.",
            "related_locations": [
                {"file_path": "a.py", "description": "missing validation in f"},
                {"file_path": "c.py", "description": "missing validation in g"},
            ],
        }
    ]


def test_synthesis_scrubs_tokens_from_title_description_and_locations() -> None:
    """review_summary.systemic_findings is persisted and served through the Code
    Review API/UI, not just posted (already-scrubbed) as a GitHub comment --
    so a credential-bearing remote URL in the model's own title/description,
    or in a finding's description carried into a related location, must be
    redacted before this ever reaches storage (mirrors proposal_from_findings)."""
    leaky = "https://user:hunter2@github.com/acme/widgets.git failed"

    def _responder(_prompt: str) -> Dict[str, Any]:
        return {
            "systemic_findings": [
                {
                    "title": f"Leaked in title: {leaky}",
                    "description": f"Leaked in description: {leaky}",
                    "finding_indices": [0, 1],
                }
            ]
        }

    stub = _Stub(_responder)
    issues = [
        _issue(file_path="a.py", description=f"leaked in finding: {leaky}"),
        _issue(file_path="b.py", description="unrelated"),
    ]
    out = synthesize_systemic_findings(issues, llm=stub)
    assert len(out) == 1
    entry = out[0]
    assert "hunter2" not in entry["title"]
    assert "hunter2" not in entry["description"]
    assert "https://***@" in entry["title"]
    assert "https://***@" in entry["description"]
    assert "hunter2" not in entry["related_locations"][0]["description"]
    assert "https://***@" in entry["related_locations"][0]["description"]


def test_empty_systemic_findings_is_a_normal_result() -> None:
    """No genuine cross-cutting pattern is the common, correct answer."""
    stub = _Stub(lambda _p: {"systemic_findings": []})
    out = synthesize_systemic_findings([_issue(), _issue()], llm=stub)
    assert out == []


def test_max_findings_in_prompt_caps_inlined_findings() -> None:
    """More findings than the prompt cap still succeeds, using only a prefix."""
    from code_review_agent.systemic_synthesis import _MAX_FINDINGS_IN_PROMPT

    captured: List[str] = []

    def _responder(prompt: str) -> Dict[str, Any]:
        captured.append(prompt)
        return {"systemic_findings": []}

    stub = _Stub(_responder)
    issues = [
        _issue(file_path=f"f{i}.py", description=f"issue {i}")
        for i in range(_MAX_FINDINGS_IN_PROMPT + 10)
    ]
    synthesize_systemic_findings(issues, llm=stub)
    assert len(captured) == 1
    assert f"finding index {_MAX_FINDINGS_IN_PROMPT}" not in captured[0]


# --------------------------------------------------------------------------- #
# Fail-safe
# --------------------------------------------------------------------------- #


def test_llm_exception_degrades_to_empty() -> None:
    def _responder(_prompt: str) -> Dict[str, Any]:
        raise RuntimeError("model exploded")

    stub = _Stub(_responder)
    out = synthesize_systemic_findings([_issue(), _issue()], llm=stub)
    assert out == []


def test_malformed_reply_degrades_to_empty() -> None:
    stub = _Stub(lambda _p: {"unexpected": "shape"})
    out = synthesize_systemic_findings([_issue(), _issue()], llm=stub)
    assert out == []


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #


def test_parse_systemic_findings_defensive() -> None:
    issues = [_issue(file_path="a.py"), _issue(file_path="b.py"), _issue(file_path="c.py")]

    # Non-dict / non-list payloads yield [].
    assert _parse_systemic_findings("not a dict", issues) == []
    assert _parse_systemic_findings({"systemic_findings": "nope"}, issues) == []
    assert _parse_systemic_findings({"systemic_findings": [42, "x"]}, issues) == []

    # Missing title/description drops the entry.
    assert (
        _parse_systemic_findings(
            {"systemic_findings": [{"description": "d", "finding_indices": [0, 1]}]}, issues
        )
        == []
    )
    assert (
        _parse_systemic_findings(
            {"systemic_findings": [{"title": "t", "finding_indices": [0, 1]}]}, issues
        )
        == []
    )

    # Fewer than two valid, in-range, non-duplicate indices drops the entry.
    assert (
        _parse_systemic_findings(
            {"systemic_findings": [{"title": "t", "description": "d", "finding_indices": [0]}]},
            issues,
        )
        == []
    )
    assert (
        _parse_systemic_findings(
            {"systemic_findings": [{"title": "t", "description": "d", "finding_indices": [0, 0]}]},
            issues,
        )
        == []
    )
    assert (
        _parse_systemic_findings(
            {"systemic_findings": [{"title": "t", "description": "d", "finding_indices": [0, 99]}]},
            issues,
        )
        == []
    )
    assert (
        _parse_systemic_findings(
            {
                "systemic_findings": [
                    {"title": "t", "description": "d", "finding_indices": [0, True]}
                ]
            },
            issues,
        )
        == []
    )

    # Out-of-range/duplicate indices are dropped but valid ones still count.
    parsed = _parse_systemic_findings(
        {
            "systemic_findings": [
                {
                    "title": "t",
                    "description": "d",
                    "finding_indices": [0, 0, 1, 99],
                }
            ]
        },
        issues,
    )
    assert parsed == [
        {
            "title": "t",
            "description": "d",
            "related_locations": [
                {"file_path": "a.py", "description": "bug"},
                {"file_path": "b.py", "description": "bug"},
            ],
        }
    ]
