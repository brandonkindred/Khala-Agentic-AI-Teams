"""Tests for the merged architecture-consistency + side-effect-impact pass."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from code_review_agent.merged_architecture_side_effect_pass import (
    find_architecture_and_side_effect_issues,
)
from code_review_agent.models import CodeReviewInput
from code_review_agent.profiles import ReviewProfile

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.models import SystemArchitecture

_MERGED_PASS_ANCHOR = '"architecture_findings"/"side_effect_findings"'


def _input(
    files: Optional[Dict[str, str]] = None,
    *,
    architecture: Optional[SystemArchitecture] = None,
    profile: ReviewProfile = ReviewProfile.CODE_REVIEW,
) -> CodeReviewInput:
    return CodeReviewInput(
        files=files if files is not None else {"app/main.py": "def bar():\n    return 1\n"},
        task_description="wire up bar",
        architecture=architecture,
        profile=profile,
    )


def test_returns_empty_when_both_env_flags_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")

    class _FailIfAsked(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _MERGED_PASS_ANCHOR not in prompt, "merged pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_FailIfAsked(), _input())
    assert arch == []
    assert side == []


def test_returns_empty_for_non_code_review_profile() -> None:
    class _FailIfAsked(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _MERGED_PASS_ANCHOR not in prompt, "merged pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _FailIfAsked(), _input(profile=ReviewProfile.ACCEPTANCE)
    )
    assert arch == []
    assert side == []


def test_returns_empty_when_no_readable_files() -> None:
    arch, side = find_architecture_and_side_effect_issues(
        DummyLLMClient(), _input(files={"empty.py": "   "})
    )
    assert arch == []
    assert side == []


def test_splits_merged_response_into_two_finding_lists() -> None:
    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "bypasses the repository layer",
                            "suggestion": "use the repository",
                        }
                    ],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "caller at other.py:3 assumes ValueError",
                            "suggestion": "update the caller",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_FindingsClient(), _input())
    assert len(arch) == 1
    assert arch[0].category == "architecture"
    assert arch[0].description == "bypasses the repository layer"
    assert len(side) == 1
    assert side[0].category == "side-effects"
    assert "other.py:3" in side[0].description


def test_fails_safe_on_llm_error() -> None:
    class _Raiser(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("boom")

    arch, side = find_architecture_and_side_effect_issues(_Raiser(), _input())
    assert arch == []
    assert side == []


def test_missing_half_key_does_not_discard_the_other_half() -> None:
    """A missing/malformed sibling key must not wipe valid findings from the
    other half — halves are validated independently."""

    class _Partial(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "bypasses the repository layer",
                            "suggestion": "use the repository",
                        }
                    ]
                    # side_effect_findings deliberately omitted
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_Partial(), _input())
    assert len(arch) == 1
    assert arch[0].category == "architecture"
    assert side == []


def test_half_parse_failure_does_not_discard_the_other_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising validate/parse on one half must not wipe valid findings from the other."""
    import code_review_agent.side_effect_impact_pass as side_pass

    class _Partial(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [
                        {
                            "severity": "medium",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "crosses a service boundary",
                            "suggestion": "keep the call in the application layer",
                        }
                    ],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "breaks a caller",
                            "suggestion": "update the caller",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    def _boom(*_a, **_k):
        raise ValueError("malformed side-effect half")

    monkeypatch.setattr(side_pass, "validate_findings", _boom)
    arch, side = find_architecture_and_side_effect_issues(_Partial(), _input())
    assert len(arch) == 1
    assert arch[0].category == "architecture"
    assert side == []


def test_fails_safe_on_non_object_reply() -> None:
    class _Gibberish(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return "not even a dict-shaped reply"  # type: ignore[return-value]
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_Gibberish(), _input())
    assert arch == []
    assert side == []


def test_runs_without_architecture_document() -> None:
    prompts: list = []

    class _EmptyFindings(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _EmptyFindings(), _input(architecture=None)
    )
    assert arch == []
    assert side == []
    assert len(prompts) == 1
    assert "no formal" in prompts[0].lower() or "not provided" in prompts[0].lower()


def test_discards_disabled_half_when_only_one_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "true")

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "should be discarded",
                            "suggestion": "n/a",
                        }
                    ],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "documentation",
                            "file_path": "app/main.py",
                            "description": "docstring says X but code does Y",
                            "suggestion": "fix the docstring",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_FindingsClient(), _input())
    assert arch == []
    assert len(side) == 1
    assert side[0].category == "documentation"


def test_skips_side_effect_half_when_pre_numbered() -> None:
    """Pre-numbered hunk mode keeps architecture findings but must not emit
    side-effect findings (same guard as the standalone side-effect pass)."""
    prompts: list = []

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {
                    "architecture_findings": [
                        {
                            "severity": "medium",
                            "category": "refactor",
                            "file_path": "app/main.py",
                            "description": "duplicates an existing helper",
                            "suggestion": "reuse the helper",
                        }
                    ],
                    "side_effect_findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "should be discarded in pre_numbered mode",
                            "suggestion": "n/a",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={"app/main.py": "4242: def bar():\n4243:     return 1\n"},
            task_description="wire up bar",
            pre_numbered=True,
        ),
    )
    assert len(prompts) == 1
    assert len(arch) == 1
    assert arch[0].category == "refactor"
    assert side == []
