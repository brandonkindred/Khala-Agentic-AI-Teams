"""Compare full-context vs selective-context pipeline prompts and outputs.

For each sample mission in ``branding_team.tests.eval_fixtures.sample_missions``,
runs the branding pipeline once to obtain real per-phase outputs, then builds
each downstream phase's task prompt two ways:

- **selective**: using the real ``_PHASE_SPEC[phase].context_phases`` set by
  the selective-context filtering (see ``orchestrator._phase_task``).
- **full**: as if no filtering existed -- every upstream phase's output is
  included, matching pre-selective-context behavior.

Reports a token-count table for both variants plus the percentage reduction,
and saves each mission's Phase 4 (channel_activation) and Phase 5
(governance) outputs to a JSON file for manual quality comparison.

Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=.:agents python3 -m branding_team.scripts.eval_selective_context

Requires no Postgres, Temporal, or live LLM provider: ``run_eval`` forces
every agent it constructs through the deterministic dummy stub client (see
``llm_service.testing.force_dummy_llm_provider``), regardless of any live
provider selected in the Postgres-backed runtime config or an inherited
``LLM_PROVIDER`` environment value.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandingMission, BrandPhase
from branding_team.orchestrator import _PHASE_SPEC, BrandingTeamOrchestrator
from branding_team.tests.eval_fixtures.sample_missions import SAMPLE_MISSIONS
from llm_service.testing import force_dummy_llm_provider

PHASE5_REDUCTION_TARGET_PCT = 40.0

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "eval_results"


def _approx_token_count(text: str) -> int:
    """Approximate the LLM token count of *text*.

    No tokenizer dependency exists elsewhere in this repo, and adding one
    solely for a one-off eval script is out of scope. This heuristic (~1.3
    tokens per whitespace-delimited word, a common rule of thumb for
    English prose and JSON) is only ever used to compare two prompt
    variants built from the same underlying content, so the exact token
    count matters less than the two counts being computed consistently.

    Preconditions:
        None.
    Postconditions:
        Returns 0 for an empty/whitespace-only string; otherwise a positive
        integer estimate.
    """
    words = text.split()
    if not words:
        return 0
    return max(1, round(len(words) * 1.3))


def _full_context_phases(phase: BrandPhase) -> tuple[BrandPhase, ...]:
    """Return every phase preceding *phase* in ``PHASE_ORDER``.

    This is what ``context_phases`` would be if selective-context filtering
    did not exist -- every upstream phase's output is included.

    Preconditions:
        ``phase`` is a member of ``PHASE_ORDER``.
    Postconditions:
        Returns a tuple of the phases strictly before ``phase`` in
        ``PHASE_ORDER``, in order.
    """
    idx = PHASE_ORDER.index(phase)
    return tuple(PHASE_ORDER[:idx])


def _slugify(name: str) -> str:
    """Convert a mission name to a filesystem-safe slug for an output filename.

    Preconditions:
        None.
    Postconditions:
        Returns a lowercase, hyphen-separated slug with no leading/trailing
        hyphens; falls back to ``"mission"`` when the input has no
        alphanumeric characters (so the caller never gets an empty filename
        stem).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "mission"


@dataclass
class PhasePromptComparison:
    """Token counts for one phase's task prompt under both context variants.

    Attributes:
        mission_name: The mission this comparison was computed for.
        phase: The branding phase whose task prompt was compared.
        selective_tokens: Approximate token count of the real, production
            selective-context task prompt (see ``_run_variant``).
        full_tokens: Approximate token count of the same phase's task
            prompt with every upstream phase's output included unfiltered.
    """

    mission_name: str
    phase: BrandPhase
    selective_tokens: int
    full_tokens: int

    @property
    def reduction_pct(self) -> float:
        """Percentage of tokens the selective variant removes versus the full variant.

        Postconditions:
            Returns ``0.0`` when ``full_tokens`` is ``0`` (avoids division
            by zero); otherwise returns ``100 * (full - selective) / full``,
            which is negative if the selective variant is somehow larger.
        """
        if self.full_tokens == 0:
            return 0.0
        return 100.0 * (self.full_tokens - self.selective_tokens) / self.full_tokens


@contextlib.contextmanager
def _phase_spec_context_override(phase: BrandPhase, context_phases: tuple[BrandPhase, ...]):
    """Temporarily replace ``_PHASE_SPEC[phase].context_phases``.

    Preconditions:
        ``phase`` is a key of ``_PHASE_SPEC``.
    Postconditions:
        ``_PHASE_SPEC[phase]`` is restored to its original value on exit,
        even if the wrapped block raises.
    """
    original = _PHASE_SPEC[phase]
    _PHASE_SPEC[phase] = original._replace(context_phases=context_phases)
    try:
        yield
    finally:
        _PHASE_SPEC[phase] = original


