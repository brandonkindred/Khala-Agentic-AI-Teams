"""Tests for the merged architecture-consistency + side-effect-impact pass."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from code_review_agent.merged_architecture_side_effect_pass import (
    find_architecture_and_side_effect_issues,
)
from code_review_agent.models import CodeReviewInput
from code_review_agent.profiles import ReviewProfile
from tests.submission_pass_two_call_client import SubmissionPassTwoCallClient

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture

pytest_plugins = ["tests.submission_pass_two_call_client"]

_MERGED_PASS_ANCHOR = "Merged submission pass:"

# Default off-diff excerpt so architecture/redundancy half has evidence when
# tests do not attach a repo_reader or formal architecture document.
_DEFAULT_EXISTING_CODEBASE = "existing/shared_helper.py already provides shared helpers\n"


def _input(
    files: Optional[Dict[str, str]] = None,
    *,
    architecture: Optional[SystemArchitecture] = None,
    profile: ReviewProfile = ReviewProfile.CODE_REVIEW,
    existing_codebase: Optional[str] = _DEFAULT_EXISTING_CODEBASE,
) -> CodeReviewInput:
    return CodeReviewInput(
        files=files if files is not None else {"app/main.py": "def bar():\n    return 1\n"},
        task_description="wire up bar",
        architecture=architecture,
        profile=profile,
        existing_codebase=existing_codebase,
    )


def test_returns_empty_when_both_env_flags_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")

    class _FailIfAsked(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _MERGED_PASS_ANCHOR not in self.latest_reasoning_prompt(), "merged pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_FailIfAsked(), _input())
    assert arch == []
    assert side == []


def test_returns_empty_for_non_code_review_profile() -> None:
    class _FailIfAsked(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _MERGED_PASS_ANCHOR not in self.latest_reasoning_prompt(), "merged pass should not run"
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
    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
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


def test_architecture_finding_pre_existing_tag_survives_the_merged_pass() -> None:
    """Regression test: an architecture/refactor finding the model tags
    pre_existing=true (e.g. a field/function untouched by this submission,
    living in a file the submission also changed elsewhere) must carry that
    tag through the merged pass's per-half parsing/validation, matching the
    side-effect half's identical, already-covered behavior -- otherwise the
    PR-review whole-file path can never route it to a human-review proposal
    instead of a blocking PR comment."""

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {
                    "architecture_findings": [
                        {
                            "severity": "medium",
                            "category": "refactor",
                            "file_path": "app/main.py",
                            "description": "duplicates an existing field untouched by this change",
                            "suggestion": "reuse the existing field",
                            "pre_existing": True,
                        }
                    ],
                    "side_effect_findings": [],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_FindingsClient(), _input())
    assert len(arch) == 1
    assert arch[0].pre_existing is True
    assert side == []


def test_fails_safe_on_llm_error() -> None:
    class _Raiser(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("boom")

    arch, side = find_architecture_and_side_effect_issues(_Raiser(), _input())
    assert arch == []
    assert side == []


def test_missing_half_key_does_not_discard_the_other_half() -> None:
    """A missing/malformed sibling key must not wipe valid findings from the
    other half — halves are validated independently."""

    class _Partial(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
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

    class _Partial(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
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
    class _Gibberish(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return "not even a dict-shaped reply"  # type: ignore[return-value]
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(_Gibberish(), _input())
    assert arch == []
    assert side == []


def test_runs_without_architecture_document() -> None:
    prompts: list = []

    class _EmptyFindings(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _EmptyFindings(), _input(architecture=None)
    )
    assert arch == []
    assert side == []
    assert len(prompts) == 1
    assert "no formal" in prompts[0].lower() or "not provided" in prompts[0].lower()


def test_skips_architecture_half_without_repository_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no architecture payload, no repo_reader, and no existing_codebase,
    Part 1 cannot verify established structure — disable it rather than emit
    speculative architecture findings."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    built: Dict[str, Any] = {}
    real_build = pass_mod.build_merged_architecture_side_effect_reasoning_system_prompt

    def _spy(*, arch_on: bool, side_on: bool) -> str:
        built["arch_on"] = arch_on
        built["side_on"] = side_on
        built["prompt"] = real_build(arch_on=arch_on, side_on=side_on)
        return built["prompt"]

    monkeypatch.setattr(
        pass_mod, "build_merged_architecture_side_effect_reasoning_system_prompt", _spy
    )

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {
                    "architecture_findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "should be discarded — no evidence for Part 1",
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

    arch, side = find_architecture_and_side_effect_issues(
        _Client(),
        _input(architecture=None, existing_codebase=""),
    )
    assert built["arch_on"] is False
    assert built["side_on"] is True
    assert arch == []
    assert len(side) == 1
    assert "Part 1: Architecture" not in built["prompt"]


def test_discards_disabled_half_when_only_one_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "true")
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    built: Dict[str, Any] = {}
    real_build = pass_mod.build_merged_architecture_side_effect_reasoning_system_prompt

    def _spy(*, arch_on: bool, side_on: bool) -> str:
        built["arch_on"] = arch_on
        built["side_on"] = side_on
        built["prompt"] = real_build(arch_on=arch_on, side_on=side_on)
        return built["prompt"]

    monkeypatch.setattr(
        pass_mod, "build_merged_architecture_side_effect_reasoning_system_prompt", _spy
    )

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
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
    assert built == {
        "arch_on": False,
        "side_on": True,
        "prompt": built["prompt"],
    }
    assert "Part 1: Architecture" not in built["prompt"]
    assert "Part 2: Side-Effect" in built["prompt"]
    assert "Do NOT perform architecture-consistency" in built["prompt"]


def test_skips_side_effect_half_when_pre_numbered() -> None:
    """Pre-numbered hunk mode keeps architecture findings but must not emit
    side-effect findings (same guard as the standalone side-effect pass)."""
    prompts: list = []

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
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
            existing_codebase=_DEFAULT_EXISTING_CODEBASE,
        ),
    )
    assert len(prompts) == 1
    assert len(arch) == 1
    assert arch[0].category == "refactor"
    assert side == []


def test_side_effect_half_runs_when_pre_numbered_with_full_content_supplied() -> None:
    """A caller that supplies ``full_content`` alongside ``pre_numbered=True`` has
    given the coordinator real full bodies for the changed paths -- the side-effect
    half must run (same re-enable condition as the standalone pass), not be
    silently discarded as unverifiable hunk-fallback mode."""
    prompts: list = []

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {
                    "architecture_findings": [],
                    "side_effect_findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "bar() behavior changed",
                            "suggestion": "check callers",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={"app/main.py": "4242: def bar():\n4243:     return 1\n"},
            pre_numbered=True,
            full_content={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            existing_codebase=_DEFAULT_EXISTING_CODEBASE,
        ),
    )
    assert len(prompts) == 1
    assert len(side) == 1
    assert side[0].category == "side-effects"


def test_side_effect_half_stays_off_when_full_content_covers_only_some_paths() -> None:
    """``full_content`` that covers only SOME of the submission's changed paths
    must NOT re-enable the side-effect half: overlaying just the covered subset
    would leave the rest as bounded ``N: ``-prefixed excerpts sitting alongside
    full bodies, with no way for the pass to tell them apart. The architecture
    half (unaffected by ``pre_numbered``) may still run."""
    prompts: list = []

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {
                    "architecture_findings": [],
                    "side_effect_findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "should never be emitted -- half is off",
                            "suggestion": "n/a",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch, side = find_architecture_and_side_effect_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={
                "app/main.py": "4242: def bar():\n4243:     return 1\n",
                "app/util.py": "1: def helper():\n2:     return 2\n",
            },
            pre_numbered=True,
            # Covers only app/main.py, not app/util.py -- partial coverage.
            full_content={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            existing_codebase=_DEFAULT_EXISTING_CODEBASE,
        ),
    )
    assert side == []


def test_large_architecture_document_shrinks_code_inline_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the standalone architecture pass, the merged call must reserve
    the architecture body in its budget so a huge document shrinks changed-file
    inlining (and caps the document on tiny contexts) instead of overflowing."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    captured: Dict[str, Any] = {}
    original_build = pass_mod._build_prompt

    def _spy(index, architecture_body, max_inline_chars, **kwargs):
        captured["max_inline_chars"] = max_inline_chars
        captured["max_architecture_chars"] = kwargs["max_architecture_chars"]
        captured["architecture_body_len"] = len(architecture_body)
        return original_build(index, architecture_body, max_inline_chars, **kwargs)

    monkeypatch.setattr(pass_mod, "_build_prompt", _spy)

    # Small context so a 100K architecture body cannot share the map-call code
    # allowance without overflowing.
    class _SmallCtx(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            return 16_384

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

    map_budget = compute_code_review_map_chunk_chars(_SmallCtx())
    arch_doc = SystemArchitecture(overview="", architecture_document="X" * 100_000)
    find_architecture_and_side_effect_issues(_SmallCtx(), _input(architecture=arch_doc))

    assert captured["architecture_body_len"] == 100_000
    assert captured["max_architecture_chars"] < 100_000
    assert captured["max_inline_chars"] < map_budget
    assert captured["max_inline_chars"] == 0


def test_skips_call_when_context_cannot_fit_fixed_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undersized context that cannot hold the system prompt + response
    reserve must skip the LLM call rather than inventing inline capacity."""
    calls = {"n": 0}

    class _TinyCtx(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            return 2_048

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            calls["n"] += 1
            raise AssertionError("merged pass should not call the LLM")

    arch, side = find_architecture_and_side_effect_issues(_TinyCtx(), _input())
    assert arch == []
    assert side == []
    assert calls["n"] == 0


def test_truncates_large_changed_file_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def get_max_context_tokens(self) -> int:
            # Fits fixed prompt + tool transcript + a usable (shrunk) response,
            # but leaves essentially no content room so the path list truncates.
            return 18_000

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {f"pkg/module_{i:04d}.py": f"def f{i}():\n    return {i}\n" for i in range(400)}
    find_architecture_and_side_effect_issues(_Client(), _input(files=files))
    assert prompts
    manifest_section = prompts[0].split("**Full content of the changed files:**", 1)[0]
    assert "more changed path(s) not listed" in manifest_section
    assert "list_changed_files" in manifest_section
    assert manifest_section.count("pkg/module_") < 400


def test_raises_tight_output_cap_for_dual_finding_arrays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM_MAX_OUTPUT_TOKENS is set near a single-pass sizing (e.g. 4096), the
    production LLMClientModel path must raise the merged call to the dual-array
    floor so both finding lists can fit in one completion."""
    import code_review_agent.submission_pass_runner as runner_mod

    from llm_service import LLMClientModel
    from software_engineering_team.shared.context_sizing import (
        CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    )

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    clones: list = []

    class _Empty(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            # Dummy default is 16K; with tool-transcript headroom that shrinks
            # the dual-array reserve. Use a window that still holds the full floor.
            return 40_000

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    class _RecordingModel(LLMClientModel):
        def clone(self, **overrides: Any) -> LLMClientModel:  # type: ignore[override]
            clones.append(overrides)
            return super().clone(**overrides)

    backing = _Empty()
    base = _RecordingModel(backing, agent_key="code_review")
    monkeypatch.setattr(runner_mod, "resolve_code_review_model", lambda _llm: base)

    find_architecture_and_side_effect_issues(backing, _input())
    assert clones == [{"max_tokens": CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS}]


def test_model_pin_takes_precedence_over_env_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned max_tokens of 4096 must be raised even when LLM_MAX_OUTPUT_TOKENS is
    already generous — the pin is what the adapter actually sends."""
    import code_review_agent.submission_pass_runner as runner_mod

    from llm_service import LLMClientModel
    from software_engineering_team.shared.context_sizing import (
        CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    )

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    clones: list = []

    class _Empty(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            return 40_000

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    class _RecordingModel(LLMClientModel):
        def clone(self, **overrides: Any) -> LLMClientModel:  # type: ignore[override]
            clones.append(overrides)
            return super().clone(**overrides)

    backing = _Empty()
    base = _RecordingModel(backing, agent_key="code_review", max_tokens=4096)
    monkeypatch.setattr(runner_mod, "resolve_code_review_model", lambda _llm: base)

    find_architecture_and_side_effect_issues(backing, _input())
    assert clones == [{"max_tokens": CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS}]


def test_clamps_oversized_cap_to_shrunk_response_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the response reserve shrinks for a small context, an oversized
    LLM_MAX_OUTPUT_TOKENS must be clamped down to that reserve."""
    import code_review_agent.submission_pass_runner as runner_mod

    from llm_service import LLMClientModel
    from software_engineering_team.shared.context_sizing import (
        _CODE_REVIEW_MERGED_PASS_MIN_RESPONSE_TOKENS,
        CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    )

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    clones: list = []

    class _SmallCtx(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            # Large enough to run (fixed prompt + transcript + min response),
            # small enough that the dual-array floor cannot fit in full.
            return 18_000

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    class _RecordingModel(LLMClientModel):
        def clone(self, **overrides: Any) -> LLMClientModel:  # type: ignore[override]
            clones.append(overrides)
            return super().clone(**overrides)

    backing = _SmallCtx()
    base = _RecordingModel(backing, agent_key="code_review")
    monkeypatch.setattr(runner_mod, "resolve_code_review_model", lambda _llm: base)

    find_architecture_and_side_effect_issues(backing, _input())
    assert len(clones) == 1
    assert clones[0]["max_tokens"] < CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS
    assert clones[0]["max_tokens"] >= _CODE_REVIEW_MERGED_PASS_MIN_RESPONSE_TOKENS


def test_clamps_oversized_cap_to_full_dual_array_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the dual-array floor is selected, an oversized LLM_MAX_OUTPUT_TOKENS
    must still be clamped to that reserved response budget."""
    import code_review_agent.submission_pass_runner as runner_mod

    from llm_service import LLMClientModel
    from software_engineering_team.shared.context_sizing import (
        CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    )

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    clones: list = []

    class _Empty(DummyLLMClient):
        def get_max_context_tokens(self) -> int:
            return 40_000

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    class _RecordingModel(LLMClientModel):
        def clone(self, **overrides: Any) -> LLMClientModel:  # type: ignore[override]
            clones.append(overrides)
            return super().clone(**overrides)

    backing = _Empty()
    base = _RecordingModel(backing, agent_key="code_review")
    monkeypatch.setattr(runner_mod, "resolve_code_review_model", lambda _llm: base)

    find_architecture_and_side_effect_issues(backing, _input())
    assert clones == [{"max_tokens": CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS}]


def test_truncation_note_is_reserved_so_large_file_still_inlines() -> None:
    """A file larger than the inline budget must keep a prefix plus note, not
    drop the whole file (and later files) because the note overflowed."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    index = CodebaseIndex.from_input(
        _input(files={"big.py": "X" * 5_000, "small.py": "def ok():\n    return 1\n"})
    )
    prompt = pass_mod._build_prompt(
        index,
        "",
        400,
        max_architecture_chars=0,
        max_manifest_chars=2_000,
        arch_on=False,
        side_on=True,
    )
    assert "### big.py ###" in prompt
    assert "Only the first" in prompt
    assert "### small.py ###" in prompt or "list_changed_files" in prompt


def test_render_manifest_emits_full_list_when_budget_matches_full_size() -> None:
    """When max_manifest_chars equals _manifest_chars, every path must render —
    do not reserve overflow-note room that would hide paths that already fit."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.submission_pass_runner import _manifest_chars

    paths = ["a", "b", "c"]
    budget = _manifest_chars(paths)
    rendered = pass_mod._render_manifest(paths, budget)
    text = "\n".join(rendered)
    assert "a" in text and "b" in text and "c" in text
    assert "more changed path(s) not listed" not in text
    # ``_manifest_chars`` counts a trailing newline after the last path; join does not.
    assert len(text) <= budget


def test_fit_changed_file_block_shrinks_when_fence_exceeds_reserve() -> None:
    """A full-file block whose actual fence exceeds the 8-char reserve must
    shrink the body instead of dropping the file entirely."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    # Eight+ backticks force code_fence_for to emit a longer fence than the reserve.
    content = "x = '''" + ("`" * 12) + "'''\n" + ("y = 1\n" * 40)
    heading = "### fencey.py ###"
    fence_reserve = 8
    base_overhead = len(heading) + 1 + 2 * (fence_reserve + 1)
    # Budget that appears to fit under the reserve but not under the real fence.
    remaining = base_overhead + len(content)
    block_lines, truncated = pass_mod._fit_changed_file_block("fencey.py", content, remaining)
    assert block_lines is not None
    block = "\n".join(block_lines)
    assert len(block) <= remaining
    assert "### fencey.py ###" in block
    # Either the full content fit with the real fence, or we shrunk with a note.
    if truncated:
        assert "Only the first" in block


def test_list_changed_files_tool_returns_submission_paths_only() -> None:
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    index = CodebaseIndex.from_input(_input(files={"a.py": "a", "b.py": "b"}))
    tools = pass_mod._build_merged_pass_tools(index, side_on=False)
    names = {getattr(t, "__name__", getattr(t, "tool_name", "")) for t in tools}
    # strands @tool may wrap the name on .tool_name / .name
    tool_ids = set()
    for t in tools:
        tool_ids.add(getattr(t, "__name__", None) or getattr(t, "name", None) or str(t))
    assert any("list_changed_files" in str(x) for x in tool_ids | names)
    changed = next(
        t
        for t in tools
        if "list_changed_files" in (getattr(t, "__name__", "") or getattr(t, "name", "") or str(t))
    )
    # Prefer calling the underlying function if strands wrapped it.
    fn = getattr(changed, "fn", None) or getattr(changed, "_fn", None) or changed
    result = fn() if callable(fn) else changed()
    assert "a.py" in result and "b.py" in result


def _merged_tool_names(tools: list) -> set:
    """Extract each tool's registered name, tolerating whichever attribute
    the ``strands`` ``@tool`` decorator populates for a given tool object."""
    return {
        getattr(t, "tool_name", None) or getattr(t, "__name__", None) or getattr(t, "name", "")
        for t in tools
    }


def test_build_merged_pass_tools_includes_scoped_tools_when_side_off() -> None:
    """With the side-effect half off, the merged builder still exposes the full
    shared scoped-tool set (read_lines/read_function/find_references) plus its
    own list_changed_files -- but not search_repository, which only the
    side-effect half introduces."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    index = CodebaseIndex.from_input(_input(files={"a.py": "a"}))
    tools = pass_mod._build_merged_pass_tools(index, side_on=False)
    assert _merged_tool_names(tools) == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
        "list_changed_files",
    }


def test_build_merged_pass_tools_includes_scoped_tools_when_side_on() -> None:
    """With the side-effect half on, the merged builder additionally exposes
    search_repository on top of the shared scoped-tool set and
    list_changed_files."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    index = CodebaseIndex.from_input(_input(files={"a.py": "a"}))
    tools = pass_mod._build_merged_pass_tools(index, side_on=True)
    assert _merged_tool_names(tools) == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
        "search_repository",
        "list_changed_files",
    }


def test_format_changed_files_page_paginates_and_hints_next_offset() -> None:
    from code_review_agent.merged_architecture_side_effect_pass import format_changed_files_page

    paths = [f"f{i:03d}.py" for i in range(25)]
    page1 = format_changed_files_page(paths, offset=0, limit=10)
    assert "f000.py" in page1 and "f009.py" in page1
    assert "f010.py" not in page1
    assert "offset=10" in page1
    assert "of 25" in page1

    page2 = format_changed_files_page(paths, offset=10, limit=10)
    assert "f010.py" in page2 and "f019.py" in page2
    assert "offset=20" in page2

    page3 = format_changed_files_page(paths, offset=20, limit=10)
    assert "f020.py" in page3 and "f024.py" in page3
    assert "offset=" not in page3  # last page — no next hint
    assert "showing paths 21-25 of 25" in page3


def test_format_changed_files_page_bounds_by_char_budget() -> None:
    from code_review_agent.merged_architecture_side_effect_pass import format_changed_files_page

    paths = [f"dir/very_long_path_name_{i}.py" for i in range(50)]
    page = format_changed_files_page(paths, offset=0, limit=500, max_chars=120)
    # Must not dump the whole list when the char budget is tight.
    assert page.count("\n") < 50
    assert "offset=" in page
    assert "of 50" in page


def test_no_extra_batching_for_small_multi_file_submission_under_budget() -> None:
    """Several small files that together still fit the per-call budget must
    make exactly one LLM call, not one per file — no behavior change for
    submissions under the budget."""
    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "def a():\n    return 1\n",
        "b.py": "def b():\n    return 2\n",
        "c.py": "def c():\n    return 3\n",
    }
    find_architecture_and_side_effect_issues(_Client(), _input(files=files))
    assert len(prompts) == 1


def test_splits_into_multiple_batches_for_oversized_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the changed-file set's total estimated size exceeds one call's
    inline-code budget, the merged pass issues one independent LLM call per
    batch and concatenates each batch's findings into the same two lists."""
    import code_review_agent.submission_pass_runner as runner_mod

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=0,
            max_inline_code_chars=200,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                for path in ("a.py", "b.py", "c.py"):
                    if f"### {path} ###" in self.latest_reasoning_prompt():
                        return {
                            "architecture_findings": [
                                {
                                    "severity": "medium",
                                    "category": "architecture",
                                    "file_path": path,
                                    "description": f"finding for {path}",
                                    "suggestion": "n/a",
                                }
                            ],
                            "side_effect_findings": [],
                        }
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "x = 1\n" * 10,
        "b.py": "y = 2\n" * 10,
        "c.py": "z = 3\n" * 10,
    }
    arch, side = find_architecture_and_side_effect_issues(_Client(), _input(files=files))

    assert len(prompts) == 3
    assert {f.description for f in arch} == {
        "finding for a.py",
        "finding for b.py",
        "finding for c.py",
    }
    assert side == []
    # Every batch's manifest still lists all three changed files (whole-
    # submission awareness), even though its content section shows only one.
    for prompt in prompts:
        manifest_section = prompt.split("**Full content of the changed files", 1)[0]
        assert "a.py" in manifest_section
        assert "b.py" in manifest_section
        assert "c.py" in manifest_section
    assert any("batch 1 of 3" in p for p in prompts)
    assert any("batch 3 of 3" in p for p in prompts)


