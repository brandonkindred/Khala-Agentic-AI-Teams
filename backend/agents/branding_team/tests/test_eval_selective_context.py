"""Unit tests for the selective-context eval script's logic.

Covers the token-count heuristic, the full-context-phases derivation, the
``_phase_spec_context_override`` primitive, and ``_run_variant`` -- which
genuinely executes each phase via ``run_single_phase`` (dummy LLM, isolated
single-node graphs, no network) so both the "selective" and "full-context"
variants produce real per-phase output, not just prompt strings. Does not
exercise ``run_eval`` end-to-end against every sample mission -- that's run
manually per the script's docstring and is intentionally out of scope for
CI, matching #6969's "Out of Scope: CI integration of eval."
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandPhase
from branding_team.orchestrator import _PHASE_SPEC, BrandingTeamOrchestrator
from branding_team.scripts.eval_selective_context import (
    SAMPLE_MISSIONS,
    _approx_token_count,
    _full_context_phases,
    _phase_spec_context_override,
    _run_variant,
    main,
)
from branding_team.tests.conftest import make_mission
from llm_service.dummy_provider import force_dummy_llm_provider


def test_approx_token_count_empty_string() -> None:
    """Whitespace-only and empty inputs contribute zero tokens."""
    assert _approx_token_count("") == 0
    assert _approx_token_count("   ") == 0


def test_approx_token_count_positive_and_monotonic() -> None:
    """More words must never yield a smaller estimate than fewer words."""
    short = _approx_token_count("one two three")
    long = _approx_token_count("one two three four five six seven eight nine ten")
    assert short > 0
    assert long > short


def test_full_context_phases_governance_includes_all_upstream() -> None:
    """GOVERNANCE's full-context prefix is every phase that precedes it, in order."""
    assert _full_context_phases(BrandPhase.GOVERNANCE) == (
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
        BrandPhase.CHANNEL_ACTIVATION,
    )


def test_full_context_phases_strategic_core_has_none() -> None:
    """STRATEGIC_CORE is first in PHASE_ORDER, so it has no upstream phases at all."""
    assert _full_context_phases(BrandPhase.STRATEGIC_CORE) == ()


def test_phase_spec_context_override_restores_on_error() -> None:
    """The override must be undone in `finally` even when the wrapped block raises,
    so a failed comparison run can never leave _PHASE_SPEC permanently mutated for
    unrelated code running later in the same process.
    """
    original = _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases

    with pytest.raises(RuntimeError):
        with _phase_spec_context_override(BrandPhase.GOVERNANCE, ()):
            assert _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases == ()
            raise RuntimeError("boom")

    assert _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases == original


def test_run_variant_selective_excludes_channel_activation_context() -> None:
    """Integration check that _run_variant(full_context=False) genuinely goes
    through the real, non-monkeypatched _PHASE_SPEC -- GOVERNANCE's task
    string must never mention channel_activation or narrative_messaging,
    the acceptance criterion #6965 exists to enforce.
    """
    orchestrator = BrandingTeamOrchestrator()
    with force_dummy_llm_provider():
        _outputs, task_strings = _run_variant(orchestrator, make_mission(), full_context=False)

    governance_task = task_strings[BrandPhase.GOVERNANCE]
    assert BrandPhase.CHANNEL_ACTIVATION.value not in governance_task
    assert BrandPhase.NARRATIVE_MESSAGING.value not in governance_task
    assert BrandPhase.STRATEGIC_CORE.value in governance_task
    assert BrandPhase.VISUAL_IDENTITY.value in governance_task


