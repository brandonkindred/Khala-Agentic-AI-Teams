"""Tests for the batched, parallel LLM scope-classification pass.

``classify_scope`` must return a per-finding in/out-of-scope verdict aligned 1:1
with its input, batch findings by cited file, fan the batches out in parallel,
and degrade any per-batch failure to the "unknown" verdict without ever raising.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from code_review_agent.scope_classifier import (
    _FILE_EXCERPT_CHARS,
    _FILE_EXCERPT_TRUNCATION_MARKER,
    UNKNOWN,
    ScopeClassification,
    _batches,
    _coerce_in_scope,
    _file_excerpt,
    _max_findings_per_group,
    _parse_classifications,
    classify_scope,
)

from llm_service.clients.dummy import DummyLLMClient


def _issue(
    *,
    file_path: str = "a.py",
    line: Optional[int] = 1,
    description: str = "bug",
) -> CodeReviewIssue:
    return CodeReviewIssue(
        severity="high",
        category="logic",
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
    assert classify_scope([]) == []


def test_plain_dummy_is_all_unknown() -> None:
    """The production dummy makes no calls and yields unknown for every finding."""
    out = classify_scope([_issue(), _issue()], llm=DummyLLMClient())
    assert out == [UNKNOWN, UNKNOWN]
    assert all(v.in_scope is None for v in out)


def test_missing_client_is_all_unknown() -> None:
    """No caller-supplied client (llm=None) degrades to all-unknown, never raises.

    The pass does not self-resolve a client — the caller owns model resolution
    (the code_review_verify model, like the sibling verification passes).
    """
    out = classify_scope([_issue(), _issue()])
    assert out == [UNKNOWN, UNKNOWN]


# --------------------------------------------------------------------------- #
# Classification + batching
# --------------------------------------------------------------------------- #


def test_classifies_in_and_out_of_scope() -> None:
    def _responder(_prompt: str) -> Dict[str, Any]:
        return {
            "verdicts": [
                {"index": 0, "in_scope": True, "reason": "on the changed line"},
                {"index": 1, "in_scope": False, "reason": "pre-existing helper"},
            ]
        }

    stub = _Stub(_responder)
    issues = [_issue(description="new"), _issue(description="old")]
    out = classify_scope(issues, llm=stub)
    assert out[0] == ScopeClassification(in_scope=True, reason="on the changed line")
    assert out[1] == ScopeClassification(in_scope=False, reason="pre-existing helper")


def test_batches_by_file() -> None:
    """Two distinct files produce two separate LLM calls (one batch each)."""

    def _responder(prompt: str) -> Dict[str, Any]:
        # Each per-file batch has exactly one finding at local index 0.
        in_scope = "a.py" in prompt
        return {"verdicts": [{"index": 0, "in_scope": in_scope, "reason": "r"}]}

    stub = _Stub(_responder)
    issues = [_issue(file_path="a.py"), _issue(file_path="b.py")]
    out = classify_scope(issues, llm=stub)
    assert len(stub.calls) == 2
    assert out[0].in_scope is True
    assert out[1].in_scope is False


def test_batching_cap_splits_one_file(monkeypatch: Any) -> None:
    """A cap of 2 splits five findings on one file into three batches."""
    monkeypatch.setenv("CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP", "2")

    def _responder(prompt: str) -> Dict[str, Any]:
        # Mark every local index in this batch in-scope. The batch has at most
        # two findings, so indices 0 and 1 cover it.
        return {
            "verdicts": [
                {"index": 0, "in_scope": True, "reason": "r"},
                {"index": 1, "in_scope": True, "reason": "r"},
            ]
        }

    stub = _Stub(_responder)
    issues = [_issue(file_path="a.py", description=f"f{i}") for i in range(5)]
    out = classify_scope(issues, llm=stub)
    assert len(stub.calls) == 3  # 2 + 2 + 1
    assert len(out) == 5
    assert all(v.in_scope is True for v in out)


def test_max_findings_per_group_override_beats_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP", "10")

    def _responder(_prompt: str) -> Dict[str, Any]:
        return {"verdicts": [{"index": 0, "in_scope": True, "reason": "r"}]}

    stub = _Stub(_responder)
    issues = [_issue(file_path="a.py", description=f"f{i}") for i in range(3)]
    classify_scope(issues, llm=stub, max_findings_per_group=1)
    assert len(stub.calls) == 3  # one batch per finding


# --------------------------------------------------------------------------- #
# Parallelism
# --------------------------------------------------------------------------- #


def test_non_positive_tuning_args_are_floored_not_raised() -> None:
    """A caller-supplied cap/workers below 1 is clamped to 1, never raises."""

    def _responder(_prompt: str) -> Dict[str, Any]:
        return {"verdicts": [{"index": 0, "in_scope": True, "reason": "r"}]}

    issues = [_issue(file_path="a.py", description=f"f{i}") for i in range(3)]

    # cap=0 would otherwise trip _batches' assert (or range(..., 0) ValueError);
    # floored to 1 → one batch per finding, all classified.
    stub = _Stub(_responder)
    out = classify_scope(issues, llm=stub, max_findings_per_group=0)
    assert len(stub.calls) == 3
    assert all(v.in_scope is True for v in out)

    # workers=0 would otherwise raise out of parallel_map; floored to 1.
    stub2 = _Stub(_responder)
    out2 = classify_scope(issues, llm=stub2, max_findings_per_group=1, max_workers=-5)
    assert len(out2) == 3
    assert all(v.in_scope is True for v in out2)


def test_batches_run_in_parallel() -> None:
    """A barrier across three file-batches only clears if they run concurrently."""
    barrier = threading.Barrier(3, timeout=5)

    def _responder(_prompt: str) -> Dict[str, Any]:
        barrier.wait()  # raises BrokenBarrierError on timeout if not concurrent
        return {"verdicts": [{"index": 0, "in_scope": True, "reason": "r"}]}

    stub = _Stub(_responder)
    issues = [_issue(file_path=f"f{i}.py") for i in range(3)]
    out = classify_scope(issues, llm=stub, max_workers=3)
    assert len(out) == 3
    assert all(v.in_scope is True for v in out)


# --------------------------------------------------------------------------- #
# Fail-safe
# --------------------------------------------------------------------------- #


def test_batch_exception_degrades_to_unknown() -> None:
    """A raising LLM call leaves that batch's findings unknown, never raises."""

    def _responder(_prompt: str) -> Dict[str, Any]:
        raise RuntimeError("model exploded")

    stub = _Stub(_responder)
    out = classify_scope([_issue(), _issue()], llm=stub)
    assert out == [UNKNOWN, UNKNOWN]


