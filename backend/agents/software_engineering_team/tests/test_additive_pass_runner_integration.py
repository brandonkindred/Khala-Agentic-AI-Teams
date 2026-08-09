"""Thin integration coverage locking the additive-pass -> shared-runner wiring.

Complements ``test_submission_pass_runner.py`` (runner-internal mechanics,
exercised directly and never through a pass) and each pass's own test module
(``test_architecture_consistency_pass.py``, ``test_side_effect_impact_pass.py``,
``test_merged_architecture_side_effect_pass.py``) with cross-cutting
invariants that only make sense checked once, across all three migrated
additive passes together:

    - None of the three pass modules construct a ``strands.Agent`` on their
      own, or re-implement any of the runner's budgeting/chunking/recovery
      helpers locally -- :func:`~code_review_agent.submission_pass_runner.run_submission_pass`
      is the sole construction point.
    - Each pass's public entry point actually delegates to
      ``run_submission_pass`` rather than calling the LLM/``Agent`` some
      other way.
    - A context-overflow on the only batch a submission needs still yields
      findings via the runner's reactive bisect recovery -- overflow alone
      must never silently degrade to an empty result, for any of the three
      migrated passes.

No network/LLM: every test here uses ``DummyLLMClient`` subclasses, matching
the runner's own unit-test posture.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

import code_review_agent.architecture_consistency_pass as arch_pass_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_pass_mod
import code_review_agent.side_effect_impact_pass as side_pass_mod
import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.models import CodeReviewInput
from strands.types.exceptions import ContextWindowOverflowException

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture
from software_engineering_team.shared.context_sizing import MergedPassBudgets

_ADDITIVE_PASS_MODULES = (arch_pass_mod, side_pass_mod, merged_pass_mod)

# Names the shared runner owns; a migrated pass module must not (re-)define
# any of them locally -- that would mean Agent construction or recovery
# mechanics leaked back out of the runner during a future edit.
_RUNNER_OWNED_NAMES = (
    "Agent",
    "_call_agent",
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


def _files() -> Dict[str, str]:
    return {"a.py": "x = 1\n", "b.py": "y = 2\n"}


# --------------------------------------------------------------------- statics


@pytest.mark.parametrize("module", _ADDITIVE_PASS_MODULES, ids=lambda m: m.__name__)
def test_pass_module_holds_none_of_the_runner_owned_names(module: Any) -> None:
    """A migrated pass module defines/imports none of the runner-owned names.

    ``Agent`` is the crux of the runner-only-construction invariant: if a
    pass module ever imported ``strands.Agent``, it could construct one
    directly, bypassing the runner's budgeting/chunking/recovery entirely.
    The remaining names are the runner's private recovery helpers -- their
    presence here would mean a duplicate implementation, not delegation.
    """
    for name in _RUNNER_OWNED_NAMES:
        assert not hasattr(module, name), (
            f"{module.__name__} must not define/import {name!r} -- that belongs "
            "solely to submission_pass_runner"
        )


def test_agent_construction_appears_only_in_the_shared_runner() -> None:
    """Source-level lock: ``Agent(`` appears only in submission_pass_runner.py
    among the runner and the three migrated pass modules."""
    assert "Agent(" in inspect.getsource(runner_mod)
    for module in _ADDITIVE_PASS_MODULES:
        assert "Agent(" not in inspect.getsource(module), (
            f"{module.__name__} constructs an Agent directly; it must delegate "
            "to submission_pass_runner.run_submission_pass instead"
        )


# ------------------------------------------------------------ delegates to runner


def test_architecture_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = arch_pass_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("architecture")
        return original(llm, **kwargs)

    monkeypatch.setattr(arch_pass_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    arch_pass_mod.find_architecture_and_redundancy_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t", architecture=_arch())
    )
    assert calls == ["architecture"]


def test_side_effect_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = side_pass_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("side_effect")
        return original(llm, **kwargs)

    monkeypatch.setattr(side_pass_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    side_pass_mod.find_side_effect_impact_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t")
    )
    assert calls == ["side_effect"]


def test_merged_pass_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    original = merged_pass_mod.run_submission_pass

    def _spy(llm, **kwargs):
        calls.append("merged")
        return original(llm, **kwargs)

    monkeypatch.setattr(merged_pass_mod, "run_submission_pass", _spy)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"architecture_findings": [], "side_effect_findings": []}

    merged_pass_mod.find_architecture_and_side_effect_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t", architecture=_arch())
    )
    assert calls == ["merged"]


# ------------------------------------------------------- overflow-alone recovery


def _overflow_budgets() -> MergedPassBudgets:
    # Generous per-call allowance: both files fit into one proactive batch,
    # so the overflow below is triggered explicitly by the scripted client
    # (mid-turn context overflow on the combined call), not by budgeting.
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

    result = arch_pass_mod.find_architecture_and_redundancy_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t", architecture=_arch())
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

    result = side_pass_mod.find_side_effect_impact_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t")
    )

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

    arch_findings, side_findings = merged_pass_mod.find_architecture_and_side_effect_issues(
        _Client(), CodeReviewInput(files=_files(), task_description="t", architecture=_arch())
    )

    assert {f.description for f in arch_findings} == {"finding for a.py", "finding for b.py"}
    assert side_findings == []
    assert call_count["n"] > 1
