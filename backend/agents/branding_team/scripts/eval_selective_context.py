"""Compare full-context vs selective-context pipeline prompts and outputs.

For each sample mission in ``branding_team.scripts.eval_fixtures.sample_missions``,
runs the branding pipeline once to obtain real per-phase outputs, then builds
each downstream phase's task prompt two ways:

- **selective**: using the real ``_PHASE_SPEC[phase].context_phases`` set by
  the selective-context filtering (see ``orchestrator._phase_task``).
- **full**: as if no filtering existed -- every upstream phase's output is
  included, matching pre-selective-context behavior.

Reports a token-count table for both variants plus the percentage reduction,
and saves each mission's Phase 4 (channel_activation) and Phase 5
(governance) outputs to a JSON file for manual quality comparison.

Also runs an LLM-as-judge pass (``branding_team.scripts.quality_judge``) that
scores both variants' Phase 4/5 outputs on strategic coherence, completeness,
and brand consistency (1-5 each), flags a regression when the selective
variant scores more than 0.5 points below the full-context variant on any
dimension, and writes both the token and quality results to a markdown
report (``<output-dir>/quality_report.md``).

Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=.:agents python3 -m branding_team.scripts.eval_selective_context

By default (no ``--live``), the whole run -- pipeline execution AND judge
scoring -- goes through the deterministic dummy stub client (see
``llm_service.dummy_provider.force_dummy_llm_provider``), regardless of any
live provider selected in the Postgres-backed runtime config or an inherited
``LLM_PROVIDER`` environment value; this makes the run fast, offline, and
reproducible, but ``DummyLLMClient`` replies from the requested schema, not
the prompt content, so both variants' output is identical and the judge can
never surface a real quality regression in this mode. Pass ``--live`` to run
both the pipeline and the judge against whatever LLM provider is actually
configured. This requires Postgres (``POSTGRES_HOST`` etc.) with at least one
provider entry in the ordered provider list (``/llm-config`` UI, or directly
in the ``llm_provider_configs`` table) -- ``llm_service.get_client`` treats
only ``LLM_PROVIDER=dummy`` as a hard override; any other ``LLM_PROVIDER``
value does *not* by itself select a live provider (there is no legacy
single-provider env fallback, see ``llm_service/factory.py``). Without a
configured provider list, ``--live`` raises ``LLMNotConfiguredError``. This is
the mode a genuine quality comparison requires. If a ``--live`` run flags a
regression, report it on the parent
story (Story: "Validate output quality with selective context via eval
comparison") for a ``context_phases`` adjustment before that story merges --
this script does not post to GitHub itself.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandingMission, BrandPhase
from branding_team.orchestrator import _PHASE_SPEC, BrandingTeamOrchestrator
from branding_team.scripts.eval_fixtures.sample_missions import SAMPLE_MISSIONS
from branding_team.scripts.quality_judge import PhaseQualityScore, score_phase_output
from llm_service import DummyLLMClient, get_client
from llm_service.dummy_provider import force_dummy_llm_provider

PHASE5_REDUCTION_TARGET_PCT = 40.0
QUALITY_REGRESSION_THRESHOLD_PTS = 0.5

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "eval_results"

# Phases scored by the LLM-as-judge pass -- the same two phases whose outputs
# are already saved to JSON for manual comparison.
_JUDGED_PHASES: tuple[BrandPhase, ...] = (BrandPhase.CHANNEL_ACTIVATION, BrandPhase.GOVERNANCE)


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


_QUALITY_DIMENSIONS: tuple[str, ...] = (
    "strategic_coherence",
    "completeness",
    "brand_consistency",
)


@dataclass
class PhaseQualityComparison:
    """LLM-as-judge quality scores for one phase's output under both context variants.

    Attributes:
        mission_name: The mission this comparison was computed for.
        phase: The branding phase whose output was judged.
        selective: The judge's score of the real, selective-context output.
        full: The judge's score of the full-context output.
    """

    mission_name: str
    phase: BrandPhase
    selective: PhaseQualityScore
    full: PhaseQualityScore

    def delta(self, dimension: str) -> int:
        """Return ``full``'s score minus ``selective``'s score for *dimension*.

        Preconditions:
            ``dimension`` is one of :data:`_QUALITY_DIMENSIONS`.
        Postconditions:
            Returns a positive value when the selective variant scored lower
            than the full-context variant on that dimension.
        """
        return getattr(self.full, dimension) - getattr(self.selective, dimension)

    def regressions(self) -> list[str]:
        """Return the dimension names where selective scores more than the threshold below full.

        Postconditions:
            Returns the subset of :data:`_QUALITY_DIMENSIONS` (in their
            declared order) whose ``delta`` strictly exceeds
            ``QUALITY_REGRESSION_THRESHOLD_PTS``; empty when no dimension
            regresses.
        """
        return [
            dim for dim in _QUALITY_DIMENSIONS if self.delta(dim) > QUALITY_REGRESSION_THRESHOLD_PTS
        ]


@contextlib.contextmanager
def _phase_spec_context_override(
    phase: BrandPhase, context_phases: tuple[BrandPhase, ...]
) -> Iterator[None]:
    """Temporarily replace ``_PHASE_SPEC[phase].context_phases``.

    Preconditions:
        ``phase`` is a key of ``_PHASE_SPEC``, and ``_PHASE_SPEC[phase]`` is a
        ``_PhaseSpec`` namedtuple (it always is, per its declared type in
        ``orchestrator.py``) -- this relies on ``._replace``, which only
        namedtuple-like records provide.
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


