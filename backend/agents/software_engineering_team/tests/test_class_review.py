"""Tests for the class-cohesion review pass (``code_review_agent.class_review``).

The pass runs one bounded LLM review per class, emits advisory findings
(severity capped at ``medium``), and is env-gated + size-capped. It never raises
into the coordinator: a per-class failure drops only that class's findings.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from code_review_agent.class_review import (
    _cap_severity,
    _render_class_prompt,
    clear_cohesion_cache,
    review_class_cohesion,
)
from code_review_agent.code_units import extract_classes
from code_review_agent.models import CodeReviewInput

from llm_service.clients.dummy import DummyLLMClient

_CLASS_SRC = '''\
class Report:
    """Builds a sales report."""

    def build(self):
        return self._rows

    def send_email(self, to):
        return smtp_send(to)
'''


class _IssueStub(DummyLLMClient):
    """Returns a canned cohesion issue for every review call."""

    def __init__(self, issues: List[Dict[str, Any]]):
        super().__init__()
        self._issues = issues

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        return {
            "approved": False,
            "issues": self._issues,
            "summary": "cohesion (stub)",
            "spec_compliance_notes": "",
            "suggested_commit_message": "",
        }


class _RaisingStub(DummyLLMClient):
    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        raise RuntimeError("boom")


def _input(**overrides: Any) -> CodeReviewInput:
    base: Dict[str, Any] = {
        "files": {"report.py": _CLASS_SRC},
        "task_description": "build reports",
        "language": "python",
    }
    base.update(overrides)
    return CodeReviewInput(**base)


@pytest.fixture(autouse=True)
def _enable_cohesion(monkeypatch):
    """The pass is default-on; make tests independent of the ambient env.

    Also clears the process-global cohesion outcome cache so each test's stub
    (which simulates a different LLM response for the same class input) is not
    shadowed by a prior test's cached result.
    """
    monkeypatch.delenv("CODE_REVIEW_CLASS_COHESION", raising=False)
    monkeypatch.delenv("CODE_REVIEW_CLASS_COHESION_MAX_CLASSES", raising=False)
    monkeypatch.delenv("CODE_REVIEW_COHESION_CACHE_SIZE", raising=False)
    clear_cohesion_cache()
    yield
    clear_cohesion_cache()


def test_cap_severity_lowers_above_medium() -> None:
    assert _cap_severity("critical") == "medium"
    assert _cap_severity("high") == "medium"
    assert _cap_severity("medium") == "medium"
    assert _cap_severity("low") == "low"
    assert _cap_severity("info") == "info"
    # An unrecognized value is treated as the ceiling (never blocks the gate).
    assert _cap_severity("bogus") == "medium"


def test_render_class_prompt_includes_purpose_and_methods() -> None:
    cu = extract_classes("report.py", _CLASS_SRC)[0]
    text = _render_class_prompt("report.py", cu)
    assert "Report" in text
    assert "Builds a sales report." in text
    assert "def build(self):" in text
    assert "def send_email(self, to):" in text


def test_emits_advisory_finding_per_class() -> None:
    stub = _IssueStub(
        [
            {
                "severity": "critical",
                "category": "structure",
                "description": "god class",
                "suggestion": "split",
            }
        ]
    )
    issues = review_class_cohesion(stub, [("report.py", _CLASS_SRC)], _input())
    assert len(issues) == 1
    (issue,) = issues
    assert issue.file_path == "report.py"
    assert issue.description == "god class"
    # Severity is capped to the advisory ceiling even though the LLM said critical.
    assert issue.severity == "medium"
    assert issue.category == "structure"
    # A cohesion finding anchors to the class: _issues_from_class_output sets
    # issue.line to the class's start line, so it must equal
    # extract_classes(...)[0].start_line exactly (not merely fall within the range).
    assert issue.line == extract_classes("report.py", _CLASS_SRC)[0].start_line


def test_blank_description_dropped() -> None:
    stub = _IssueStub([{"severity": "medium", "description": "  "}])
    assert review_class_cohesion(stub, [("report.py", _CLASS_SRC)], _input()) == []


def test_no_classes_returns_empty_without_llm_call() -> None:
    stub = _IssueStub([{"severity": "high", "description": "should not appear"}])
    # A classless (function-only) block yields no cohesion review at all.
    assert review_class_cohesion(stub, [("f.py", "def f():\n    return 1\n")], _input()) == []


def test_env_disable_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_CLASS_COHESION", "false")
    stub = _IssueStub([{"severity": "high", "description": "x"}])
    assert review_class_cohesion(stub, [("report.py", _CLASS_SRC)], _input()) == []


def test_zero_cap_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_CLASS_COHESION_MAX_CLASSES", "0")
    stub = _IssueStub([{"severity": "high", "description": "x"}])
    assert review_class_cohesion(stub, [("report.py", _CLASS_SRC)], _input()) == []


def test_per_class_failure_is_swallowed() -> None:
    # A verifier/LLM error for a class drops only that class's findings, never raises.
    assert review_class_cohesion(_RaisingStub(), [("report.py", _CLASS_SRC)], _input()) == []


class _CountingStub(DummyLLMClient):
    """Like _IssueStub but counts how many times the LLM was actually invoked."""

    def __init__(self, issues: List[Dict[str, Any]]):
        super().__init__()
        self._issues = issues
        self.calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        self.calls += 1
        return {"approved": False, "issues": self._issues, "summary": "c"}


def test_cohesion_cache_reuses_result() -> None:
    """A second review of the same class + context is served from cache (no LLM call)."""
    stub = _CountingStub([{"severity": "low", "description": "concern"}])
    blocks = [("report.py", _CLASS_SRC)]
    first = review_class_cohesion(stub, blocks, _input())
    second = review_class_cohesion(stub, blocks, _input())
    assert len(first) == 1 and len(second) == 1
    assert first[0].description == second[0].description == "concern"
    assert stub.calls == 1  # second run hit the cache


def test_cohesion_cache_disabled_reruns(monkeypatch) -> None:
    """With the cache disabled (size 0) the class is re-reviewed every run."""
    monkeypatch.setenv("CODE_REVIEW_COHESION_CACHE_SIZE", "0")
    stub = _CountingStub([{"severity": "low", "description": "concern"}])
    blocks = [("report.py", _CLASS_SRC)]
    review_class_cohesion(stub, blocks, _input())
    review_class_cohesion(stub, blocks, _input())
    assert stub.calls == 2  # no caching -> two real calls


def test_multiple_classes_fan_out_in_parallel() -> None:
    """Two classes cause the pass to fan out (ThreadPoolExecutor), each yielding a finding."""
    two = (
        "class A:\n"
        '    """A."""\n'
        "    def a(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        '    """B."""\n'
        "    def b(self):\n"
        "        return 2\n"
    )
    stub = _IssueStub([{"severity": "low", "description": "concern"}])
    issues = review_class_cohesion(stub, [("m.py", two)], _input(files={"m.py": two}))
    assert len(issues) == 2
    assert all(i.file_path == "m.py" for i in issues)


def test_class_cap_truncates_collection(monkeypatch) -> None:
    """The class cap bounds how many classes are reviewed (fan-out control)."""
    monkeypatch.setenv("CODE_REVIEW_CLASS_COHESION_MAX_CLASSES", "1")
    two = "class A:\n    def a(self): return 1\n\nclass B:\n    def b(self): return 2\n"
    stub = _IssueStub([{"severity": "low", "description": "concern"}])
    issues = review_class_cohesion(stub, [("m.py", two)], _input(files={"m.py": two}))
    assert len(issues) == 1  # only the first class reviewed under the cap