def test_one_batch_fails_others_survive() -> None:
    """A failure in one file's batch does not affect other files' verdicts."""

    def _responder(prompt: str) -> Dict[str, Any]:
        if "good.py" in prompt:
            return {"verdicts": [{"index": 0, "in_scope": True, "reason": "ok"}]}
        raise RuntimeError("bad batch")

    stub = _Stub(_responder)
    issues = [_issue(file_path="good.py"), _issue(file_path="bad.py")]
    out = classify_scope(issues, llm=stub)
    assert out[0] == ScopeClassification(in_scope=True, reason="ok")
    assert out[1] is UNKNOWN


def test_missing_index_stays_unknown() -> None:
    """A finding the model omits keeps the unknown default."""

    def _responder(_prompt: str) -> Dict[str, Any]:
        return {"verdicts": [{"index": 0, "in_scope": True, "reason": "r"}]}

    stub = _Stub(_responder)
    issues = [_issue(file_path="a.py", description="0"), _issue(file_path="a.py", description="1")]
    out = classify_scope(issues, llm=stub)
    assert out[0].in_scope is True
    assert out[1] is UNKNOWN


# --------------------------------------------------------------------------- #
# Prompt context
# --------------------------------------------------------------------------- #


def test_prompt_includes_task_and_file_context() -> None:
    captured: List[str] = []

    def _responder(prompt: str) -> Dict[str, Any]:
        captured.append(prompt)
        return {"verdicts": [{"index": 0, "in_scope": True, "reason": "r"}]}

    stub = _Stub(_responder)
    input_data = CodeReviewInput(
        files={"a.py": "def f():\n    return 1\n"},
        task_description="Add feature X",
        task_requirements="Must do Y",
        acceptance_criteria=["criterion one"],
    )
    classify_scope([_issue(file_path="a.py")], llm=stub, input_data=input_data)
    prompt = captured[0]
    assert "Add feature X" in prompt
    assert "Must do Y" in prompt
    assert "criterion one" in prompt
    assert "def f():" in prompt