def _diverges_from_full_context(phase: BrandPhase) -> bool:
    """True when *phase*'s real, selective ``context_phases`` differs from its full-context prefix.

    Preconditions:
        ``phase`` is a member of ``PHASE_ORDER``.
    Postconditions:
        Returns ``False`` for every phase whose selective and full-context
        task prompts are built from the identical upstream phase set (today,
        every phase except ``GOVERNANCE``); ``True`` only where selective-context
        filtering actually removes an upstream phase from the prompt.
    """
    return _PHASE_SPEC[phase].context_phases != _full_context_phases(phase)


def _first_diverging_phase() -> Optional[BrandPhase]:
    """Return the first phase (in ``PHASE_ORDER``) whose context actually diverges.

    Postconditions:
        Returns the first non-``STRATEGIC_CORE`` phase for which
        ``_diverges_from_full_context`` is ``True``, or ``None`` if no phase
        diverges (every phase's selective and full-context prompts would be
        identical).
    """
    for phase in PHASE_ORDER:
        if phase == BrandPhase.STRATEGIC_CORE:
            continue
        if _diverges_from_full_context(phase):
            return phase
    return None


def _run_variant_pair(
    orchestrator: BrandingTeamOrchestrator, mission: BrandingMission
) -> tuple[
    dict[BrandPhase, object], dict[BrandPhase, str], dict[BrandPhase, object], dict[BrandPhase, str]
]:
    """Run the selective and full-context variants sharing every non-diverging phase.

    Running ``_run_variant(full_context=False)`` and ``_run_variant(full_context=True)``
    fully independently (as this eval originally did) means every phase gets
    regenerated twice from scratch -- including phases whose selective
    ``context_phases`` already equals their full-upstream prefix (today, every
    phase except ``GOVERNANCE``). Under a live, stochastic LLM provider that
    introduces two confounds this function eliminates: (1) a phase whose
    context is identical between variants would still be compared as two
    independent random samples of the same treatment, manufacturing a token
    or quality delta unrelated to selective-context filtering; (2) a
    downstream diverging phase's two variants would each build on separately,
    randomly regenerated upstream outputs, so their comparison would conflate
    "the effect of selective context" with "randomness between two unrelated
    generations." Sharing every phase up to the first divergence removes both:
    only the phase(s) whose context actually differs are ever run twice, and
    both forks start from the identical, single upstream generation.

    Preconditions:
        None beyond ``_run_variant``'s.
    Postconditions:
        Returns ``(selective_outputs, selective_tasks, full_outputs, full_tasks)``
        with the same per-phase shape ``_run_variant`` returns. For every
        phase before the first diverging phase (see ``_first_diverging_phase``),
        ``selective_outputs[phase] is full_outputs[phase]`` (the identical
        object, from one shared execution) and
        ``selective_tasks[phase] == full_tasks[phase]``. From the first
        diverging phase onward, each variant is executed independently against
        its own accumulated prior-outputs, exactly as ``_run_variant`` would.
        When no phase diverges, all four return values are pairwise identical
        to a single shared run's outputs/tasks. ``_PHASE_SPEC`` is left
        unmodified on return, even if a phase invocation raises.
    Raises:
        RuntimeError: propagated from a degraded phase output, exactly as
            ``_run_variant`` raises.
    """
    shared_outputs: dict[BrandPhase, object] = {}
    shared_tasks: dict[BrandPhase, str] = {}
    shared_prior: dict[str, dict] = {}

    fork_phase = _first_diverging_phase()
    fork_idx = PHASE_ORDER.index(fork_phase) if fork_phase is not None else len(PHASE_ORDER)

    for phase in PHASE_ORDER[:fork_idx]:
        with _phase_spec_context_override(phase, _PHASE_SPEC[phase].context_phases):
            shared_tasks[phase] = BrandingTeamOrchestrator._phase_task(  # noqa: SLF001
                mission, phase, dict(shared_prior)
            )
            output, degraded = orchestrator.run_single_phase(mission, phase, shared_prior)
        if degraded:
            raise RuntimeError(
                f"Phase {phase.value!r} degraded to a default-constructed output for "
                f"mission {mission.company_name!r} (shared prefix) -- aborting eval "
                "rather than reporting results derived from a placeholder output."
            )
        shared_outputs[phase] = output
        shared_prior[phase.value] = output.model_dump(mode="json")

    selective_outputs, selective_tasks = dict(shared_outputs), dict(shared_tasks)
    full_outputs, full_tasks = dict(shared_outputs), dict(shared_tasks)
    selective_prior, full_prior = dict(shared_prior), dict(shared_prior)

    for phase in PHASE_ORDER[fork_idx:]:
        for full_context, outputs, tasks, prior in (
            (False, selective_outputs, selective_tasks, selective_prior),
            (True, full_outputs, full_tasks, full_prior),
        ):
            context_phases = (
                _full_context_phases(phase) if full_context else _PHASE_SPEC[phase].context_phases
            )
            with _phase_spec_context_override(phase, context_phases):
                tasks[phase] = BrandingTeamOrchestrator._phase_task(  # noqa: SLF001
                    mission, phase, dict(prior)
                )
                output, degraded = orchestrator.run_single_phase(mission, phase, prior)
            if degraded:
                variant = "full-context" if full_context else "selective"
                raise RuntimeError(
                    f"Phase {phase.value!r} degraded to a default-constructed output for "
                    f"mission {mission.company_name!r} ({variant} variant) -- aborting eval "
                    "rather than reporting results derived from a placeholder output."
                )
            outputs[phase] = output
            prior[phase.value] = output.model_dump(mode="json")

    return selective_outputs, selective_tasks, full_outputs, full_tasks


