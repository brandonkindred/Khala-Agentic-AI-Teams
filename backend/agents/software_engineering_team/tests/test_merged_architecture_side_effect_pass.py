"""Tests for the merged architecture-consistency + side-effect-impact pass."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest
from code_review_agent.merged_architecture_side_effect_pass import (
    find_architecture_and_side_effect_issues,
)
from code_review_agent.models import CodeReviewInput
from code_review_agent.profiles import ReviewProfile
from tests.submission_pass_two_call_client import (
    MutationFindingClient,
    SubmissionPassTwoCallClient,
    mutation_finding_payload,
    wire_run_agent_via_reasoning_for_test_clients,
    wire_run_agent_via_reasoning_with_raw,
)

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture


@pytest.fixture(autouse=True)
def _wire_submission_pass_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the submission-pass runner's ``run_agent_via_reasoning`` through the
    two-call test stub for every test in this module.

    File-scoped (a plain module-level fixture, not a ``pytest_plugins``
    registration): a fixture defined directly in a test module only applies to
    that module's own tests, so this cannot leak into sibling test files under
    pytest-xdist the way a ``pytest_plugins`` registration would (each xdist
    worker collects the whole test tree, so a session-wide plugin's autouse
    fixtures would otherwise apply to every test the worker runs).
    """
    import code_review_agent.submission_pass_runner as runner_mod

    wire_run_agent_via_reasoning_for_test_clients(monkeypatch, runner_mod)


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


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param("fenced", id="fenced"),
        pytest.param("prose", id="prose-prefixed"),
    ],
)
def test_recovers_fenced_and_prose_wrapped_reply(
    monkeypatch: pytest.MonkeyPatch, wrap: str
) -> None:
    """A merged-pass reply wrapped in a ```json fence or prefixed with prose still
    parses both halves: the pass routes it through the canonical recovery ladder
    rather than a bare ``json.loads`` that would raise on the fence/prose."""
    import code_review_agent.submission_pass_runner as runner_mod

    payload = {
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
    inner = json.dumps(payload)
    raw = f"```json\n{inner}\n```" if wrap == "fenced" else f"Sure, here you go: {inner}"
    wire_run_agent_via_reasoning_with_raw(monkeypatch, runner_mod, raw)

    arch, side = find_architecture_and_side_effect_issues(DummyLLMClient(), _input())
    assert len(arch) == 1
    assert arch[0].category == "architecture"
    assert len(side) == 1
    assert side[0].category == "side-effects"


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

    def _spy(*, arch_on: bool, side_on: bool, mutation_on: bool = True) -> str:
        built["arch_on"] = arch_on
        built["side_on"] = side_on
        built["prompt"] = real_build(arch_on=arch_on, side_on=side_on, mutation_on=mutation_on)
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

    def _spy(*, arch_on: bool, side_on: bool, mutation_on: bool = True) -> str:
        built["arch_on"] = arch_on
        built["side_on"] = side_on
        built["prompt"] = real_build(arch_on=arch_on, side_on=side_on, mutation_on=mutation_on)
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


def test_large_architecture_document_is_inlined_in_full() -> None:
    """A huge architecture document is inlined in full in the user prompt."""
    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch_doc = SystemArchitecture(overview="", architecture_document="X" * 100_000)
    find_architecture_and_side_effect_issues(_Client(), _input(architecture=arch_doc))

    assert prompts
    assert "X" * 100_000 in prompts[0]
    assert "the remainder was omitted" not in prompts[0]


def test_inlines_full_changed_file_manifest() -> None:
    """All changed paths are listed when prompt packing is unbounded."""
    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {f"pkg/module_{i:04d}.py": f"def f{i}():\n    return {i}\n" for i in range(400)}
    find_architecture_and_side_effect_issues(_Client(), _input(files=files))
    assert prompts
    manifest_section = prompts[0].split("**Full content of the changed files:**", 1)[0]
    assert "more changed path(s) not listed" not in manifest_section
    assert manifest_section.count("pkg/module_") == 400


def test_runner_does_not_pin_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submission passes must not clone the model with a token output budget."""
    import code_review_agent.submission_pass_runner as runner_mod

    from llm_service import LLMClientModel

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    clones: list = []

    class _Empty(DummyLLMClient):
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
    assert not any("max_tokens" in c for c in clones)


def test_build_prompt_inlines_large_changed_files_in_full() -> None:
    """Large and small changed files are both inlined without truncation."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    big_content = "X" * 5_000
    small_content = "def ok():\n    return 1\n"
    index = CodebaseIndex.from_input(
        _input(files={"big.py": big_content, "small.py": small_content})
    )
    prompt = pass_mod._build_prompt(index, "", arch_on=False, side_on=True)
    assert big_content in prompt
    assert small_content in prompt
    assert "Only the first" not in prompt
    assert "more changed file(s) not shown" not in prompt


def test_build_prompt_renders_replaced_content_when_side_on() -> None:
    """A path with a ``replaced_content`` entry gets its before-image section
    when the side-effect half is on."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    files = {"app/main.py": "def bar():\n    return 2\n"}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = pass_mod._build_prompt(
        index,
        "",
        arch_on=False,
        side_on=True,
        replaced_content={"app/main.py": "def bar():\n    return 1\n"},
    )
    assert "Replaced (pre-change) content" in prompt
    assert "def bar():\n    return 1\n" in prompt


def test_build_prompt_omits_replaced_content_when_side_off() -> None:
    """The before-image section never renders when the side-effect half is off,
    even if ``replaced_content`` is supplied (architecture-only prompts must
    stay unaffected)."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    files = {"app/main.py": "def bar():\n    return 2\n"}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = pass_mod._build_prompt(
        index,
        "",
        arch_on=True,
        side_on=False,
        replaced_content={"app/main.py": "def bar():\n    return 1\n"},
    )
    assert "Replaced (pre-change) content" not in prompt