def _run_variant(
    orchestrator: BrandingTeamOrchestrator, mission: BrandingMission, *, full_context: bool
) -> tuple[dict[BrandPhase, object], dict[BrandPhase, str]]:
    """Run every phase for *mission* via ``run_single_phase``, genuinely executing one variant.

    Calling ``run_single_phase`` phase by phase (mirroring the loop
    ``orchestrator._run_phases_with_cache`` runs in production) rather than
    ``orchestrator.run()`` means every phase's task prompt is built by the
    real ``_phase_task`` -- the actual selective-context filtering logic --
    and ``run_single_phase`` takes no ``phase_cache`` parameter at all (it's
    what the Temporal activity calls directly), so this never touches the
    shared, Redis-backed-when-configured ``PhaseOutputCache`` either.

    Preconditions:
        ``full_context`` selects which variant to run: ``False`` uses the
        real ``_PHASE_SPEC[phase].context_phases`` for every phase (the
        actual production/selective behavior); ``True`` temporarily widens
        every phase's ``context_phases`` to its full upstream prefix (via
        ``_full_context_phases``), simulating pre-selective-context behavior.
    Postconditions:
        - Returns ``(outputs, task_strings)``: ``outputs`` maps every
          ``BrandPhase`` in ``PHASE_ORDER`` to its real, non-degraded output
          model; ``task_strings`` maps each phase to the exact task string
          ``_phase_task`` built for it under this variant.
        - ``_PHASE_SPEC`` is left unmodified on return, even if a phase
          invocation raises.
    Raises:
        RuntimeError: if any phase's ``run_single_phase`` call reports
            ``degraded=True`` (its structured output could not be parsed
            and was defaulted to a bare, field-empty ``model_cls()``
            instead). A degraded output cannot be trusted for token-count
            or quality comparison -- silently accepting it risks reporting
            a false PASS/FAIL and saving a placeholder as if it were a real
            result, so this aborts the variant loudly instead.
    """
    outputs: dict[BrandPhase, object] = {}
    task_strings: dict[BrandPhase, str] = {}
    prior_outputs: dict[str, dict] = {}

    for phase in PHASE_ORDER:
        context_phases = (
            _full_context_phases(phase) if full_context else _PHASE_SPEC[phase].context_phases
        )
        with _phase_spec_context_override(phase, context_phases):
            task_strings[phase] = BrandingTeamOrchestrator._phase_task(  # noqa: SLF001
                mission, phase, dict(prior_outputs)
            )
            output, degraded = orchestrator.run_single_phase(mission, phase, prior_outputs)
        if degraded:
            variant = "full-context" if full_context else "selective"
            raise RuntimeError(
                f"Phase {phase.value!r} degraded to a default-constructed output for "
                f"mission {mission.company_name!r} ({variant} variant) -- aborting eval "
                "rather than reporting results derived from a placeholder output."
            )
        outputs[phase] = output
        prior_outputs[phase.value] = output.model_dump(mode="json")

    return outputs, task_strings