def run_eval(
    missions: Optional[list[BrandingMission]] = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    live: bool = False,
) -> tuple[list[PhasePromptComparison], list[PhaseQualityComparison]]:
    """Run the full-vs-selective-context eval over *missions* and save Phase 4/5 outputs.

    Preconditions:
        ``missions`` is an iterable of ``BrandingMission`` instances, or
        ``None`` (defaults to ``SAMPLE_MISSIONS``).
    Postconditions:
        - Returns ``(comparisons, quality_comparisons)``: one
          ``PhasePromptComparison`` per (mission, phase) pair for every phase
          after ``STRATEGIC_CORE`` (which never filters), and one
          ``PhaseQualityComparison`` per (mission, phase) pair for each phase
          in ``_JUDGED_PHASES`` (channel_activation, governance).
        - Writes each mission's Phase 4 and Phase 5 outputs, under both the
          ``selective`` and ``full_context`` variants, to
          ``output_dir/<mission-slug>.json`` -- each produced by its own
          real, independent phase execution against genuinely different
          task prompts (not a shared prompt-string-only comparison). If two
          missions in ``missions`` share the same slugified ``company_name``,
          the second and later occurrences get a ``-2``, ``-3``, ... suffix
          rather than silently overwriting an earlier mission's file.
        - When ``live`` is ``False`` (the default): both pipeline execution
          and judge scoring go through the forced dummy client. Under the
          forced dummy client the two variants' output *content* will
          typically be identical regardless of prompt differences --
          ``DummyLLMClient`` replies from an agent's output schema, not its
          prompt text -- so this mode validates that the real
          selective-context code path runs (and measures its prompt-size
          effect) and that the judge pipeline itself works end to end, not
          real output quality; every ``PhaseQualityComparison.regressions()``
          is empty in this mode.
        - When ``live`` is ``True``: ``force_dummy_llm_provider`` is not
          entered, so both the pipeline and the judge (via
          ``llm_service.get_client``) run against whatever LLM provider is
          actually configured (the Postgres provider list, or
          ``LLM_PROVIDER`` when Postgres is unset) -- this is the mode a
          genuine quality comparison requires.
        - Every phase genuinely executes via ``_run_variant`` -> ``run_single_phase``
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

    comparisons: list[PhasePromptComparison] = []
    quality_comparisons: list[PhaseQualityComparison] = []
    slug_counts: dict[str, int] = {}

    llm_context = contextlib.nullcontext() if live else force_dummy_llm_provider()
    with llm_context:
        orchestrator = BrandingTeamOrchestrator()
        judge_client = get_client(agent_key="branding_quality_judge")
        if live and isinstance(judge_client, DummyLLMClient):
            raise RuntimeError(
                "run_eval(live=True) resolved to DummyLLMClient: LLM_PROVIDER=dummy is set "
                "in this environment and llm_service.get_client() treats it as a hard "
                "override that pre-empts any configured provider list, so a --live run "
                "would silently stay on the deterministic stub and could never report a "
                "real quality regression. Unset LLM_PROVIDER (or set it to something other "
                "than 'dummy') and configure a provider via /llm-config before running "
                "with --live."
            )
        for mission in missions:
            selective_outputs, selective_tasks, full_outputs, full_tasks = _run_variant_pair(
                orchestrator, mission
            )

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

            for phase in _JUDGED_PHASES:
                selective_score = score_phase_output(
                    judge_client,
                    mission=mission,
                    phase=phase,
                    output=selective_outputs[phase],
                    variant_label="selective",
                    strategic_core=selective_outputs[BrandPhase.STRATEGIC_CORE],
                )
                if selective_outputs[phase] is full_outputs[phase]:
                    # _run_variant_pair shares this phase's output object between
                    # variants (its context_phases doesn't diverge) -- judging it a
                    # second time would score identical content via a second real
                    # LLM call, which can itself return a different integer score
                    # and manufacture a false regression despite zero actual
                    # treatment difference. Reuse the one score already computed.
                    full_score = selective_score
                else:
                    full_score = score_phase_output(
                        judge_client,
                        mission=mission,
                        phase=phase,
                        output=full_outputs[phase],
                        variant_label="full",
                        strategic_core=full_outputs[BrandPhase.STRATEGIC_CORE],
                    )
                quality_comparisons.append(
                    PhaseQualityComparison(
                        mission_name=mission.company_name,
                        phase=phase,
                        selective=selective_score,
                        full=full_score,
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
            base_slug = _slugify(mission.company_name)
            slug_counts[base_slug] = slug_counts.get(base_slug, 0) + 1
            occurrence = slug_counts[base_slug]
            slug = base_slug if occurrence == 1 else f"{base_slug}-{occurrence}"
            out_path = output_dir / f"{slug}.json"
            out_path.write_text(json.dumps(eval_record, indent=2), encoding="utf-8")

    return comparisons, quality_comparisons


def _print_report(comparisons: list[PhasePromptComparison]) -> None:
    """Print the per-phase token-count table and the Phase 5 pass/fail verdict.

    Preconditions:
        ``comparisons`` is the list ``run_eval`` returns (may be empty, or
        contain zero ``GOVERNANCE`` entries if every mission passed had no
        governance phase -- not expected in normal use, but handled).
    Postconditions:
        Prints one table row per comparison, then either "No GOVERNANCE
        (Phase 5) comparisons collected." (if none exist) or the arithmetic
        mean of each GOVERNANCE comparison's ``reduction_pct`` (not the
        reduction between the missions' average token counts, which would
        weight missions differently) and a PASS/FAIL verdict against
        ``PHASE5_REDUCTION_TARGET_PCT``. Writes to stdout only; returns
        nothing.
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

    avg_reduction = sum(c.reduction_pct for c in governance) / len(governance)

    print("-" * 88)
    print(
        f"Phase 5 (governance) average reduction: {avg_reduction:.1f}% "
        f"(target: >= {PHASE5_REDUCTION_TARGET_PCT:.0f}%)"
    )
    verdict = "PASS" if avg_reduction >= PHASE5_REDUCTION_TARGET_PCT else "FAIL"
    print(
        f"Acceptance criterion (>= {PHASE5_REDUCTION_TARGET_PCT:.0f}% Phase 5 reduction): {verdict}"
    )