def test_build_prompt_renders_replaced_content_with_both_halves_on() -> None:
    """The before-image section still renders when both halves are enabled."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    files = {"app/main.py": "def bar():\n    return 2\n"}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = pass_mod._build_prompt(
        index,
        "",
        arch_on=True,
        side_on=True,
        replaced_content={"app/main.py": "def bar():\n    return 1\n"},
    )
    assert "Replaced (pre-change) content" in prompt


def test_build_prompt_omits_replaced_content_when_absent() -> None:
    """Default (``replaced_content=None``) renders exactly as today."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import CodebaseIndex

    index = CodebaseIndex.from_input(_input())
    prompt = pass_mod._build_prompt(index, "", arch_on=False, side_on=True)
    assert "Replaced (pre-change) content" not in prompt


# --------------------------------------------------------------------------- CODE_REVIEW_MUTATION_ANALYSIS


def test_replaced_content_reaches_prompt_when_mutation_analysis_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (``CODE_REVIEW_MUTATION_ANALYSIS`` unset): a before-image supplied
    on ``CodeReviewInput.replaced_content`` reaches the merged pass's user
    prompt as a "Replaced (pre-change) content" section."""
    monkeypatch.delenv("CODE_REVIEW_MUTATION_ANALYSIS", raising=False)

    class _Capture(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"architecture_findings": [], "side_effect_findings": []}

    client = _Capture()
    find_architecture_and_side_effect_issues(
        client,
        _input(
            files={"app/main.py": "def bar():\n    return 2\n"},
        ).model_copy(update={"replaced_content": {"app/main.py": "def bar():\n    return 1\n"}}),
    )
    assert "Replaced (pre-change) content" in client.latest_reasoning_prompt()
    assert "def bar():\n    return 1\n" in client.latest_reasoning_prompt()


def test_replaced_content_hidden_from_prompt_when_mutation_analysis_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODE_REVIEW_MUTATION_ANALYSIS=false`` must hide the before-image from
    the model entirely, even though the side-effect half is otherwise on."""
    monkeypatch.setenv("CODE_REVIEW_MUTATION_ANALYSIS", "false")

    class _Capture(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"architecture_findings": [], "side_effect_findings": []}

    client = _Capture()
    find_architecture_and_side_effect_issues(
        client,
        _input(
            files={"app/main.py": "def bar():\n    return 2\n"},
        ).model_copy(update={"replaced_content": {"app/main.py": "def bar():\n    return 1\n"}}),
    )
    assert "Replaced (pre-change) content" not in client.latest_reasoning_prompt()
    assert "def bar():\n    return 1\n" not in client.latest_reasoning_prompt()


def test_reasoning_system_prompt_reflects_mutation_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merged pass's system prompt must carry (or omit) the
    mutation-vs-replaced-code contract sub-check per ``CODE_REVIEW_MUTATION_ANALYSIS``,
    while still carrying the side-effect body either way."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    captured: Dict[str, Any] = {}

    def _fake_run_submission_pass(llm: Any, **kwargs: Any) -> list:
        captured["reasoning_system_prompt"] = kwargs["reasoning_system_prompt"]
        return []

    monkeypatch.setattr(pass_mod, "run_submission_pass", _fake_run_submission_pass)

    monkeypatch.delenv("CODE_REVIEW_MUTATION_ANALYSIS", raising=False)
    find_architecture_and_side_effect_issues(DummyLLMClient(), _input())
    assert "mutation-vs-replaced-code" in captured["reasoning_system_prompt"]
    assert "Side-Effect / Blast-Radius Impact" in captured["reasoning_system_prompt"]

    captured.clear()
    monkeypatch.setenv("CODE_REVIEW_MUTATION_ANALYSIS", "false")
    find_architecture_and_side_effect_issues(DummyLLMClient(), _input())
    assert "mutation-vs-replaced-code" not in captured["reasoning_system_prompt"]
    assert "Side-Effect / Blast-Radius Impact" in captured["reasoning_system_prompt"]


