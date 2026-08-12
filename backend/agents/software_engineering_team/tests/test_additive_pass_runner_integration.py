"""Thin integration coverage locking the additive-pass runner-only invariant.

The three once-per-submission additive code-review passes
(``architecture_consistency_pass``, ``side_effect_impact_pass``,
``merged_architecture_side_effect_pass``) were migrated onto the shared
``submission_pass_runner`` so Agent construction and overflow recovery live
in one place. This file does not re-test that mechanics:
``test_submission_pass_runner.py`` covers the runner's bisect recovery in
isolation, and each pass's own overflow-recovery test exercises its path end
to end. Instead, this file locks in the migration invariant so a future change
cannot silently reintroduce a direct ``Agent`` construction, a duplicated
recovery helper, or a call path that bypasses ``run_submission_pass`` in a
pass module without failing a test.

No network/LLM: every test here uses ``DummyLLMClient`` subclasses or a
spied-in ``Agent`` stand-in, matching the runner's own unit-test posture.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List

import code_review_agent.architecture_consistency_pass as arch_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_mod
import code_review_agent.side_effect_impact_pass as side_mod
import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.models import CodeReviewInput

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture

_PASS_MODULES = (arch_mod, side_mod, merged_mod)

# Names the shared runner owns; a migrated pass module must not (re-)define
# or import any of them locally -- that would mean Agent construction or
# recovery mechanics leaked back out of the runner during a future edit.
_RUNNER_OWNED_NAMES = (
    "run_agent_via_reasoning",
    "_call_agent",
    "_is_overflow_shaped",
    "_run_batch_with_recovery",
    "_recover_from_overflow",
)


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        overview="Layered service architecture.",
        architecture_document="All writes MUST go through the repository layer.",
    )


def _input(**overrides: Any) -> CodeReviewInput:
    files = overrides.pop("files", {"app/main.py": "def bar():\n    return 1\n"})
    return CodeReviewInput(files=files, task_description="wire up bar", **overrides)


# --------------------------------------------------------------------------- static checks


@pytest.mark.parametrize("module", _PASS_MODULES, ids=lambda m: m.__name__)
def test_pass_module_holds_none_of_the_runner_owned_names(module: Any) -> None:
    """A migrated pass module defines/imports none of the runner-owned names.

    ``run_agent_via_reasoning`` is the crux of the runner-only-construction
    invariant: if a pass module ever imported it directly, it could bypass
    the runner's overflow recovery entirely.
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


def test_think_then_format_routes_through_the_shared_runner() -> None:
    """Source-level lock: the shared runner delegates Agent work to via_reasoning."""
    runner_source = inspect.getsource(runner_mod)
    assert "run_agent_via_reasoning" in runner_source, (
        "sanity check failed: submission_pass_runner no longer delegates to run_agent_via_reasoning"
    )

    for module in _PASS_MODULES:
        source = inspect.getsource(module)
        assert "run_agent_via_reasoning" not in source, (
            f"{module.__name__} must not call run_agent_via_reasoning directly"
        )
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
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return "prose"

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
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return "prose"

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
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return "prose"

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"architecture_findings": [], "side_effect_findings": []}

    merged_mod.find_architecture_and_side_effect_issues(_Client(), _input(architecture=_arch()))
    assert calls == ["merged"]


# ------------------------------------------------------- dynamic via_reasoning checks


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
def test_run_agent_via_reasoning_routes_through_the_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
    entry_point: Callable[[Any, CodeReviewInput], Any],
    reply: str,
    build_input: Callable[[], CodeReviewInput],
) -> None:
    """Each pass's real public entry point invokes think-then-format via the runner."""
    calls: List[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return kwargs["parse"](reply)

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _spy)

    entry_point(DummyLLMClient(), build_input())

    assert calls, "expected the pass to invoke run_agent_via_reasoning via the shared runner"
    assert calls[0]["reasoning_think"] is True