def test_run_variant_full_context_includes_all_upstream() -> None:
    """Mirror image of the selective-variant check above: with full_context=True,
    GOVERNANCE's task string must mention every upstream phase, since the
    override widens context_phases to the full PHASE_ORDER prefix.
    """
    orchestrator = BrandingTeamOrchestrator()
    with force_dummy_llm_provider():
        _outputs, task_strings = _run_variant(orchestrator, make_mission(), full_context=True)

    governance_task = task_strings[BrandPhase.GOVERNANCE]
    assert BrandPhase.CHANNEL_ACTIVATION.value in governance_task
    assert BrandPhase.NARRATIVE_MESSAGING.value in governance_task
    assert BrandPhase.STRATEGIC_CORE.value in governance_task
    assert BrandPhase.VISUAL_IDENTITY.value in governance_task


def test_run_variant_full_context_task_is_never_shorter() -> None:
    """For every phase, the full-context task string is never shorter than the
    selective one -- full-context only ever adds upstream phase output back in,
    never removes any content the selective variant already included.
    """
    orchestrator = BrandingTeamOrchestrator()
    mission = make_mission()
    with force_dummy_llm_provider():
        _selective_outputs, selective_tasks = _run_variant(
            orchestrator, mission, full_context=False
        )
        _full_outputs, full_tasks = _run_variant(orchestrator, mission, full_context=True)

    for phase in PHASE_ORDER:
        assert len(full_tasks[phase]) >= len(selective_tasks[phase])


def test_run_variant_returns_real_output_for_every_phase() -> None:
    """_run_variant must produce a genuine, non-None output for every phase in
    PHASE_ORDER, not just build task-prompt strings -- both variants execute
    the real per-phase graph via run_single_phase.
    """
    orchestrator = BrandingTeamOrchestrator()
    with force_dummy_llm_provider():
        outputs, _task_strings = _run_variant(orchestrator, make_mission(), full_context=False)

    assert set(outputs.keys()) == set(PHASE_ORDER)
    assert all(output is not None for output in outputs.values())


def test_run_variant_restores_phase_spec_after_full_context() -> None:
    """The full-context variant temporarily widens every phase's context_phases;
    after _run_variant returns, _PHASE_SPEC must be back to its real, selective
    values so a later selective-variant call in the same process isn't corrupted.
    """
    orchestrator = BrandingTeamOrchestrator()
    originals = {phase: spec.context_phases for phase, spec in _PHASE_SPEC.items()}

    with force_dummy_llm_provider():
        _run_variant(orchestrator, make_mission(), full_context=True)

    for phase, spec in _PHASE_SPEC.items():
        assert spec.context_phases == originals[phase]


def test_main_no_mission_filter_runs_all_sample_missions(tmp_path) -> None:
    """With no --mission filter, main() must pass every SAMPLE_MISSIONS entry
    to run_eval and return 0 -- the CLI's default/happy path.
    """
    with patch("branding_team.scripts.eval_selective_context.run_eval") as mock_run_eval:
        mock_run_eval.return_value = []
        exit_code = main(["--output-dir", str(tmp_path)])

    assert exit_code == 0
    mock_run_eval.assert_called_once_with(missions=SAMPLE_MISSIONS, output_dir=tmp_path)


def test_main_mission_filter_no_match_returns_one(tmp_path) -> None:
    """A --mission substring matching no sample mission must abort with exit
    code 1 before ever calling run_eval, per main()'s documented contract.
    """
    with patch("branding_team.scripts.eval_selective_context.run_eval") as mock_run_eval:
        exit_code = main(["--output-dir", str(tmp_path), "--mission", "no-such-company"])

    assert exit_code == 1
    mock_run_eval.assert_not_called()


def test_main_mission_filter_matches_case_insensitive_substring(tmp_path) -> None:
    """--mission filters SAMPLE_MISSIONS by a case-insensitive substring of
    company_name, passing only the matches through to run_eval.
    """
    with patch("branding_team.scripts.eval_selective_context.run_eval") as mock_run_eval:
        mock_run_eval.return_value = []
        exit_code = main(["--output-dir", str(tmp_path), "--mission", "northwind"])

    assert exit_code == 0
    called_missions = mock_run_eval.call_args.kwargs["missions"]
    assert called_missions
    assert all("northwind" in m.company_name.lower() for m in called_missions)