def _mutation_finding_client() -> MutationFindingClient:
    return MutationFindingClient(
        anchor=_MERGED_PASS_ANCHOR,
        response_with_finding={
            "architecture_findings": [],
            "side_effect_findings": [mutation_finding_payload()],
        },
        response_without_finding={"architecture_findings": [], "side_effect_findings": []},
    )


def test_fires_mutation_finding_when_before_image_present() -> None:
    """A mutation-contract side-effect finding is produced when the
    submission carries a before-image for the changed file."""
    arch, side = find_architecture_and_side_effect_issues(
        _mutation_finding_client(),
        _input(files={"app/main.py": "def bar():\n    return 2\n"}).model_copy(
            update={"replaced_content": {"app/main.py": "def bar():\n    return 1\n"}}
        ),
    )
    assert arch == []
    assert len(side) == 1
    assert side[0].category == "side-effects"
    assert "app/caller.py" in side[0].description


def test_no_speculative_finding_without_before_image() -> None:
    """The identical scripted reply logic produces no finding when there is
    no before-image to react to -- the mutation sub-check cannot speculate
    about a prior version it was never shown."""
    arch, side = find_architecture_and_side_effect_issues(
        _mutation_finding_client(),
        _input(files={"app/main.py": "def bar():\n    return 2\n"}),
    )
    assert arch == []
    assert side == []


def test_render_manifest_lists_every_path() -> None:
    """``_render_manifest`` lists every changed path with no character cap."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod

    paths = ["a", "b", "c"]
    rendered = pass_mod._render_manifest(paths)
    text = "\n".join(rendered)
    assert "a" in text and "b" in text and "c" in text
    assert "more changed path(s) not listed" not in text


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


def _merged_tool_by_name(tools: list, name: str):
    """Look up one tool by its registered name (see ``_merged_tool_names``)."""
    return next(
        t
        for t in tools
        if (getattr(t, "tool_name", None) or getattr(t, "__name__", None) or getattr(t, "name", ""))
        == name
    )


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


def test_list_changed_files_shares_run_level_total_call_budget_with_base_tools() -> None:
    """list_changed_files is added outside ``_build_tools``/``build_side_effect_tools``,
    but must still share their one call tracker -- exhausting the shared
    total-call budget via a base tool must also short-circuit
    list_changed_files, proving the run-level cap covers the merged pass's
    whole tool set, not just the tools built underneath it."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import _MAX_TOTAL_TOOL_CALLS, CodebaseIndex

    index = CodebaseIndex.from_input(_input(files={"a.py": "a", "b.py": "b"}))
    tools = pass_mod._build_merged_pass_tools(index, side_on=False)
    list_files = _merged_tool_by_name(tools, "list_files")
    list_changed_files = _merged_tool_by_name(tools, "list_changed_files")
    for _ in range(_MAX_TOTAL_TOOL_CALLS):
        list_files()
    result = list_changed_files()
    assert "tool call budget" in result
    assert "exhausted" in result
    assert "a.py" not in result


def test_list_changed_files_and_search_repository_share_one_budget_when_side_on() -> None:
    """When the side-effect half is on, list_changed_files, search_repository,
    and the seven shared tools all come from three different builder layers
    (``_build_tools`` -> ``build_side_effect_tools`` -> ``_build_merged_pass_tools``)
    but must still share exactly one run-level budget end to end."""
    import code_review_agent.merged_architecture_side_effect_pass as pass_mod
    from code_review_agent.false_positive_filter import _MAX_TOTAL_TOOL_CALLS, CodebaseIndex

    index = CodebaseIndex.from_input(_input(files={"a.py": "a"}))
    tools = pass_mod._build_merged_pass_tools(index, side_on=True)
    list_files = _merged_tool_by_name(tools, "list_files")
    list_changed_files = _merged_tool_by_name(tools, "list_changed_files")
    search_repository = _merged_tool_by_name(tools, "search_repository")
    for _ in range(_MAX_TOTAL_TOOL_CALLS):
        list_files()
    changed_result = list_changed_files()
    assert "tool call budget" in changed_result and "exhausted" in changed_result
    repo_result = search_repository("anything")
    assert "tool call budget" in repo_result and "exhausted" in repo_result


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


def test_no_extra_batching_for_small_multi_file_submission() -> None:
    """Several small files make exactly one LLM call when no overflow occurs."""
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
    for path in files:
        assert f"### {path} ###" in prompts[0]


def test_reactive_recovery_bisects_overflowing_batch_through_public_entry_point() -> None:
    """The merged pass benefits from the shared runner's reactive bisect recovery."""
    from strands.types.exceptions import ContextWindowOverflowException

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
    assert call_count["n"] > 1