def _print_quality_report(quality_comparisons: list[PhaseQualityComparison]) -> None:
    """Print the per-phase LLM-as-judge quality-score table and the overall regression verdict.

    Preconditions:
        ``quality_comparisons`` is the list ``run_eval`` returns (may be empty).
    Postconditions:
        Prints one table row per comparison (selective/full score per
        dimension) and, when at least one comparison exists, an overall
        PASS/FAIL verdict -- FAIL when any comparison's ``regressions()`` is
        non-empty, naming every regressed (mission, phase, dimension). Writes
        to stdout only; returns nothing.
    """
    header = f"\n{'Mission':<32}{'Phase':<20}"
    for dim in _QUALITY_DIMENSIONS:
        header += f"{dim + ' (sel/full)':<28}"
    print(header)
    print("-" * (52 + 28 * len(_QUALITY_DIMENSIONS)))
    for c in quality_comparisons:
        row = f"{c.mission_name:<32}{c.phase.value:<20}"
        for dim in _QUALITY_DIMENSIONS:
            sel = getattr(c.selective, dim)
            full = getattr(c.full, dim)
            row += f"{f'{sel}/{full}':<28}"
        print(row)

    if not quality_comparisons:
        print("\nNo quality comparisons collected.")
        return

    all_regressions = [(c, dim) for c in quality_comparisons for dim in c.regressions()]
    print("-" * (52 + 28 * len(_QUALITY_DIMENSIONS)))
    if not all_regressions:
        print(
            "Quality regression check (selective within "
            f"{QUALITY_REGRESSION_THRESHOLD_PTS} pts of full-context on every dimension): PASS"
        )
        return

    print(
        "Quality regression check (selective within "
        f"{QUALITY_REGRESSION_THRESHOLD_PTS} pts of full-context on every dimension): FAIL"
    )
    for c, dim in all_regressions:
        print(
            f"  REGRESSION: {c.mission_name} / {c.phase.value} / {dim}: "
            f"selective={getattr(c.selective, dim)} full={getattr(c.full, dim)}"
        )


