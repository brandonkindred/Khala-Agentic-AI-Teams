"""Shared pipeline state: ``PipelineContext``, its status/updater type aliases."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Union

if TYPE_CHECKING:
    from agents.blogging.blog_writer_agent.models import WriterOutput

from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.shared.content_plan import ContentPlan, PlanningPhaseResult
from agents.blogging.shared.content_profile import LengthPolicy, SeriesContext

PipelineStatus = Literal["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]

# Type alias for job updater callback
JobUpdater = Callable[..., None]


@dataclass
class PipelineContext:
    """Mutable state threaded across the blogging pipeline stages.

    Split out so each stage (planning -> draft -> gates) can run as its own Temporal
    activity: the activity seeds a context from the previous stage's serialized DTO,
    runs the stage, and serializes the produced fields. In thread mode a single
    context is threaded through all three stages in-process.

    Invariants:
        - ``llm_client`` and ``length_policy`` are resolved (non-None) before any
          stage runs.
        - ``planning_phase_result``/``plan``/``elicited_stories_text`` are populated
          by the planning stage before the draft stage reads them.
        - ``covered_sections`` is the de-duplicated set of plan section titles that
          already received an author narrative (a fresh interview or a story-bank
          hit). It is populated by the planning stage in thread mode: it is
          ``None`` before the planning stage runs and a ``set[str]`` (possibly
          empty) afterward. Consumption by the draft stage and threading it
          across the Temporal activity boundary are both follow-ups —
          ``PlanningStageResult`` doesn't carry it yet and neither
          ``draft_stage_activity`` nor ``gates_stage_activity`` re-seed it
          (unlike ``elicited_stories_text``, which both do) — so today nothing
          reads this field in either execution mode.
        - ``selected_title`` is populated by the planning stage, after outline
          approval (``_run_title_selection``, a no-op without a configured job
          store). The draft stage reads it and threads it into the writer/revision
          inputs; at ``None`` (no job store, or no title chosen) the writer picks
          its own title. The gates stage also still runs its own, independent
          selection round today, whose result feeds only
          ``PublishingPack.title_options`` — it does not touch the
          already-written draft. Unlike ``plan``/``elicited_stories_text``,
          ``selected_title`` does not yet cross the Temporal activity boundary:
          ``draft_stage_activity``/``gates_stage_activity`` re-seed the context
          without it, so a Temporal-mode run sees it stay at its ``None`` default
          regardless of what the planning stage selected — only thread mode
          carries the planning stage's choice through today.
        - ``draft_result`` is populated by the draft stage before the gates stage
          reads it.
    """

    brief: ResearchBriefInput
    work_dir: Optional[Union[str, Path]]
    # ``Any`` is deliberate: the LLM client is one of several unrelated concrete
    # types (a Strands model wrapper, a FailoverLLMClient, a DummyLLMClient) with no
    # shared base. ``Optional`` because it may be None at construction — __post_init__
    # rejects that, so every stage that runs sees a resolved client.
    llm_client: Any
    length_policy: Optional[LengthPolicy]
    series_context: Optional[SeriesContext]
    job_id: Optional[str]
    job_updater: Optional[JobUpdater]
    draft_editor_iterations: int
    max_rewrite_iterations: int
    run_gates: bool
    planning_phase_result: Optional[PlanningPhaseResult] = None
    plan: Optional[ContentPlan] = None
    elicited_stories_text: Optional[str] = None
    covered_sections: Optional[set[str]] = None
    selected_title: Optional[str] = None
    draft_result: Optional["WriterOutput"] = None
    status: PipelineStatus = "PASS"

    def __post_init__(self) -> None:
        # Enforce the resolved-inputs invariant at construction so the Temporal
        # activity path (which builds a context directly) fails loudly here rather
        # than with an opaque error deep inside a stage. Explicit raise (not assert)
        # so the check survives ``python -O``.
        if self.llm_client is None:
            raise ValueError("PipelineContext.llm_client must be resolved before running a stage")
        if self.length_policy is None:
            raise ValueError(
                "PipelineContext.length_policy must be resolved before running a stage"
            )