def test_one_batch_failure_does_not_discard_other_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed reply for one batch must not wipe out findings already
    collected from other, successful batches."""
    import code_review_agent.submission_pass_runner as runner_mod

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=0,
            max_inline_code_chars=200,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                if "### a.py ###" in self.latest_reasoning_prompt():
                    return "not even a dict-shaped reply"  # type: ignore[return-value]
                if "### b.py ###" in self.latest_reasoning_prompt():
                    return {
                        "architecture_findings": [
                            {
                                "severity": "medium",
                                "category": "architecture",
                                "file_path": "b.py",
                                "description": "finding for b.py",
                                "suggestion": "n/a",
                            }
                        ],
                        "side_effect_findings": [],
                    }
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "x = 1\n" * 10,
        "b.py": "y = 2\n" * 10,
        "c.py": "z = 3\n" * 10,
    }
    arch, side = find_architecture_and_side_effect_issues(_Client(), _input(files=files))
    assert [f.description for f in arch] == ["finding for b.py"]
    assert side == []


def test_reactive_recovery_bisects_overflowing_batch_through_public_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass must now benefit from the shared runner's reactive bisect
    recovery: a combined batch call that overflows mid-turn should be retried
    as two single-file calls, rather than simply skipped (the pre-runner
    behavior — this pass had no reactive recovery of its own)."""
    import code_review_agent.submission_pass_runner as runner_mod
    from strands.types.exceptions import ContextWindowOverflowException

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    # A generous inline budget so both tiny files actually inline into the
    # first (combined) call instead of being omitted for lack of room — the
    # default DummyLLMClient context leaves ~0 content room once the dual-
    # array response reserve is set aside (see test_no_extra_batching_for_
    # small_multi_file_submission_under_budget, which never inspects prompt
    # content for exactly this reason).
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=0,
            max_inline_code_chars=100_000,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    call_count = {"n": 0}

    class _Client(SubmissionPassTwoCallClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            if _MERGED_PASS_ANCHOR in prompt:
                call_count["n"] += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise ContextWindowOverflowException("combined batch too large")
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                for path in ("a.py", "b.py"):
                    if f"### {path} ###" in self.latest_reasoning_prompt():
                        return {
                            "architecture_findings": [
                                {
                                    "severity": "medium",
                                    "category": "architecture",
                                    "file_path": path,
                                    "description": f"finding for {path}",
                                    "suggestion": "n/a",
                                }
                            ],
                            "side_effect_findings": [],
                        }
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    arch, side = find_architecture_and_side_effect_issues(_Client(), _input(files=files))

    assert {f.description for f in arch} == {"finding for a.py", "finding for b.py"}
    assert side == []
    # More than one call proves the overflowing combined attempt was actually
    # retried (bisected), not that the test happened to pass on the first try.
    assert call_count["n"] > 1