def _write_markdown_report(
    comparisons: list[PhasePromptComparison],
    quality_comparisons: list[PhaseQualityComparison],
    output_dir: Path,
) -> Path:
    """Write the combined token/quality results to ``output_dir/quality_report.md``.

    Preconditions:
        ``output_dir`` exists (``run_eval`` already creates it).
    Postconditions:
        Returns the written file's path. The file contains a token-reduction
        table, a quality-score table, and a regression verdict section
        listing every flagged (mission, phase, dimension), or a line stating
        no regressions were found.
    """
    lines = ["# Selective-Context Eval Report", ""]

    lines.append("## Token counts")
    lines.append("")
    lines.append("| Mission | Phase | Selective | Full | Reduction |")
    lines.append("|---|---|---:|---:|---:|")
    for c in comparisons:
        lines.append(
            f"| {c.mission_name} | {c.phase.value} | {c.selective_tokens} | "
            f"{c.full_tokens} | {c.reduction_pct:.1f}% |"
        )

    lines.append("")
    lines.append("## LLM-as-judge quality scores")
    lines.append("")
    dim_headers = " | ".join(f"{dim} (sel/full)" for dim in _QUALITY_DIMENSIONS)
    lines.append(f"| Mission | Phase | {dim_headers} |")
    lines.append("|---|---|" + "---|" * len(_QUALITY_DIMENSIONS))
    for c in quality_comparisons:
        scores = " | ".join(
            f"{getattr(c.selective, dim)}/{getattr(c.full, dim)}" for dim in _QUALITY_DIMENSIONS
        )
        lines.append(f"| {c.mission_name} | {c.phase.value} | {scores} |")

    lines.append("")
    lines.append("## Regression verdict")
    lines.append("")
    all_regressions = [(c, dim) for c in quality_comparisons for dim in c.regressions()]
    if not all_regressions:
        lines.append(
            "No regressions: every selective-context score is within "
            f"{QUALITY_REGRESSION_THRESHOLD_PTS} points of the full-context score "
            "on every dimension."
        )
    else:
        lines.append(
            "Regressions detected (selective more than "
            f"{QUALITY_REGRESSION_THRESHOLD_PTS} points below full-context):"
        )
        lines.append("")
        for c, dim in all_regressions:
            lines.append(
                f"- {c.mission_name} / {c.phase.value} / {dim}: "
                f"selective={getattr(c.selective, dim)} full={getattr(c.full, dim)}"
            )

    report_path = output_dir / "quality_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main(argv: Optional[list[str]] = None) -> int:
    """Run the eval script's CLI entry point.

    Parses ``--output-dir``, ``--mission``, and ``--live``, runs the eval
    over the selected sample missions, and prints the token and quality
    reports.

    Preconditions:
        ``argv`` is a list of CLI argument strings, or ``None`` to use
        ``sys.argv[1:]`` (``argparse``'s default).
    Postconditions:
        Returns ``0`` on success. Returns ``1`` without running the eval if
        ``--mission`` matches no sample mission (prints the reason to
        stderr). On success, writes one JSON file per selected mission plus
        ``quality_report.md`` to ``--output-dir`` (see ``run_eval`` and
        ``_write_markdown_report``), and prints the token and quality-score
        tables and verdicts to stdout (see ``_print_report`` and
        ``_print_quality_report``).
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
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Run the pipeline and the LLM-as-judge pass against the actually-configured "
            "LLM provider instead of the deterministic dummy stub. Required for the judge "
            "to surface a genuine quality difference between variants. Requires Postgres "
            "with a provider configured via /llm-config -- setting LLM_PROVIDER alone "
            "(to anything other than 'dummy') does not select a live provider."
        ),
    )
    args = parser.parse_args(argv)

    missions = SAMPLE_MISSIONS
    if args.mission:
        needle = args.mission.lower()
        missions = [m for m in SAMPLE_MISSIONS if needle in m.company_name.lower()]
        if not missions:
            print(f"No sample mission matches --mission={args.mission!r}", file=sys.stderr)
            return 1

    comparisons, quality_comparisons = run_eval(
        missions=missions, output_dir=args.output_dir, live=args.live
    )
    _print_report(comparisons)
    _print_quality_report(quality_comparisons)
    report_path = _write_markdown_report(comparisons, quality_comparisons, args.output_dir)
    print(f"\nMarkdown report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
