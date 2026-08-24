"""Unit tests for the selective-context eval script's logic.

Covers the token-count heuristic, the full-context-phases derivation, the
``_phase_spec_context_override`` primitive, ``_run_variant`` -- which
genuinely executes each phase via ``run_single_phase`` (dummy LLM, isolated
single-node graphs, no network) so both the "selective" and "full-context"
variants produce real per-phase output, not just prompt strings --
``run_eval``'s output-file disambiguation for duplicate mission names, and
``main``'s CLI argument parsing and mission filtering. Does not exercise
``run_eval`` end-to-end against every sample mission -- that's run manually
per the script's docstring and is intentionally out of scope for CI,
matching the eval task's "Out of Scope: CI integration of eval."
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import branding_team.scripts.eval_selective_context as eval_ctx
from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandPhase
from branding_team.orchestrator import _PHASE_SPEC, BrandingTeamOrchestrator
from branding_team.scripts.eval_selective_context import (
    QUALITY_REGRESSION_THRESHOLD_PTS,
    SAMPLE_MISSIONS,
    PhaseQualityComparison,
    _approx_token_count,
    _diverges_from_full_context,
    _first_diverging_phase,
    _full_context_phases,
    _phase_spec_context_override,
    _print_quality_report,
    _run_variant,
    _run_variant_pair,
    _slugify,
    _write_markdown_report,
    main,
    run_eval,
)
from branding_team.scripts.quality_judge import PairedPhaseQualityScore, PhaseQualityScore
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


def test_diverges_from_full_context_true_only_for_governance() -> None:
    """Today only GOVERNANCE's selective context_phases differs from its full-context
    prefix. STRATEGIC_CORE has no upstream phases at all, so it has no full-context
    variant to diverge from and is excluded from this loop; every remaining phase's
    selective and full task prompts are identical.
    """
    for phase in PHASE_ORDER:
        if phase == BrandPhase.STRATEGIC_CORE:
            continue
        expected = phase == BrandPhase.GOVERNANCE
        assert _diverges_from_full_context(phase) is expected


def test_first_diverging_phase_is_governance() -> None:
    """GOVERNANCE is the only (and therefore first) diverging phase today."""
    assert _first_diverging_phase() == BrandPhase.GOVERNANCE


def test_run_variant_pair_shares_non_diverging_phase_outputs() -> None:
    """Every phase before the first divergence (narrative_messaging, visual_identity,
    channel_activation, strategic_core) must be the identical shared object between
    the selective and full-context results -- not two independently regenerated
    outputs -- so a live run can never manufacture a false delta on those phases.
    """
    orchestrator = BrandingTeamOrchestrator()
    mission = make_mission()
    with force_dummy_llm_provider():
        selective_outputs, selective_tasks, full_outputs, full_tasks = _run_variant_pair(
            orchestrator, mission
        )

    fork_phase = _first_diverging_phase()
    fork_idx = PHASE_ORDER.index(fork_phase)
    for phase in PHASE_ORDER[:fork_idx]:
        assert selective_outputs[phase] is full_outputs[phase]
        assert selective_tasks[phase] == full_tasks[phase]

    # The diverging phase itself must still be judged independently per variant.
    assert selective_outputs[fork_phase] is not full_outputs[fork_phase]


def test_run_variant_pair_governance_task_strings_match_run_variant() -> None:
    """_run_variant_pair's GOVERNANCE task strings must match what independently
    calling _run_variant(full_context=False/True) would build -- the shared-prefix
    optimization must not change what's actually sent to the LLM for the phase
    that does diverge, only avoid redundant upstream regeneration.
    """
    mission = make_mission()
    with force_dummy_llm_provider():
        pair_orchestrator = BrandingTeamOrchestrator()
        _sel_outputs, sel_tasks, _full_outputs, full_tasks = _run_variant_pair(
            pair_orchestrator, mission
        )

        solo_orchestrator = BrandingTeamOrchestrator()
        _outputs_sel, solo_selective_tasks = _run_variant(
            solo_orchestrator, mission, full_context=False
        )
        _outputs_full, solo_full_tasks = _run_variant(solo_orchestrator, mission, full_context=True)

    assert sel_tasks[BrandPhase.GOVERNANCE] == solo_selective_tasks[BrandPhase.GOVERNANCE]
    assert full_tasks[BrandPhase.GOVERNANCE] == solo_full_tasks[BrandPhase.GOVERNANCE]


def test_run_variant_pair_restores_phase_spec() -> None:
    """_PHASE_SPEC must be back to its real, selective values after _run_variant_pair
    returns, exactly like _run_variant guarantees.
    """
    orchestrator = BrandingTeamOrchestrator()
    originals = {phase: spec.context_phases for phase, spec in _PHASE_SPEC.items()}

    with force_dummy_llm_provider():
        _run_variant_pair(orchestrator, make_mission())

    for phase, spec in _PHASE_SPEC.items():
        assert spec.context_phases == originals[phase]


def test_run_eval_live_mode_rejects_dummy_provider(tmp_path) -> None:
    """live=True must fail fast with a clear error if LLM_PROVIDER=dummy is still
    set in the environment, rather than silently running the dummy stub and
    reporting a meaningless PASS (Codex P1 finding).
    """
    with force_dummy_llm_provider():
        with pytest.raises(RuntimeError, match="DummyLLMClient"):
            run_eval(missions=[make_mission()], output_dir=tmp_path, live=True)


def test_run_eval_disambiguates_duplicate_company_name_slugs(tmp_path) -> None:
    """Two missions sharing a company_name must each get their own output
    file (`-2` suffix on the second) instead of the second silently
    overwriting the first's Phase 4/5 results.
    """
    first = make_mission()
    second = first.model_copy()

    run_eval(missions=[first, second], output_dir=tmp_path)

    slug = _slugify(first.company_name)
    written = sorted(p.name for p in tmp_path.glob("*.json"))
    assert f"{slug}.json" in written
    assert f"{slug}-2.json" in written


def test_run_eval_default_dummy_mode_has_no_quality_regressions(tmp_path) -> None:
    """Under the default (dummy) mode, the judge always scores both variants
    identically, so run_eval must return a non-empty quality_comparisons list
    with zero regressions on every comparison.
    """
    _comparisons, quality_comparisons = run_eval(missions=[make_mission()], output_dir=tmp_path)

    assert quality_comparisons
    assert all(c.regressions() == [] for c in quality_comparisons)
    phases = {c.phase for c in quality_comparisons}
    # _JUDGED_PHASES (not divergence) is the single source of truth for which
    # phases run_eval judges at all -- both are judged regardless of whether
    # their context actually diverges (see the two tests below for that).
    assert phases == set(eval_ctx._JUDGED_PHASES)


def test_run_eval_judges_shared_phase_output_only_once(tmp_path) -> None:
    """CHANNEL_ACTIVATION's context doesn't diverge between variants, so its output
    is the identical shared object in both -- run_eval must call the single-output
    judge on it exactly once (never twice) and never route it through the paired
    judge call at all.
    """
    # CHANNEL_ACTIVATION is asserted directly (not derived from
    # _first_diverging_phase()) because this test's whole point is pinning
    # down *which* judged phase is non-diverging today; if that ever changes,
    # this assertion should fail loudly rather than silently track the change.
    with patch(
        "branding_team.scripts.eval_selective_context.score_phase_output",
        wraps=eval_ctx.score_phase_output,
    ) as mock_score:
        run_eval(missions=[make_mission()], output_dir=tmp_path)

    judged_phases = [call.kwargs["phase"] for call in mock_score.call_args_list]
    assert judged_phases == [BrandPhase.CHANNEL_ACTIVATION]


def test_run_eval_judges_diverging_phase_with_both_paired_orderings(tmp_path) -> None:
    """GOVERNANCE's context does diverge between variants, so run_eval must score
    both candidates through exactly two score_phase_output_pair calls -- one with
    selective as OUTPUT A, one with full as OUTPUT A -- to average out any
    positional bias, never a single call or two separate score_phase_output calls.
    """
    # GOVERNANCE is asserted directly for the same reason as the shared-phase
    # test above: it mirrors test_first_diverging_phase_is_governance(), which
    # documents today's divergence point, so a mismatch here signals a real
    # behavior change rather than a stale assumption.
    with patch(
        "branding_team.scripts.eval_selective_context.score_phase_output_pair",
        wraps=eval_ctx.score_phase_output_pair,
    ) as mock_pair:
        run_eval(missions=[make_mission()], output_dir=tmp_path)

    judged_phases = [call.kwargs["phase"] for call in mock_pair.call_args_list]
    assert judged_phases == [BrandPhase.GOVERNANCE, BrandPhase.GOVERNANCE]
    output_a_args = [call.kwargs["output_a"] for call in mock_pair.call_args_list]
    output_b_args = [call.kwargs["output_b"] for call in mock_pair.call_args_list]
    # The two calls must use opposite orderings of the same two candidates.
    assert output_a_args[0] is output_b_args[1]
    assert output_b_args[0] is output_a_args[1]


def test_run_eval_averages_both_paired_orderings_to_cancel_positional_bias(tmp_path) -> None:
    """A judge with a fixed A-over-B positional bias must not skew the final
    selective/full scores: averaging the forward and reverse orderings should
    cancel the bias and leave both candidates with the same averaged score,
    since a single ordering alone would report a manufactured one-point delta.
    """
    # Simulates a judge that always scores whichever candidate is OUTPUT A one
    # point higher than whichever is OUTPUT B, regardless of actual content.
    biased_high = PhaseQualityScore(
        strategic_coherence=5, completeness=5, brand_consistency=5, rationale="a-slot"
    )
    biased_low = PhaseQualityScore(
        strategic_coherence=4, completeness=4, brand_consistency=4, rationale="b-slot"
    )

    def fake_paired(*_args, **kwargs):
        # Whichever candidate is passed as output_a gets the higher score.
        return PairedPhaseQualityScore(output_a=biased_high, output_b=biased_low)

    with patch(
        "branding_team.scripts.eval_selective_context.score_phase_output_pair",
        side_effect=fake_paired,
    ):
        _comparisons, quality_comparisons = run_eval(missions=[make_mission()], output_dir=tmp_path)

    governance = next(c for c in quality_comparisons if c.phase == BrandPhase.GOVERNANCE)
    # Averaging (5+4)/2 = 4.5 for both sides -- the one-point positional bias
    # cancels out exactly (not merely to the same rounded integer) and reports
    # zero regression, not a false one. The average is preserved as a genuine
    # half-point float, never rounded away (see test_average_phase_quality_score
    # _preserves_half_point_averages for why that distinction matters).
    assert governance.selective.strategic_coherence == 4.5
    assert governance.full.strategic_coherence == 4.5
    assert governance.regressions() == []


def test_run_eval_writes_markdown_report(tmp_path) -> None:
    """run_eval's caller (main()) writes quality_report.md via
    _write_markdown_report; verify it contains both the token table and the
    quality-score table for a real run.
    """
    comparisons, quality_comparisons = run_eval(missions=[make_mission()], output_dir=tmp_path)

    report_path = _write_markdown_report(comparisons, quality_comparisons, tmp_path)

    assert report_path == tmp_path / "quality_report.md"
    content = report_path.read_text(encoding="utf-8")
    assert "# Selective-Context Eval Report" in content
    assert "## Token counts" in content
    assert "## LLM-as-judge quality scores" in content
    assert "## Regression verdict" in content
    assert "No regressions" in content


def _quality_score(**overrides) -> PhaseQualityScore:
    values = {"strategic_coherence": 5, "completeness": 5, "brand_consistency": 5, "rationale": ""}
    values.update(overrides)
    return PhaseQualityScore(**values)


def _comparison_score(**overrides) -> eval_ctx.ComparisonScore:
    values = {"strategic_coherence": 5, "completeness": 5, "brand_consistency": 5, "rationale": ""}
    values.update(overrides)
    return eval_ctx.ComparisonScore(**values)


def test_average_phase_quality_score_averages_each_dimension() -> None:
    """_average_phase_quality_score must average each dimension independently,
    not just one field.
    """
    a = _quality_score(strategic_coherence=5, completeness=3, brand_consistency=1)
    b = _quality_score(strategic_coherence=3, completeness=3, brand_consistency=5)

    averaged = eval_ctx._average_phase_quality_score(a, b)

    assert averaged.strategic_coherence == 4  # (5+3)/2 = 4
    assert averaged.completeness == 3  # (3+3)/2 = 3
    assert averaged.brand_consistency == 3  # (1+5)/2 = 3


def test_average_phase_quality_score_preserves_half_point_averages() -> None:
    """A genuine half-point average (e.g. 3 and 4 -> 3.5) must be preserved
    exactly, not rounded to the nearest integer -- rounding it away before
    the 0.5-point regression threshold sees it could hide a real one-point
    regression (two averages that both round to the same integer) or
    manufacture a false one (a true 0.5-point gap rounding to a full point).
    """
    a = _quality_score(strategic_coherence=3)
    b = _quality_score(strategic_coherence=4)

    averaged = eval_ctx._average_phase_quality_score(a, b)

    assert averaged.strategic_coherence == 3.5


def test_phase_quality_comparison_detects_regression_hidden_by_rounding() -> None:
    """Reproduces the exact bug this float-preservation fix closes: selective
    averaging to 3.5 and full averaging to 4.5 is a true 1.0-point regression
    (exceeds the 0.5 threshold), but round()-ing each to the nearest integer
    would make both 4 -- a delta of 0 -- and silently hide it.
    """
    comparison = PhaseQualityComparison(
        mission_name="Acme",
        phase=BrandPhase.GOVERNANCE,
        selective=_comparison_score(strategic_coherence=3.5),
        full=_comparison_score(strategic_coherence=4.5),
    )
    assert comparison.delta("strategic_coherence") == 1.0
    assert comparison.regressions() == ["strategic_coherence"]


def test_phase_quality_comparison_no_regression_when_scores_are_equal() -> None:
    """Identical scores (delta 0, well within the 0.5-point threshold) are never a regression."""
    assert QUALITY_REGRESSION_THRESHOLD_PTS == 0.5
    comparison = PhaseQualityComparison(
        mission_name="Acme",
        phase=BrandPhase.GOVERNANCE,
        selective=_comparison_score(strategic_coherence=4),
        full=_comparison_score(strategic_coherence=4),
    )
    assert comparison.regressions() == []


def test_phase_quality_comparison_flags_regression_past_threshold() -> None:
    """A one-point drop on a single dimension exceeds the 0.5-point threshold
    and must be flagged by name.
    """
    comparison = PhaseQualityComparison(
        mission_name="Acme",
        phase=BrandPhase.GOVERNANCE,
        selective=_comparison_score(strategic_coherence=3),
        full=_comparison_score(strategic_coherence=5),
    )
    assert comparison.regressions() == ["strategic_coherence"]


def test_phase_quality_comparison_flags_multiple_regressed_dimensions() -> None:
    """Every dimension that regresses past the threshold is reported, not just the first."""
    comparison = PhaseQualityComparison(
        mission_name="Acme",
        phase=BrandPhase.CHANNEL_ACTIVATION,
        selective=_comparison_score(strategic_coherence=2, completeness=2, brand_consistency=5),
        full=_comparison_score(strategic_coherence=5, completeness=5, brand_consistency=5),
    )
    assert comparison.regressions() == ["strategic_coherence", "completeness"]


def test_fmt_score_strips_trailing_zero_but_keeps_fractional_precision() -> None:
    """_fmt_score must render a whole number without a trailing '.0' but keep
    genuine fractional averages visible, e.g. for the markdown/console report.
    """
    assert eval_ctx._fmt_score(5.0) == "5"
    assert eval_ctx._fmt_score(3.5) == "3.5"


def test_phase_quality_comparison_delta_rejects_invalid_dimension() -> None:
    """delta() must raise ValueError for a dimension outside _QUALITY_DIMENSIONS
    rather than an unclear AttributeError from getattr.
    """
    comparison = PhaseQualityComparison(
        mission_name="Acme",
        phase=BrandPhase.GOVERNANCE,
        selective=_comparison_score(),
        full=_comparison_score(),
    )
    with pytest.raises(ValueError, match="Invalid dimension"):
        comparison.delta("not_a_real_dimension")


def test_print_quality_report_handles_empty_list(capsys) -> None:
    """_print_quality_report must not raise on an empty comparisons list and
    must state that nothing was collected.
    """
    _print_quality_report([])
    out = capsys.readouterr().out
    assert "No quality comparisons collected." in out


def test_main_no_mission_filter_runs_all_sample_missions(tmp_path) -> None:
    """With no --mission filter, main() must pass every SAMPLE_MISSIONS entry
    to run_eval and return 0 -- the CLI's default/happy path.
    """
    with patch("branding_team.scripts.eval_selective_context.run_eval") as mock_run_eval:
        mock_run_eval.return_value = ([], [])
        exit_code = main(["--output-dir", str(tmp_path)])

    assert exit_code == 0
    mock_run_eval.assert_called_once_with(missions=SAMPLE_MISSIONS, output_dir=tmp_path, live=False)


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
        mock_run_eval.return_value = ([], [])
        exit_code = main(["--output-dir", str(tmp_path), "--mission", "northwind"])

    assert exit_code == 0
    called_missions = mock_run_eval.call_args.kwargs["missions"]
    assert called_missions
    assert all("northwind" in m.company_name.lower() for m in called_missions)


def test_main_live_flag_forwarded_to_run_eval(tmp_path) -> None:
    """--live must be forwarded to run_eval as live=True; omitting it defaults to False
    (already covered by the other main() tests above).
    """
    with patch("branding_team.scripts.eval_selective_context.run_eval") as mock_run_eval:
        mock_run_eval.return_value = ([], [])
        exit_code = main(["--output-dir", str(tmp_path), "--live"])

    assert exit_code == 0
    mock_run_eval.assert_called_once_with(missions=SAMPLE_MISSIONS, output_dir=tmp_path, live=True)
