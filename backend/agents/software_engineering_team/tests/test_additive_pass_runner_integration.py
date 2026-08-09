"""Thin integration coverage locking the additive-pass runner-only invariant.

The three once-per-submission additive code-review passes
(``architecture_consistency_pass``, ``side_effect_impact_pass``,
``merged_architecture_side_effect_pass``) were migrated onto the shared
``submission_pass_runner`` so budgeting, chunking, ``Agent`` construction, and
overflow recovery live in one place. This file does not re-test that
mechanics (``test_submission_pass_runner.py`` and each pass's own
"...through_public_entry_point" tests already do); it locks in the migration
invariant so a future change cannot silently reintroduce a direct ``Agent``
construction, a duplicated recovery helper, or a call path that bypasses
``run_submission_pass`` in a pass module without failing a test -- and proves
that a context-overflow on the only batch a submission needs still yields
findings via the runner's reactive recovery, for all three passes, not a
silent empty result.

No network/LLM: every test here uses ``DummyLLMClient`` subclasses or a
spied-in ``Agent`` stand-in, matching the runner's own unit-test posture.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Tuple

import code_review_agent.architecture_consistency_pass as arch_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_mod
import code_review_agent.side_effect_impact_pass as side_mod
import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.models import CodeReviewInput
from strands.types.exceptions import ContextWindowOverflowException

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture
from software_engineering_team.shared.context_sizing import MergedPassBudgets

_PASS_MODULES = (arch_mod, side_mod, merged_mod)

# Names the shared runner owns; a migrated pass module must not (re-)define
# or import any of them locally -- that would mean Agent construction or
# recovery mechanics leaked back out of the runner during a future edit.
_RUNNER_OWNED_NAMES = (
    "Agent",
    "_call_agent",
    "_is_overflow_shaped",
    "_run_batch_with_recovery",
    "_recover_from_overflow",
    "_shrink_items",
    "_shrink_budgets",
    "_pack_batches",
    "_MAX_BATCH_BISECT_DEPTH",
)


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        overview="Layered service architecture.",
        architecture_document="All writes MUST go through the repository layer.",
    )


def _input(**overrides: Any) -> CodeReviewInput:
    files = overrides.pop("files", {"app/main.py": "def bar():\n    return 1\n"})
    return CodeReviewInput(files=files, task_description="wire up bar", **overrides)


def _two_files() -> Dict[str, str]:
    return {"a.py": "x = 1\n", "b.py": "y = 2\n"}


# --------------------------------------------------------------------------- static checks


@pytest.mark.parametrize("module", _PASS_MODULES, ids=lambda m: m.__name__)
def test_pass_module_holds_none_of_the_runner_owned_names(module: Any) -> None:
    """A migrated pass module defines/imports none of the runner-owned names.

    ``Agent`` is the crux of the runner-only-construction invariant: if a
    pass module ever imported ``strands.Agent``, it could construct one
    directly, bypassing the runner's budgeting/chunking/recovery entirely.
    The remaining names are the runner's private recovery helpers -- their
    presence here would mean a duplicate implementation, not delegation.
    """
    for name in _RUNNER_OWNED_NAMES:
        assert getattr(runner_mod, name, None) is not None, (
            f"sanity check failed: submission_pass_runner no longer defines {name!r}"
        )
        assert not hasattr(module, name), (
            f"{module.__name__} must not define/import {name!r} -- that belongs "
            "solely to submission_pass_runner"
        )


def test_agent_construction_appears_only_in_the_shared_runner() -> None:
    """Source-level lock: no additive pass module constructs or imports
    ``strands.Agent`` directly -- among the runner and the three migrated
    pass modules, only the runner's source may reference it."""
    runner_source = inspect.getsource(runner_mod)
    assert "Agent(" in runner_source, (
        "sanity check failed: submission_pass_runner no longer appears to construct Agent anywhere"
    )

    for module in _PASS_MODULES:
        source = inspect.getsource(module)
        assert "Agent(" not in source, f"{module.__name__} must not construct Agent directly"
        assert "import Agent" not in source, f"{module.__name__} must not import strands.Agent"
        assert "strands.Agent" not in source, f"{module.__name__} must not reference strands.Agent"


# ------------------------------------------------------------ delegates to runner


