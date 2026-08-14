"""Tests for the code-review scope-verification pass.

Findings that are not confidently about the change under review must not be
posted as PR comments. This module tests the posting-eligibility tagging
(``pre_existing=True`` routes to issue proposals) independently of GitHub.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from code_review_agent.models import CodeReviewIssue
from code_review_agent.scope_filter import (
    ScopeVerdict,
    apply_scope_verdicts,
    apply_scope_verification,
)

from llm_service.clients.dummy import DummyLLMClient


def _issue(
    *,
    file_path: str = "a.py",
    line: Optional[int] = 1,
    description: str = "bug",
    pre_existing: bool = False,
) -> CodeReviewIssue:
    return CodeReviewIssue(
        severity="high",
        category="logic",
        file_path=file_path,
        line=line,
        description=description,
        suggestion="fix",
        pre_existing=pre_existing,
    )


def _changed(path: str = "a.py", lines: Optional[List[int]] = None) -> Dict[str, List[int]]:
    return {path: list(lines if lines is not None else [1, 2])}


def test_added_line_stays_in_scope_even_without_verdict() -> None:
    """A finding on a line this PR added is in-scope; missing verdicts cannot tag it out."""
    issue = _issue(line=2, description="on added line")
    out = apply_scope_verdicts(
        [issue], changed_by_path=_changed(lines=[2]), verdicts={}, grounded=True
    )
    assert out[0].pre_existing is False


def test_unchanged_context_unsure_is_not_posted() -> None:
    """Fail closed for posting: unsure on an unchanged line → pre_existing."""
    issue = _issue(line=99, description="old helper is messy")
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="unsure", confidence="low")},
        grounded=True,
    )
    assert out[0].pre_existing is True


def test_missing_verdict_on_off_diff_is_not_posted() -> None:
    issue = _issue(line=99, description="context nit")
    out = apply_scope_verdicts(
        [issue], changed_by_path=_changed(lines=[1]), verdicts={}, grounded=True
    )
    assert out[0].pre_existing is True


def test_confident_out_of_scope_when_grounded_is_not_posted() -> None:
    issue = _issue(line=50, description="pre-existing naming")
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="out_of_scope", confidence="high")},
        grounded=True,
    )
    assert out[0].pre_existing is True


def test_ungrounded_out_of_scope_does_not_strip_in_scope_finding() -> None:
    """An ungrounded OOS verdict must not convert a default in-scope finding."""
    issue = _issue(line=50, description="might be in scope", pre_existing=False)
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="out_of_scope", confidence="high")},
        grounded=False,
    )
    assert out[0].pre_existing is False


def test_omission_off_diff_stays_in_scope() -> None:
    issue = _issue(file_path="missing.py", line=1, description="PR should have added missing.py")
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="omission", confidence="high")},
        grounded=True,
    )
    assert out[0].pre_existing is False


def test_confident_in_scope_on_off_diff_file_stays_postable() -> None:
    issue = _issue(file_path="other.py", line=3, description="should have updated other.py")
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="in_scope", confidence="medium")},
        grounded=True,
    )
    assert out[0].pre_existing is False


def test_low_confidence_in_scope_fails_closed() -> None:
    issue = _issue(line=50, description="maybe related")
    out = apply_scope_verdicts(
        [issue],
        changed_by_path=_changed(lines=[1]),
        verdicts={0: ScopeVerdict(scope="in_scope", confidence="low")},
        grounded=True,
    )
    assert out[0].pre_existing is True


def test_plain_dummy_client_is_a_noop() -> None:
    """The unscripted dummy harness must not fail-close every PR finding in tests."""
    issue = _issue(line=99, description="context", pre_existing=False)
    out = apply_scope_verification(
        DummyLLMClient(),
        issues=[issue],
        changed_by_path=_changed(lines=[1]),
        files={"a.py": "x = 1\n"},
    )
    assert out[0].pre_existing is False
    assert out[0].description == issue.description


def test_scripted_stub_marks_out_of_scope(monkeypatch: Any) -> None:
    class _Stub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "scope_verdicts" in prompt.lower() or "scope" in prompt.lower():
                return {
                    "verdicts": [
                        {
                            "index": 0,
                            "scope": "out_of_scope",
                            "confidence": "high",
                            "reasoning": "unchanged helper",
                        }
                    ]
                }
            return super().complete_json(prompt, **kwargs)

    # Pretend the run was grounded so a confident OOS tag is honored.
    import code_review_agent.scope_filter as sf

    monkeypatch.setattr(sf, "_scope_run_was_grounded", lambda *a, **k: True)

    issue = _issue(line=50, description="old helper")
    out = apply_scope_verification(
        _Stub(),
        issues=[issue],
        changed_by_path=_changed(lines=[1]),
        files={"a.py": "def old():\n    pass\n+\n"},
    )
    assert out[0].pre_existing is True


def test_empty_issues_and_empty_changed_map_are_noop() -> None:
    assert (
        apply_scope_verification(
            DummyLLMClient(), issues=[], changed_by_path=_changed(), files={"a.py": "x"}
        )
        == []
    )
    issue = _issue()
    out = apply_scope_verification(
        DummyLLMClient(), issues=[issue], changed_by_path={}, files={"a.py": "x"}
    )
    assert out[0].pre_existing is False


def test_env_disable_is_noop(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODE_REVIEW_SCOPE_FILTER", "false")

    class _Stub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            raise AssertionError("disabled filter must not call the LLM")

    issue = _issue(line=99)
    out = apply_scope_verification(
        _Stub(), issues=[issue], changed_by_path=_changed(lines=[1]), files={"a.py": "x"}
    )
    assert out[0].pre_existing is False


def test_setup_failure_leaves_tags_unchanged(monkeypatch: Any) -> None:
    import code_review_agent.scope_filter as sf

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(sf, "_verify_scope", _boom)

    class _Stub(DummyLLMClient):
        pass

    issue = _issue(line=99, pre_existing=False)
    out = apply_scope_verification(
        _Stub(), issues=[issue], changed_by_path=_changed(lines=[1]), files={"a.py": "x"}
    )
    assert out[0].pre_existing is False


def test_parse_scope_verdicts_skips_malformed() -> None:
    from code_review_agent.scope_filter import _parse_scope_verdicts

    assert _parse_scope_verdicts("nope", 1) == {}
    assert _parse_scope_verdicts({"verdicts": "nope"}, 1) == {}
    parsed = _parse_scope_verdicts(
        {
            "verdicts": [
                "skip",
                {"index": True, "scope": "unsure"},
                {"index": -1, "scope": "unsure"},
                {"index": 99, "scope": "unsure"},
                {"index": 0, "scope": "in_scope", "confidence": "high"},
                {"index": 0, "scope": "out_of_scope", "confidence": "high"},
            ]
        },
        1,
    )
    assert parsed[0].scope == "in_scope"


def test_wrapped_exact_dummy_is_noop() -> None:
    class _Wrap:
        client = DummyLLMClient()

    issue = _issue(line=99)
    out = apply_scope_verification(
        _Wrap(),  # type: ignore[arg-type]
        issues=[issue],
        changed_by_path=_changed(lines=[1]),
        files={"a.py": "x"},
    )
    assert out[0].pre_existing is False


def test_empty_files_fail_closes_off_diff() -> None:
    class _Stub(DummyLLMClient):
        pass

    issue = _issue(line=99)
    out = apply_scope_verification(
        _Stub(), issues=[issue], changed_by_path=_changed(lines=[1]), files={}
    )
    assert out[0].pre_existing is True


def test_scope_grounding_list_files_and_none() -> None:
    from code_review_agent.false_positive_filter import CodebaseIndex
    from code_review_agent.models import CodeReviewInput
    from code_review_agent.scope_filter import _scope_run_was_grounded

    index = CodebaseIndex.from_input(CodeReviewInput(files={"a.py": "x = 1\n"}))
    assert _scope_run_was_grounded(None, index, "missing.py") is False

    class _Agent:
        messages = [{"content": [{"toolUse": {"name": "list_files"}}]}]

    assert _scope_run_was_grounded(_Agent(), index, "missing.py") is True
    assert _scope_run_was_grounded(_Agent(), index, "a.py") is False

    class _NoList:
        messages = [
            "skip",
            {"content": "not-a-list"},
            {"content": ["x", {"toolUse": "not-a-dict"}]},
            {"content": [{"toolUse": {"name": "read_file"}}]},
        ]

    assert _scope_run_was_grounded(_NoList(), index, "missing.py") is False

    class _Bad:
        @property
        def messages(self) -> Any:
            raise RuntimeError("no")

    assert _scope_run_was_grounded(_Bad(), index, "missing.py") is False


def test_ungrounded_oos_from_llm_is_preserved(monkeypatch: Any) -> None:
    class _Stub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            return {
                "verdicts": [
                    {
                        "index": 0,
                        "scope": "out_of_scope",
                        "confidence": "high",
                        "reasoning": "guess",
                    }
                ]
            }

    import code_review_agent.scope_filter as sf

    monkeypatch.setattr(sf, "_scope_run_was_grounded", lambda *a, **k: False)
    issue = _issue(line=50, description="keep me", pre_existing=False)
    out = apply_scope_verification(
        _Stub(), issues=[issue], changed_by_path=_changed(lines=[1]), files={"a.py": "x = 1\n"}
    )
    assert out[0].pre_existing is False
