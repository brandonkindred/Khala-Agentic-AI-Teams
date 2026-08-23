"""Unit tests for the selective-context eval script's pure/deterministic logic.

Covers the token-count heuristic and the full-vs-selective prompt comparison
helper (``compare_phase_prompts``). Does not run the LLM-driven pipeline
(``run_eval``) -- that's exercised manually per the script's docstring and is
intentionally out of scope for CI, matching #6969's "Out of Scope: CI
integration of eval."
"""

from __future__ import annotations

import pytest

from branding_team.models import BrandPhase
from branding_team.orchestrator import _PHASE_SPEC
from branding_team.scripts.eval_selective_context import (
    _approx_token_count,
    _full_context_phases,
    compare_phase_prompts,
)
from branding_team.tests.conftest import make_mission


def test_approx_token_count_empty_string() -> None:
    assert _approx_token_count("") == 0
    assert _approx_token_count("   ") == 0


def test_approx_token_count_positive_and_monotonic() -> None:
    short = _approx_token_count("one two three")
    long = _approx_token_count("one two three four five six seven eight nine ten")
    assert short > 0
    assert long > short


def test_full_context_phases_governance_includes_all_upstream() -> None:
    assert _full_context_phases(BrandPhase.GOVERNANCE) == (
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
        BrandPhase.CHANNEL_ACTIVATION,
    )


def test_full_context_phases_strategic_core_has_none() -> None:
    assert _full_context_phases(BrandPhase.STRATEGIC_CORE) == ()


def test_compare_phase_prompts_full_is_never_smaller_than_selective() -> None:
    mission = make_mission()
    prior_outputs = {
        BrandPhase.STRATEGIC_CORE.value: {"marker": "STRATEGIC_MARKER"},
        BrandPhase.NARRATIVE_MESSAGING.value: {"marker": "NARRATIVE_MARKER"},
        BrandPhase.VISUAL_IDENTITY.value: {"marker": "VISUAL_MARKER"},
        BrandPhase.CHANNEL_ACTIVATION.value: {
            "channel_guidelines": [{"channel": "website", "marker": "CHANNEL_MARKER"}]
        },
    }

    comparison = compare_phase_prompts(mission, BrandPhase.GOVERNANCE, prior_outputs)

    assert comparison.full_tokens >= comparison.selective_tokens
    assert comparison.reduction_pct > 0


def test_compare_phase_prompts_does_not_mutate_phase_spec() -> None:
    original = _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases

    compare_phase_prompts(make_mission(), BrandPhase.GOVERNANCE, {})

    assert _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases == original


def test_compare_phase_prompts_restores_spec_even_on_error(monkeypatch) -> None:
    """The second (full-context) ``_phase_task`` call fails after ``_PHASE_SPEC``
    has already been mutated; the ``finally`` in ``compare_phase_prompts`` must
    still restore the original ``context_phases`` before the exception propagates.
    """
    original = _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases

    from branding_team import orchestrator as orchestrator_module

    call_count = {"n": 0}
    real_method = orchestrator_module.BrandingTeamOrchestrator._phase_task

    def _fail_on_second_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated failure building the full-context task string")
        return real_method(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module.BrandingTeamOrchestrator,
        "_phase_task",
        staticmethod(_fail_on_second_call),
    )

    with pytest.raises(RuntimeError):
        compare_phase_prompts(make_mission(), BrandPhase.GOVERNANCE, {})

    assert _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases == original