# --------------------------------------------------------------------------- #
# Pure-helper units
# --------------------------------------------------------------------------- #


def test_parse_classifications_defensive() -> None:
    assert _parse_classifications("not a dict", 2) == {}
    assert _parse_classifications({"verdicts": "nope"}, 2) == {}
    assert _parse_classifications({"verdicts": [42, "x"]}, 2) == {}
    # Out-of-range and boolean indices are dropped.
    assert _parse_classifications({"verdicts": [{"index": 5, "in_scope": True}]}, 2) == {}
    assert _parse_classifications({"verdicts": [{"index": True, "in_scope": True}]}, 2) == {}
    # First entry for an index wins; duplicate dropped.
    parsed = _parse_classifications(
        {
            "verdicts": [
                {"index": 0, "in_scope": True, "reason": "first"},
                {"index": 0, "in_scope": False, "reason": "dup"},
            ]
        },
        1,
    )
    assert parsed == {0: ScopeClassification(in_scope=True, reason="first")}


def test_coerce_in_scope_tokens() -> None:
    assert _coerce_in_scope(True) is True
    assert _coerce_in_scope(False) is False
    assert _coerce_in_scope("in_scope") is True
    assert _coerce_in_scope("OUT_OF_SCOPE") is False
    assert _coerce_in_scope("yes") is True
    assert _coerce_in_scope("no") is False
    assert _coerce_in_scope("unknown") is None
    assert _coerce_in_scope(None) is None
    assert _coerce_in_scope(1) is None
    assert _coerce_in_scope("maybe") is None


def test_batches_group_by_file_and_cap() -> None:
    issues = [
        _issue(file_path="a.py"),
        _issue(file_path="b.py"),
        _issue(file_path="a.py"),
        _issue(file_path=""),
    ]
    batches = _batches(issues, max_findings_per_group=1)
    # a.py -> [0], [2]; b.py -> [1]; "" -> "(unknown)" -> [3]
    assert [0] in batches and [2] in batches and [1] in batches and [3] in batches
    assert len(batches) == 4
    # Larger cap groups same-file findings into one batch.
    grouped = _batches(issues, max_findings_per_group=10)
    assert [0, 2] in grouped


def test_file_excerpt_variants() -> None:
    assert _file_excerpt("a.py", None) == ""
    assert _file_excerpt("a.py", {}) == ""
    assert _file_excerpt("a.py", {"a.py": ""}) == ""  # present but empty
    assert _file_excerpt("a.py", {"b.py": "x"}) == ""  # missing key
    assert _file_excerpt("a.py", {"a.py": "short"}) == "short"  # under the cap
    big = "y" * (_FILE_EXCERPT_CHARS + 100)
    out = _file_excerpt("a.py", {"a.py": big})
    assert out == big[:_FILE_EXCERPT_CHARS] + _FILE_EXCERPT_TRUNCATION_MARKER


def test_max_findings_per_group_defensive_default(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP", "not-a-number")
    from code_review_agent.scope_classifier import DEFAULT_SCOPE_MAX_FINDINGS_PER_GROUP

    assert _max_findings_per_group() == DEFAULT_SCOPE_MAX_FINDINGS_PER_GROUP
    monkeypatch.setenv("CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP", "0")
    assert _max_findings_per_group() == 1  # floored