def test_architecture_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = arch_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("architecture")
        return original(llm, **kwargs)

    monkeypatch.setattr(arch_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    arch_mod.find_architecture_and_redundancy_issues(_Client(), _input(architecture=_arch()))
    assert calls == ["architecture"]


def test_side_effect_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = side_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("side_effect")
        return original(llm, **kwargs)

    monkeypatch.setattr(side_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    side_mod.find_side_effect_impact_issues(_Client(), _input())
    assert calls == ["side_effect"]


def test_merged_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = merged_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("merged")
        return original(llm, **kwargs)

    monkeypatch.setattr(merged_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"architecture_findings": [], "side_effect_findings": []}

    merged_mod.find_architecture_and_side_effect_issues(_Client(), _input(architecture=_arch()))
    assert calls == ["merged"]


# ------------------------------------------------------- dynamic Agent-symbol checks


def _agent_spy_class(reply: str, calls: List[Tuple[Any, str, list]]) -> type:
    """Build a `strands.Agent` stand-in that records construction and returns ``reply``."""

    class _Spy:
        def __init__(self, *, model: Any, system_prompt: str, tools: list) -> None:
            calls.append((model, system_prompt, tools))

        def __call__(self, prompt: str) -> str:
            return reply

    return _Spy


_ARCH_ENTRY: Callable[[Any, CodeReviewInput], Any] = (
    arch_mod.find_architecture_and_redundancy_issues
)
_SIDE_ENTRY: Callable[[Any, CodeReviewInput], Any] = side_mod.find_side_effect_impact_issues
_MERGED_ENTRY: Callable[[Any, CodeReviewInput], Any] = (
    merged_mod.find_architecture_and_side_effect_issues
)


@pytest.mark.parametrize(
    "entry_point, reply, build_input",
    [
        (_ARCH_ENTRY, '{"findings": []}', lambda: _input(architecture=_arch())),
        (_SIDE_ENTRY, '{"findings": []}', lambda: _input()),
        (
            _MERGED_ENTRY,
            '{"architecture_findings": [], "side_effect_findings": []}',
            lambda: _input(architecture=_arch()),
        ),
    ],
    ids=["architecture_consistency", "side_effect_impact", "merged_architecture_side_effect"],
)
def test_agent_construction_routes_through_the_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
    entry_point: Callable[[Any, CodeReviewInput], Any],
    reply: str,
    build_input: Callable[[], CodeReviewInput],
) -> None:
    """Each pass's real public entry point constructs its Agent only via the runner's ``Agent`` symbol."""
    calls: List[Tuple[Any, str, list]] = []
    monkeypatch.setattr(runner_mod, "Agent", _agent_spy_class(reply, calls))

    entry_point(DummyLLMClient(), build_input())

    assert calls, "expected the pass to construct at least one Agent via the shared runner"


# ------------------------------------------------------- overflow-alone recovery


def _overflow_budgets() -> MergedPassBudgets:
    # Generous per-call allowance: both files fit into one proactive batch,
    # so the overflow below is triggered explicitly by the scripted client
    # (mid-turn context overflow on the combined call), not by budgeting --
    # DummyLLMClient's real context window is otherwise consumed almost
    # entirely by the merged pass's larger combined (Part 1 + Part 2)
    # system prompt, leaving too little manifest/content budget to even
    # render either file's heading.
    return MergedPassBudgets(
        max_architecture_chars=100_000,
        max_inline_code_chars=100_000,
        max_manifest_chars=2_000,
        reserved_response_tokens=4096,
    )


def _finding(path: str, category: str) -> Dict[str, Any]:
    return {
        "severity": "medium",
        "category": category,
        "file_path": path,
        "description": f"finding for {path}",
        "suggestion": "n/a",
    }


def test_architecture_pass_overflow_alone_still_returns_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow on the combined batch, with nothing else wrong, must recover
    via the shared runner's bisect and still return findings -- a silent
    empty result here would be the pre-runner regression this pass migrated
    away from."""
    monkeypatch.setattr(
        runner_mod, "compute_code_review_merged_pass_budgets", lambda *a, **k: _overflow_budgets()
    )
    call_count = {"n": 0}

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            call_count["n"] += 1
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise ContextWindowOverflowException("combined batch too large")
            for path in ("a.py", "b.py"):
                if f"### {path} ###" in prompt:
                    return {"findings": [_finding(path, "architecture")]}
            return {"findings": []}

    result = arch_mod.find_architecture_and_redundancy_issues(
        _Client(), _input(files=_two_files(), architecture=_arch())
    )

    assert {f.description for f in result} == {"finding for a.py", "finding for b.py"}
    assert call_count["n"] > 1  # proves the overflowing call was retried, not skipped


def test_side_effect_pass_overflow_alone_still_returns_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_mod, "compute_code_review_merged_pass_budgets", lambda *a, **k: _overflow_budgets()
    )
    call_count = {"n": 0}

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            call_count["n"] += 1
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise ContextWindowOverflowException("combined batch too large")
            for path in ("a.py", "b.py"):
                if f"### {path} ###" in prompt:
                    return {"findings": [_finding(path, "side-effects")]}
            return {"findings": []}

    result = side_mod.find_side_effect_impact_issues(_Client(), _input(files=_two_files()))

    assert {f.description for f in result} == {"finding for a.py", "finding for b.py"}
    assert call_count["n"] > 1


def test_merged_pass_overflow_alone_still_returns_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_mod, "compute_code_review_merged_pass_budgets", lambda *a, **k: _overflow_budgets()
    )
    call_count = {"n": 0}

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            call_count["n"] += 1
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise ContextWindowOverflowException("combined batch too large")
            for path in ("a.py", "b.py"):
                if f"### {path} ###" in prompt:
                    return {
                        "architecture_findings": [_finding(path, "architecture")],
                        "side_effect_findings": [],
                    }
            return {"architecture_findings": [], "side_effect_findings": []}

    arch_findings, side_findings = merged_mod.find_architecture_and_side_effect_issues(
        _Client(), _input(files=_two_files(), architecture=_arch())
    )

    assert {f.description for f in arch_findings} == {"finding for a.py", "finding for b.py"}
    assert side_findings == []
    assert call_count["n"] > 1