def run_eval(
    missions: Optional[list[BrandingMission]] = None, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> list[PhasePromptComparison]:
    """Run the full-vs-selective-context eval over *missions* and save Phase 4/5 outputs.

    Preconditions:
        ``missions`` is an iterable of ``BrandingMission`` instances, or
        ``None`` (defaults to ``SAMPLE_MISSIONS``).
    Postconditions:
        - Returns one ``PhasePromptComparison`` per (mission, phase) pair for
          every phase after ``STRATEGIC_CORE`` (which never filters).
        - Writes each mission's Phase 4 and Phase 5 outputs, under both the
          ``selective`` and ``full_context`` variants, to
          ``output_dir/<mission-slug>.json`` -- each produced by its own
          real, independent phase execution against genuinely different
          task prompts (not a shared prompt-string-only comparison). Under
          the forced dummy client the two variants' output *content* will
          typically be identical regardless of prompt differences --
          ``DummyLLMClient`` replies from an agent's output schema, not its
          prompt text -- so this script validates that the real
          selective-context code path runs (and measures its prompt-size
          effect), not output quality; a live-provider run swapping in for
          ``force_dummy_llm_provider`` would be needed for actual
          LLM-as-judge or manual quality comparison, which issue #6969
          explicitly leaves out of scope.
        - Every phase genuinely executes through the forced dummy client for
          every mission in this call, via ``_run_variant`` -> ``run_single_phase``
          (never ``orchestrator.run()``'s cached thread path or its
          ``_use_monolithic`` escape hatch): ``run_single_phase`` takes no
          ``phase_cache`` parameter at all, so this never touches the
          shared, Redis-backed-when-configured ``PhaseOutputCache``, and it
          genuinely calls the real ``_phase_task`` selective-context
          filtering logic for both variants (unlike the monolithic graph
          path, which wires phases via Strands edges and never calls
          ``_phase_task`` at all).
    """
    missions = list(missions) if missions is not None else SAMPLE_MISSIONS
    output_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = BrandingTeamOrchestrator()
    comparisons: list[PhasePromptComparison] = []

    with force_dummy_llm_provider():
        for mission in missions:
            selective_outputs, selective_tasks = _run_variant(
                orchestrator, mission, full_context=False
            )
            full_outputs, full_tasks = _run_variant(orchestrator, mission, full_context=True)

            for phase in PHASE_ORDER:
                if phase == BrandPhase.STRATEGIC_CORE:
                    continue
                comparisons.append(
                    PhasePromptComparison(
                        mission_name=mission.company_name,
                        phase=phase,
                        selective_tokens=_approx_token_count(selective_tasks[phase]),
                        full_tokens=_approx_token_count(full_tasks[phase]),
                    )
                )

            eval_record = {
                "mission": mission.company_name,
                "selective": {
                    "channel_activation": selective_outputs[
                        BrandPhase.CHANNEL_ACTIVATION
                    ].model_dump(mode="json"),
                    "governance": selective_outputs[BrandPhase.GOVERNANCE].model_dump(mode="json"),
                },
                "full_context": {
                    "channel_activation": full_outputs[BrandPhase.CHANNEL_ACTIVATION].model_dump(
                        mode="json"
                    ),
                    "governance": full_outputs[BrandPhase.GOVERNANCE].model_dump(mode="json"),
                },
            }
            out_path = output_dir / f"{_slugify(mission.company_name)}.json"
            out_path.write_text(json.dumps(eval_record, indent=2, default=str))

    return comparisons


def _print_report(comparisons: list[PhasePromptComparison]) -> None:
    """Print the per-phase token-count table and the Phase 5 pass/fail verdict.

    Preconditions:
        ``comparisons`` is the list ``run_eval`` returns (may be empty, or
        contain zero ``GOVERNANCE`` entries if every mission passed had no
        governance phase -- not expected in normal use, but handled).
    Postconditions:
        Prints one table row per comparison, then either "No GOVERNANCE
        (Phase 5) comparisons collected." (if none exist) or the average
        Phase 5 reduction percentage across missions and a PASS/FAIL verdict
        against ``PHASE5_REDUCTION_TARGET_PCT``. Writes to stdout only;
        returns nothing.
    """
    print(f"\n{'Mission':<32}{'Phase':<22}{'Selective':>12}{'Full':>10}{'Reduction':>12}")
    print("-" * 88)
    for c in comparisons:
        print(
            f"{c.mission_name:<32}{c.phase.value:<22}{c.selective_tokens:>12}"
            f"{c.full_tokens:>10}{c.reduction_pct:>11.1f}%"
        )

    governance = [c for c in comparisons if c.phase == BrandPhase.GOVERNANCE]
    if not governance:
        print("\nNo GOVERNANCE (Phase 5) comparisons collected.")
        return

    avg_selective = sum(c.selective_tokens for c in governance) / len(governance)
    avg_full = sum(c.full_tokens for c in governance) / len(governance)
    avg_reduction = 100.0 * (avg_full - avg_selective) / avg_full if avg_full else 0.0

    print("-" * 88)
    print(
        f"Phase 5 (governance) average reduction: {avg_reduction:.1f}% "
        f"(target: >= {PHASE5_REDUCTION_TARGET_PCT:.0f}%)"
    )
    verdict = "PASS" if avg_reduction >= PHASE5_REDUCTION_TARGET_PCT else "FAIL"
    print(
        f"Acceptance criterion (>= {PHASE5_REDUCTION_TARGET_PCT:.0f}% Phase 5 reduction): {verdict}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Run the eval script's CLI entry point.

    Parses ``--output-dir`` and ``--mission``, runs the eval over the
    selected sample missions, and prints the report.

    Preconditions:
        ``argv`` is a list of CLI argument strings, or ``None`` to use
        ``sys.argv[1:]`` (``argparse``'s default).
    Postconditions:
        Returns ``0`` on success. Returns ``1`` without running the eval if
        ``--mission`` matches no sample mission (prints the reason to
        stderr). On success, writes one JSON file per selected mission to
        ``--output-dir`` (see ``run_eval``) and prints the comparison table
        and verdict to stdout (see ``_print_report``).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write per-mission Phase 4/5 output JSON files (default: eval_results/ next to this script).",
    )
    parser.add_argument(
        "--mission",
        default=None,
        help="Case-insensitive substring filter on company_name; runs only matching sample missions.",
    )
    args = parser.parse_args(argv)

    missions = SAMPLE_MISSIONS
    if args.mission:
        needle = args.mission.lower()
        missions = [m for m in SAMPLE_MISSIONS if needle in m.company_name.lower()]
        if not missions:
            print(f"No sample mission matches --mission={args.mission!r}", file=sys.stderr)
            return 1

    comparisons = run_eval(missions=missions, output_dir=args.output_dir)
    _print_report(comparisons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
