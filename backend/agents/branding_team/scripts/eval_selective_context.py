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
``_force_dummy_llm_provider``), regardless of any live provider selected in
the Postgres-backed runtime config or an inherited ``LLM_PROVIDER``
environment value.
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
from llm_service import config as _llm_config
from shared.hitl.models import HumanReview

PHASE5_REDUCTION_TARGET_PCT = 40.0

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "eval_results"


@contextlib.contextmanager
def _force_dummy_llm_provider():
    """Force every agent constructed inside this block through the dummy stub client.

    ``resolve_provider()`` prefers a live provider selected via the
    Postgres-backed runtime config over ``LLM_PROVIDER``, and every call site
    (``llm_service.factory``, ``llm_service.strands_provider``) reads it as a
    module attribute (``llm_config.resolve_provider()``), never via a
    ``from ... import resolve_provider`` local binding -- so overriding the
    attribute here forces every agent this script constructs through the
    dummy stub client (no live LLM call, network access, or spend) even when
    Postgres holds a live provider or the caller's shell already exported a
    non-dummy ``LLM_PROVIDER`` (which a mere ``os.environ.setdefault`` would
    not override).

    Preconditions:
        None.
    Postconditions:
        ``llm_service.config.resolve_provider`` is restored to its original
        value on exit, even if the wrapped block raises -- so importing or
        unit-testing this module never leaves the process-wide LLM provider
        resolution permanently patched for unrelated code (e.g. other tests
        in the same pytest session).
    """
    original = _llm_config.resolve_provider
    _llm_config.resolve_provider = lambda: "dummy"
    try:
        yield
    finally:
        _llm_config.resolve_provider = original


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
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "mission"


@dataclass
class PhasePromptComparison:
    mission_name: str
    phase: BrandPhase
    selective_tokens: int
    full_tokens: int

    @property
    def reduction_pct(self) -> float:
        if self.full_tokens == 0:
            return 0.0
        return 100.0 * (self.full_tokens - self.selective_tokens) / self.full_tokens


def compare_phase_prompts(
    mission: BrandingMission, phase: BrandPhase, prior_outputs: dict[str, dict]
) -> PhasePromptComparison:
    """Build the selective- and full-context task prompts for *phase* and token-count both.

    Preconditions:
        - ``phase`` is a runnable phase (a key of ``_PHASE_SPEC``).
        - ``prior_outputs`` maps upstream phase value strings to JSON-safe
          phase-output dicts.
    Postconditions:
        - Returns a comparison with both token counts computed.
        - ``_PHASE_SPEC`` is left unmodified on return, even if an exception
          is raised while building the full-context prompt.
    """
    selective_task = BrandingTeamOrchestrator._phase_task(  # noqa: SLF001
        mission, phase, prior_outputs
    )

    original_spec = _PHASE_SPEC[phase]
    try:
        _PHASE_SPEC[phase] = original_spec._replace(context_phases=_full_context_phases(phase))
        full_task = BrandingTeamOrchestrator._phase_task(mission, phase, prior_outputs)  # noqa: SLF001
    finally:
        _PHASE_SPEC[phase] = original_spec

    return PhasePromptComparison(
        mission_name=mission.company_name,
        phase=phase,
        selective_tokens=_approx_token_count(selective_task),
        full_tokens=_approx_token_count(full_task),
    )


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
        - Writes each mission's Phase 4 and Phase 5 outputs to
          ``output_dir/<mission-slug>.json``.
    """
    missions = list(missions) if missions is not None else SAMPLE_MISSIONS
    output_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = BrandingTeamOrchestrator()
    comparisons: list[PhasePromptComparison] = []

    with _force_dummy_llm_provider():
        for mission in missions:
            result = orchestrator.run(mission=mission, human_review=HumanReview(approved=True))

            prior_outputs: dict[str, dict] = {}
            phase_outputs = {
                BrandPhase.STRATEGIC_CORE: result.strategic_core,
                BrandPhase.NARRATIVE_MESSAGING: result.narrative_messaging,
                BrandPhase.VISUAL_IDENTITY: result.visual_identity,
                BrandPhase.CHANNEL_ACTIVATION: result.channel_activation,
                BrandPhase.GOVERNANCE: result.governance,
            }
            for phase in PHASE_ORDER:
                output = phase_outputs[phase]
                if output is None:
                    continue
                if phase != BrandPhase.STRATEGIC_CORE:
                    comparisons.append(compare_phase_prompts(mission, phase, dict(prior_outputs)))
                prior_outputs[phase.value] = output.model_dump(mode="json")

            eval_record = {
                "mission": mission.company_name,
                "channel_activation": (
                    result.channel_activation.model_dump(mode="json")
                    if result.channel_activation
                    else None
                ),
                "governance": (
                    result.governance.model_dump(mode="json") if result.governance else None
                ),
            }
            out_path = output_dir / f"{_slugify(mission.company_name)}.json"
            out_path.write_text(json.dumps(eval_record, indent=2, default=str))

    return comparisons


def _print_report(comparisons: list[PhasePromptComparison]) -> None:
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
