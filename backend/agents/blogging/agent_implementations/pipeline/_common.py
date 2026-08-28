"""Shared helpers used by more than one pipeline stage.

A handful of these functions (see each docstring's "Deferred import" note) look up
one of their own collaborators via a deferred import from the
``blog_writing_process_v2`` shim instead of a normal top-level import. That's not
decoration: ``agents.blogging.agent_implementations.blog_writing_process_v2`` is the
module the existing test suite monkeypatches (e.g.
``monkeypatch.setattr(blog_writing_process_v2, "get_blog_job", ...)``), and a Python
function resolves a bare global through the ``__dict__`` of the module it was
*defined* in — never through a re-export in some other module that merely imported a
reference to it. Binding the lookup at call time through the shim's own namespace is
what makes those patches keep taking effect now that this code lives outside the
monolith, mirroring ``agents.blogging.api.background``'s late ``_main`` imports (see
that module's docstring for the same rationale applied to the API layer's split).
"""

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Protocol, Tuple, Union

if TYPE_CHECKING:
    from agents.blogging.blog_writer_agent.models import WriterInput, WriterOutput
    from agents.blogging.ghost_writer_agent.models import StoryGap

from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent
from agents.blogging.blog_research_agent.agent_cache import AgentCache
from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.shared.artifacts import write_artifact
from agents.blogging.shared.content_plan import (
    ContentPlan,
    PlanningInput,
    PlanningPhaseResult,
    build_research_digest,
    content_plan_to_content_brief_markdown,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import (
    LengthPolicy,
    SeriesContext,
    build_planning_length_context,
    series_context_block,
)
from agents.blogging.shared.errors import BloggingError, DraftError, PlanningError, ResearchError
from agents.blogging.shared.models import BlogPhase, get_phase_progress
from agents.blogging.shared.planning_config import plan_critic_max_iterations
from agents.blogging.shared.run_pipeline_job import _is_external_cancellation
from strands.types.exceptions import EventLoopException
from temporalio.exceptions import CancelledError

from llm_service import LLMClientModel, with_model_override
from llm_service.interface import LLMClient, LLMRateLimitError, LLMTemporaryError

from .constants import (
    BRAND_SPEC_PROMPT_PATH,
    HITL_MAX_CONSECUTIVE_READ_ERRORS,
    HITL_POLL_INTERVAL_S,
    STYLE_GUIDE_PATH,
)
from .context import JobUpdater

logger = logging.getLogger(__name__)

_DEFAULT_PLANNING_CONTEXT_TOKENS = 16_384
_PLANNING_NON_RESEARCH_RESERVE_TOKENS = 6_000


def _planning_research_digest_max_chars(llm: Any) -> int:
    """Return a conservative digest budget for the selected planning model.

    Research text is budgeted at one character per token, matching the research
    agent's safety rule for poorly tokenizing web content.  The remaining context
    is reserved for the planning instructions, brief, prior plan/feedback, and
    model output.
    """
    try:
        context_tokens = int(llm.get_max_context_tokens())
    except (AttributeError, TypeError, ValueError):
        context_tokens = _DEFAULT_PLANNING_CONTEXT_TOKENS
    except Exception:
        logger.warning(
            "Could not resolve planning context size; using %d-token fallback",
            _DEFAULT_PLANNING_CONTEXT_TOKENS,
            exc_info=True,
        )
        context_tokens = _DEFAULT_PLANNING_CONTEXT_TOKENS
    return max(1, context_tokens - _PLANNING_NON_RESEARCH_RESERVE_TOKENS)


def _unwrap_llm_cause(exc: BaseException) -> BaseException:
    """Return the underlying model error when strands wraps it in EventLoopException.

    Same helper as ``blog_writer_agent``'s (repeated locally rather than shared, per
    that module's convention — see its own copy's docstring).

    Preconditions:
        - ``exc`` is the exception caught at an LLM call boundary.
    Postconditions:
        - If ``exc`` is an ``EventLoopException`` with a non-None ``original_exception``,
          returns that original exception.
        - Otherwise returns ``exc`` unchanged.
    """
    if isinstance(exc, EventLoopException):
        original = getattr(exc, "original_exception", None)
        if isinstance(original, BaseException):
            return original
    return exc


def _wait_for_hitl(
    job_id: str,
    is_waiting: Callable[[str], bool],
    *,
    on_poll: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Block until a human-in-the-loop wait clears or the job goes terminal.

    Single home for the pipeline's HITL poll loops (title selection, outline/draft
    feedback, uncertainty answers): the poll cadence (``HITL_POLL_INTERVAL_S``), the
    terminal-status check, and the blocking sleep live here instead of being copied
    at every wait site.

    Args:
        job_id: The job being waited on.
        is_waiting: Predicate ``(job_id) -> bool`` — True while a human response is
            still outstanding.
        on_poll: Optional ``(job_id) -> bool`` invoked once per iteration before
            sleeping. Return True to re-poll immediately without sleeping (e.g. after
            handling incremental feedback); a falsy return sleeps.

    Preconditions:
        - ``is_waiting`` (and ``on_poll`` when provided) are callables accepting a
          ``job_id`` string.
    Postconditions:
        - Returns True iff the job reached a terminal state while waiting — either a
          "failed"/"cancelled" status, or the job disappeared from the store
          (``get_blog_job`` is None). The caller aborts with its own FAIL result.
        - Returns False once ``is_waiting`` became False without a terminal state
          (a human responded) — the caller reads the response.
        - Any exception raised by ``is_waiting`` / ``get_blog_job`` (not just
          network/HTTP blips — also unexpected programming errors) is ridden out:
          logged and retried on the next poll, up to
          ``HITL_MAX_CONSECUTIVE_READ_ERRORS`` consecutive failures, after which the
          error propagates (a persistent outage still fails the job).
          ``CancelledError`` is re-raised immediately so Temporal cancellation is
          not delayed by the retry budget. ``on_poll`` errors are not caught — they
          propagate immediately.
        - Does not mutate job state; ``on_poll`` may.
    """
    # Deferred import: see module docstring — keeps monkeypatch.setattr(shim,
    # "get_blog_job", ...) effective now that this function lives outside the shim.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import get_blog_job

    consecutive_read_errors = 0
    while True:
        # Wrap only the job-store reads: a transient blip during a long HITL wait should
        # retry next poll, not fail the whole job. on_poll (below) stays outside so its
        # errors surface immediately. CancelledError must escape immediately so Temporal
        # cancellation is not delayed by the consecutive-error budget.
        try:
            if not is_waiting(job_id):
                return False
            job_data = get_blog_job(job_id)
        except CancelledError:
            raise
        except Exception as e:
            consecutive_read_errors += 1
            if consecutive_read_errors >= HITL_MAX_CONSECUTIVE_READ_ERRORS:
                logger.warning(
                    "HITL wait for job %s: %d consecutive job-store read failures; giving up",
                    job_id,
                    consecutive_read_errors,
                )
                raise
            logger.warning(
                "HITL wait for job %s: transient job-store read failure (%d/%d), retrying: %s",
                job_id,
                consecutive_read_errors,
                HITL_MAX_CONSECUTIVE_READ_ERRORS,
                e,
            )
            time.sleep(HITL_POLL_INTERVAL_S)
            continue
        consecutive_read_errors = 0
        if job_data is None:
            # The job was deleted from the store mid-wait. ``get_blog_job`` only
            # returns None for a genuinely-absent job (transient/HTTP errors raise),
            # so treat it as terminal and stop polling a job that no longer exists.
            logger.warning("Job %s not found during HITL wait — treating as terminal", job_id)
            return True
        if job_data.get("status") in ("failed", "cancelled"):
            return True
        if on_poll is not None and on_poll(job_id):
            continue
        time.sleep(HITL_POLL_INTERVAL_S)


def _apply_stage_model_override(base: LLMClient, model: Optional[str]) -> LLMClient:
    """Return a variant of ``base`` pinning Ollama fallback candidates to ``model``.

    ``base`` may be a Strands :class:`LLMClientModel` (what the pipeline actually
    passes — ``get_strands_model`` wraps the failover client) or a raw failover
    client. In both cases the override reaches the backing :class:`FailoverLLMClient`
    via :func:`with_model_override`, so an Ollama candidate uses ``model`` while a
    non-Ollama candidate keeps its configured model — multi-provider failover is
    preserved. A backing with no failover client (e.g. a ``DummyLLMClient``) or a
    falsy ``model`` returns ``base`` unchanged.

    Preconditions: ``model`` is a non-empty model name or falsy. Postconditions:
        returns a client ready to use; ``base`` is never mutated (a Strands model is
        rebuilt over the pinned backing, preserving its response format and config).
    """
    if not model:
        return base
    if isinstance(base, LLMClientModel):
        pinned_backing = with_model_override(base.client, model)
        if pinned_backing is base.client:
            # No failover client underneath (e.g. Dummy) — nothing to pin.
            return base
        return LLMClientModel(pinned_backing, **base.get_config())
    return with_model_override(base, model)


def planning_llm_client(base: LLMClient) -> LLMClient:
    """Return the LLM client to use for blog planning.

    When ``BLOG_PLANNING_MODEL`` is set, returns a variant of ``base`` whose Ollama
    fallback candidates are pinned to that model; otherwise returns ``base`` unchanged.
    The override is applied per call (via :func:`_apply_stage_model_override` →
    :func:`with_model_override`), so multi-provider failover is preserved — an Ollama
    provider uses the planning model while a non-Ollama fallback keeps its configured
    model — and ``base``'s agent attribution and reasoning hook carry across. Works
    whether ``base`` is a raw failover client or the Strands model the pipeline passes.

    :param base: The default client the blog pipeline would otherwise use.
    :returns: ``base``, or a failover-preserving variant pinning Ollama candidates to
        ``BLOG_PLANNING_MODEL``.
    """
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        planning_model_override,
    )

    return _apply_stage_model_override(base, planning_model_override())


def plan_critic_llm_client(base: LLMClient) -> LLMClient:
    """Return the LLM client to use for the plan critic.

    When ``BLOG_PLAN_CRITIC_MODEL`` is set, returns a variant of ``base`` whose Ollama
    fallback candidates are pinned to that model (via :func:`with_model_override`, so
    multi-provider failover is preserved); otherwise returns ``base`` unchanged. The
    override preserves ``base``'s agent attribution (see :func:`planning_llm_client`).

    Per the architectural tenet, the critic runs on the same model as the writer
    by default. This hook exists so per-role model diversification can be flipped
    on later without further code changes.

    :param base: The default client the blog pipeline would otherwise use.
    :returns: ``base``, or a failover-preserving variant pinning Ollama candidates to
        ``BLOG_PLAN_CRITIC_MODEL``.
    """
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        plan_critic_model_override,
    )

    return _apply_stage_model_override(base, plan_critic_model_override())


def build_plan_critic_agent(base: LLMClient) -> Optional[BlogPlanCriticAgent]:
    """Construct the plan-critic agent when enabled, else return None."""
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        plan_critic_enabled,
    )
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        plan_critic_llm_client as _plan_critic_llm_client,
    )

    if not plan_critic_enabled():
        return None
    return BlogPlanCriticAgent(llm_client=_plan_critic_llm_client(base))


def _persist_content_plan_artifacts(
    work_dir: Union[str, Path],
    plan: ContentPlan,
    *,
    llm_client: Any,
    topic: str,
    content_plan_markdown: Optional[str] = None,
) -> str:
    """Persist content-plan artifacts and a freshly extracted allowed_claims.json.

    Single source for the artifact set that must change together whenever the
    content plan changes: ``run_planning``'s initial persist and
    ``run_planning_stage``'s outline-approval re-plan loop both call this
    instead of writing the artifacts separately, so ``allowed_claims.json``
    can never drift out of sync with the plan text it was derived from.

    ``run_planning`` runs a research stage (``ResearchAgent``) ahead of this
    helper, but its compiled document/reference list are not yet threaded
    through here (a separate follow-up); ``extract_allowed_claims`` is called
    against the plan's own markdown with an empty reference list.

    Args:
        work_dir: Directory to persist artifacts to.
        plan: The (possibly revised) content plan to persist.
        llm_client: Resolved LLM client used for claims extraction. May be a raw
            ``LLMClient`` or a Strands ``LLMClientModel`` wrapper (what the
            pipeline actually passes in production, e.g. via
            ``get_strands_model``) — either is accepted.
        topic: Topic/brief text recorded on the persisted ``AllowedClaims``.
        content_plan_markdown: Pre-computed ``content_plan_to_markdown_doc(plan)``
            text, when a caller already needed it for something else (e.g. a
            progress update fired before this call) and wants to avoid
            recomputing it here. Computed from ``plan`` when omitted.

    Preconditions:
        - ``work_dir`` is not None. ``plan`` is a valid ``ContentPlan``.
          ``llm_client`` is resolved (non-None). ``content_plan_markdown``,
          when given, is ``content_plan_to_markdown_doc(plan)`` for this same
          ``plan``.
    Postconditions:
        - Writes ``content_plan.json``, ``content_plan.md``, ``outline.md``,
          ``content_brief.md``, and ``allowed_claims.json`` to ``work_dir``.
        - ``allowed_claims.json`` is always written, even with zero claims,
          since ``extract_allowed_claims`` never raises.
        - Returns the ``content_plan.md`` markdown text used as
          ``extract_allowed_claims``'s ``compiled_document``, so callers that
          already need it (e.g. for progress updates) can reuse it.
    """
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        extract_allowed_claims,
    )

    if content_plan_markdown is None:
        content_plan_markdown = content_plan_to_markdown_doc(plan)
    write_artifact(work_dir, "content_plan.json", plan.model_dump(mode="json"))
    write_artifact(work_dir, "content_plan.md", content_plan_markdown)
    write_artifact(work_dir, "outline.md", content_plan_to_outline_markdown(plan))
    write_artifact(work_dir, "content_brief.md", content_plan_to_content_brief_markdown(plan))
    logger.info("Persisted content_plan.json, content_plan.md, outline.md, content_brief.md")

    # extract_allowed_claims calls complete_json() directly, which only the backing
    # LLMClient exposes — a Strands LLMClientModel wrapper (what get_strands_model
    # returns, and what the pipeline actually passes in production) does not, so it
    # must be unwrapped first (same pattern as _apply_stage_model_override above).
    claims_llm_client = llm_client.client if isinstance(llm_client, LLMClientModel) else llm_client
    allowed_claims = extract_allowed_claims(
        claims_llm_client, content_plan_markdown, references=[], topic=topic
    )
    write_artifact(work_dir, "allowed_claims.json", allowed_claims.to_dict())
    logger.info("Persisted allowed_claims.json (%d claim(s))", len(allowed_claims.claims))
    return content_plan_markdown


def run_planning(
    brief: ResearchBriefInput,
    *,
    work_dir: Optional[Union[str, Path]],
    llm_client: Any,
    length_policy: LengthPolicy,
    series_context: Optional[SeriesContext],
    job_updater: Optional[JobUpdater],
    on_research_digest: Optional[Callable[[str], None]] = None,
) -> PlanningPhaseResult:
    """
    Planning step for the full pipeline: build the content plan for ``brief``.

    Args:
        brief: The research brief describing the blog topic.
        work_dir: Optional directory for artifact persistence (planning artifacts
            are written when set).
        llm_client: Resolved LLM client used for planning.
        length_policy: Resolved length/format policy for the plan.
        series_context: Optional series-instalment scope.
        job_updater: Optional UI progress callback.
        on_research_digest: Optional internal callback that receives the bounded
            digest so callers performing later re-planning can reuse it.

    Preconditions:
        - ``brief`` is a valid ``ResearchBriefInput``.
        - ``llm_client`` and ``length_policy`` are resolved (non-None).
    Postconditions:
        - Runs ``ResearchAgent.run(brief)`` and, when ``work_dir`` is set, persists
          its compiled document as ``research_packet.md`` under ``work_dir`` (before
          the planning artifacts below); reports a "research" phase progress message
          via ``job_updater`` before and after the research call.
        - Passes a bounded digest of the compiled research document into planning;
          a research run with no web references or academic papers supplies an
          empty digest.
        - Returns a ``PlanningPhaseResult`` (content plan with title candidates,
          sections, requirements analysis, and planning telemetry).
        - When ``work_dir`` is given, ``allowed_claims.json`` is always written
          (in addition to the other planning artifacts) since
          ``extract_allowed_claims`` never raises — a run with no verifiable
          claims yields a valid ``allowed_claims.json`` with an empty ``claims``
          list rather than omitting the artifact.
    Raises:
        ResearchError: If research fails for a non-transient reason (phase="research",
            so Temporal/thread-mode failure tracking attributes the failure correctly
            instead of defaulting to "planning").
        PlanningError: If content planning fails for a non-transient reason.
        BloggingError: Blogging-domain errors from the research agent, planner, or
            plan critic propagate unwrapped (not re-wrapped).
        LLMRateLimitError / LLMTemporaryError: transient LLM-transport failures
            (including from research and the plan critic) propagate unwrapped so
            Temporal's activity funnel can retry the stage instead of treating them
            as a terminal error.
    """
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        BlogWriterAgent,
        ResearchAgent,
        load_brand_spec_prompt,
        load_style_file,
    )
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        build_plan_critic_agent as _build_plan_critic_agent,
    )

    # Same progress-callback as the stage functions use; _make_update is the single
    # source of the swallow-but-reraise-CancelledError update logic.
    _update = _make_update(job_updater)

    def _report_research(status_text: str, **extra: Any) -> None:
        # Reported on its own "research" phase (not a BlogPhase member, same
        # convention as the "story_elicitation"/"title_selection" raw job_updater
        # calls elsewhere in this module) since it doesn't have its own slice of the
        # overall progress bar. Guarded the same way _make_update guards phase
        # updates, so a failing job_updater can't abort research/planning.
        if not job_updater:
            return
        try:
            job_updater(phase="research", progress=0, status_text=status_text, **extra)
        except CancelledError:
            raise
        except Exception as e:
            logger.warning("Failed to update job status: %s", e)

    # Research: run before planning so a compiled research document is available
    # as an artifact. Checkpointed under work_dir (when set) so a Temporal retry of
    # this activity after a transient planning failure resumes research from its
    # last completed step instead of repeating every search/fetch/summarization call.
    # Cache setup and the artifact write are wrapped alongside the agent call (not
    # around/after it) so a failure anywhere in this block — cache dir creation, the
    # agent run, or the artifact write — is attributed to phase="research" too,
    # instead of surfacing unattributed or misattributed to planning.
    _report_research("Researching topic...")

    try:
        research_cache = (
            AgentCache(cache_dir=Path(work_dir) / ".research_cache")
            if work_dir is not None
            else None
        )
        research_agent = ResearchAgent(llm_client=llm_client, cache=research_cache)
        research_output = research_agent.run(brief)
        if work_dir is not None:
            write_artifact(work_dir, "research_packet.md", research_output.compiled_document or "")
            logger.info(
                "Persisted research_packet.md (%d reference(s))",
                len(research_output.references),
            )
    except (BloggingError, LLMRateLimitError, LLMTemporaryError):
        raise
    except Exception as e:
        cause = _unwrap_llm_cause(e)
        if isinstance(cause, (BloggingError, LLMRateLimitError, LLMTemporaryError)):
            # ResearchAgent's live Strands call (unlike the planner's, which goes
            # through run_json_gate/call_json_with_retry) doesn't unwrap
            # EventLoopException itself, so a transient LLM error can still reach
            # here wrapped. Re-raise the unwrapped cause so Temporal recognizes it
            # as transient instead of failing the job on a terminal ResearchError.
            raise cause
        if _is_external_cancellation(cause):
            # Mirrors the unwrap above: a Temporal cancellation raised inside the
            # Strands event loop reaches here as EventLoopException(CancelledError),
            # so _is_external_cancellation must see the unwrapped cause (its
            # __cause__/__context__ walk can't reach into original_exception) — and
            # must re-raise that cause, not the EventLoopException wrapper, so the
            # job is recorded as cancelled rather than as a research failure.
            raise cause
        raise ResearchError(f"Research failed: {e}", cause=e) from e

    _report_research(
        f"Research complete ({len(research_output.references)} reference(s))",
        research_sources_count=len(research_output.references),
    )

    _update(
        BlogPhase.PLANNING,
        sub_progress=0.0,
        status_text="Generating content plan...",
    )

    planning_client = planning_llm_client(llm_client)

    # The research agent emits a human-readable fallback document even when no web
    # references or academic papers were found.  That artifact remains useful for
    # diagnostics, but it is not evidence for the planner, so keep the planning
    # digest empty in that case.  Supplying the resolved client lets
    # build_research_digest compact documents that exceed its prompt budget instead
    # of passing them through.
    digest_llm = (
        planning_client.client if isinstance(planning_client, LLMClientModel) else planning_client
    )
    digest_max_chars = _planning_research_digest_max_chars(planning_client)
    research_digest = build_research_digest(
        research_output.compiled_document
        if research_output.references or research_output.academic_papers
        else "",
        max_chars=digest_max_chars,
        llm=digest_llm,
    )
    if on_research_digest is not None:
        on_research_digest(research_digest)

    planning_input = PlanningInput(
        brief=brief.brief,
        audience=brief.audience,
        tone_or_purpose=brief.tone_or_purpose,
        research_digest=research_digest,
        length_policy_context=build_planning_length_context(length_policy),
        series_context_block=series_context_block(series_context),
    )

    # Load the author's brand spec + writing guidelines so the plan critic can
    # evaluate against the author-owned sources of truth, and so the planning
    # BlogWriterAgent receives the same brand/style context (even when the
    # critic is disabled).
    try:
        brand_spec_for_critic = load_brand_spec_prompt(BRAND_SPEC_PROMPT_PATH)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not load brand spec for plan critic: %s", e)
        brand_spec_for_critic = ""
    try:
        writing_guidelines_for_critic = load_style_file(STYLE_GUIDE_PATH)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not load writing guidelines for plan critic: %s", e)
        writing_guidelines_for_critic = ""

    plan_critic = _build_plan_critic_agent(llm_client)

    # Planning convergence cap: honour the critic's max iterations when the
    # critic is enabled, since the critic can reject plans the planner would
    # otherwise accept. Fall back to the planner's own iteration cap otherwise.
    planning_max_iter = plan_critic_max_iterations() if plan_critic is not None else 5

    try:
        planning_draft_agent = BlogWriterAgent(
            llm_client=planning_client,
            writing_style_guide_content=writing_guidelines_for_critic,
            brand_spec_content=brand_spec_for_critic,
        )
        planning_phase_result = planning_draft_agent.plan_content(
            planning_input,
            length_policy=length_policy,
            on_llm_request=lambda msg: _update(BlogPhase.PLANNING, status_text=msg),
            plan_critic=plan_critic,
            work_dir=work_dir,
            max_iterations=planning_max_iter,
        )
    except (BloggingError, LLMRateLimitError, LLMTemporaryError):
        # Domain errors and transient LLM-transport errors must stay unwrapped
        # so temporal.activities._run_stage (and callers) classify them correctly.
        raise
    except Exception as e:
        if _is_external_cancellation(e):
            raise
        raise PlanningError(f"Planning failed: {e}", cause=e) from e

    plan = planning_phase_result.content_plan
    plan_brief_md = content_plan_to_content_brief_markdown(plan)
    logger.info(
        "Planning complete: %s iteration(s), %s title candidates\n%s",
        planning_phase_result.planning_iterations_used,
        len(plan.title_candidates),
        plan_brief_md,
    )
    content_plan_markdown = content_plan_to_markdown_doc(plan)
    _update(
        BlogPhase.PLANNING,
        sub_progress=1.0,
        status_text=(
            f"Planning complete ({planning_phase_result.planning_iterations_used} iteration(s), "
            f"{len(plan.title_candidates)} titles)"
        ),
        planning_iterations_used=planning_phase_result.planning_iterations_used,
        parse_retry_count=planning_phase_result.parse_retry_count,
        planning_wall_ms_total=planning_phase_result.planning_wall_ms_total,
        content_plan_detail=content_plan_markdown,
    )

    if work_dir is not None:
        # Reuse the markdown already computed above for the progress update, rather
        # than have _persist_content_plan_artifacts recompute the identical text.
        _persist_content_plan_artifacts(
            work_dir,
            plan,
            llm_client=llm_client,
            topic=brief.brief,
            content_plan_markdown=content_plan_markdown,
        )
        # Persist the critic's final verdict under a stable filename for easy inspection;
        # per-iteration reports (plan_critic_report_v{N}.json) remain in work_dir too.
        if planning_phase_result.plan_critic_report is not None:
            write_artifact(
                work_dir,
                "plan_critic_report.json",
                planning_phase_result.plan_critic_report,
            )
            logger.info(
                "Persisted plan_critic_report.json (status=%s)",
                planning_phase_result.plan_critic_report.get("status"),
            )

    return planning_phase_result


# Common English stopwords to drop when extracting plan keywords. Length-based
# filtering alone drops meaningful short acronyms (e.g. "API", "SQL", "UX", "AI")
# while letting long stopwords (e.g. "with", "your", "about") through, so we
# filter by membership in this list instead.
_PLAN_KEYWORD_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "for",
        "so",
        "yet",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "as",
        "is",
        "it",
        "be",
        "are",
        "was",
        "were",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "with",
        "from",
        "into",
        "onto",
        "about",
        "over",
        "under",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "again",
        "further",
        "then",
        "than",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "not",
        "too",
        "very",
        "just",
        "also",
        "this",
        "that",
        "these",
        "those",
        "your",
        "you",
        "our",
        "their",
        "its",
        "his",
        "her",
        "which",
        "who",
        "whom",
        "using",
        "use",
        "used",
        "i",
        "we",
        "us",
        "my",
        "me",
        "he",
        "she",
        "him",
        "they",
        "them",
        "if",
        "no",
        "now",
        "what",
        "while",
        "because",
        "without",
        "until",
        "against",
        "although",
        "though",
        "unless",
        "despite",
        "since",
        "whether",
        "toward",
        "towards",
        "within",
        "upon",
        "across",
        "among",
        "amongst",
        "beyond",
        "regarding",
        "concerning",
    }
)


# Short (< 4 char) technical/domain terms admitted regardless of the general
# length floor below. This is an explicit, bounded allowlist rather than a
# casing-based heuristic ("all uppercase => acronym") on purpose: LLM-generated
# plan text doesn't reliably capitalize real acronyms ("api" or "Api" are as
# likely as "API"), and conversely an all-caps heading doesn't mean every word
# in it is an acronym (a heading like "HOW TO USE AI" is not three acronyms
# and a stopword) -- casing is wrong as a signal in both directions. This list
# is necessarily incomplete (there's no bounded, casing-independent way to
# recognize *every* short technical term without reintroducing the false
# positives above); it covers common terms and can grow as real gaps surface.
_PLAN_KEYWORD_SHORT_TERMS = frozenset(
    {
        "ai",
        "ml",
        "ux",
        "ui",
        "os",
        "io",
        "db",
        "ip",
        "vr",
        "ar",
        "api",
        "sql",
        "css",
        "xml",
        "url",
        "uri",
        "aws",
        "gcp",
        "ci",
        "cd",
        "qa",
        "cli",
        "sdk",
        "llm",
        "nlp",
        "seo",
        "roi",
        "kpi",
        "crm",
        "erp",
        "iot",
        "b2b",
        "b2c",
        "saas",
        "gpu",
        "cpu",
        "dns",
        "ssh",
        "html",
        "http",
        "https",
        "json",
    }
)


def _strip_non_alnum_edges(word: str) -> str:
    """Trim any non-alphanumeric characters from both ends of *word*.

    Unlike ``str.strip()`` against a fixed character set, this handles
    arbitrary wrapper punctuation LLM output commonly produces -- smart
    quotes ("about"), markdown emphasis (**about**), em/en dashes
    (about--) -- without needing to enumerate every such character.
    Internal punctuation (e.g. the hyphen in "ai-driven") is untouched.
    """
    start, end = 0, len(word)
    while start < end and not word[start].isalnum():
        start += 1
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return word[start:end]


def _extract_plan_keywords(plan: Any) -> list[str]:
    """Extract searchable keywords from a content plan for story bank queries.

    Combines the overarching topic and section titles, lowercases, and
    splits on whitespace. A token is admitted as a keyword if either:

    - it's in ``_PLAN_KEYWORD_SHORT_TERMS``, a bounded allowlist of short
      technical/domain terms (e.g. "api", "sql", "ux") that would otherwise
      be dropped by the length floor below; or
    - it is at least 4 characters and not in ``_PLAN_KEYWORD_STOPWORDS``
      (the original length heuristic, still needed to drop long stopwords
      like "with"/"your"/"about" and ordinary short words like "new" that
      would otherwise cause spurious keyword-overlap matches in the story
      bank).

    Tokens are trimmed of surrounding punctuation via
    ``_strip_non_alnum_edges``; tokens with no alphanumeric content at all
    (e.g. "--", "##") reduce to an empty string and are dropped.
    """
    parts: list[str] = []
    topic = getattr(plan, "overarching_topic", "") or ""
    parts.extend(topic.lower().split())
    for section in getattr(plan, "sections", []) or []:
        title = getattr(section, "title", "") or ""
        parts.extend(title.lower().split())
    seen: set[str] = set()
    keywords: list[str] = []
    for word in parts:
        cleaned = _strip_non_alnum_edges(word)
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        admitted = cleaned in _PLAN_KEYWORD_SHORT_TERMS or (
            len(cleaned) >= 4 and cleaned not in _PLAN_KEYWORD_STOPWORDS
        )
        if admitted:
            seen.add(cleaned)
            keywords.append(cleaned)
    return keywords


# Regex matching [Author: ...] placeholders in draft output.
_PLACEHOLDER_RE = re.compile(
    r"\[Author:\s*(?:add\s+)?(.+?)\]",
    re.IGNORECASE,
)


def _extract_story_placeholders(draft_text: str) -> List[Tuple[str, str]]:
    """Return (full_match, topic_description) pairs for each ``[Author: ...]`` placeholder."""
    results = []
    for m in _PLACEHOLDER_RE.finditer(draft_text):
        results.append((m.group(0), m.group(1).strip()))
    return results


class _DraftAgent(Protocol):
    """Minimal draft-agent surface used by post-draft story placeholder fill.

    Preconditions:
        - Implementers provide a callable ``run`` matching this signature.
    Postconditions:
        - Structural typing only; no runtime registration.
    """

    def run(
        self,
        draft_input: "WriterInput",
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> "WriterOutput": ...


def _fill_story_placeholders(
    *,
    draft_text: str,
    plan: ContentPlan,
    llm_client: Any,
    job_id: str,
    job_updater: JobUpdater,
    elicited_stories_text: Optional[str],
    draft_agent: _DraftAgent,
    draft_input_kwargs: dict[str, Any],
    work_dir: Optional[Union[str, Path]],
    iteration: int,
) -> Tuple["WriterOutput", Optional[str]]:
    """Scan draft for ``[Author: ...]`` placeholders and interview the user for each.

    For each placeholder the ghost writer conducts an interview.  If the user
    indicates they have no relevant experience the placeholder is removed and
    the section is rewritten without a personal story.  Otherwise the collected
    narrative replaces the placeholder.

    ``llm_client`` is typed as ``Any`` deliberately (same as ``PipelineContext``):
    production passes a Strands ``LLMClientModel`` / model wrapper that does not
    subclass ``llm_service.interface.LLMClient``, while tests and failover paths
    may pass other client shapes. The runtime contract is non-None only.

    Args:
        draft_text: Draft content that may contain ``[Author: ...]`` placeholders.
        plan: Blog plan object used for keyword extraction.
        llm_client: LLM client passed to sub-agents.
        job_id: Identifier of the active blog job.
        job_updater: Callable that publishes phase, progress, and status text.
        elicited_stories_text: Existing collected stories, if any.
        draft_agent: Agent used to re-draft after stories are collected.
        draft_input_kwargs: Base kwargs for ``WriterInput``; must not include
            ``elicited_stories``.
        work_dir: Optional directory for draft artifacts. If ``None``, no draft
            artifact is persisted.
        iteration: Current draft iteration number.

    Preconditions:
        - ``draft_text`` is a ``str``.
        - ``plan`` is a ``ContentPlan`` instance.
        - ``llm_client`` is not ``None``.
        - ``draft_agent`` provides a callable ``run`` method.
        - ``draft_input_kwargs`` does not already contain ``elicited_stories``
          (this function injects that key when building ``WriterInput``).
    Postconditions:
        - Returns ``(updated_draft_result, updated_elicited_stories_text)``.
        - When no placeholders exist, returns a ``WriterOutput`` wrapping the
          original ``draft_text`` and the unchanged ``elicited_stories_text``.
        - If the post-story re-draft call raises a non-cancellation
          exception, the original ``draft_text`` — including any unfilled
          ``[Author: ...]`` placeholders — is returned unchanged alongside
          the updated ``elicited_stories_text``; the failure is logged but
          not raised.

    Raises:
        TypeError: a precondition on ``draft_text``, ``plan``, ``llm_client``,
            or ``draft_agent`` is violated.
        ValueError: ``draft_input_kwargs`` already contains ``elicited_stories``.
        CancelledError: a Temporal-native (or otherwise external) cancellation
            propagates unchanged from both the non-fatal story-bank-save
            guard below and the non-fatal re-draft guard — neither ever
            swallows it.
    """
    # Local imports so GhostWriterElicitationAgent is resolved from
    # agents.blogging.ghost_writer_agent at call time — tests monkeypatch that
    # module attribute. Hoisting would freeze the class binding and break those patches.
    from agents.blogging.blog_writer_agent.models import WriterInput, WriterOutput
    from agents.blogging.ghost_writer_agent import GhostWriterElicitationAgent
    from agents.blogging.ghost_writer_agent.agent import MAX_ROUNDS_POST_DRAFT
    from agents.blogging.ghost_writer_agent.models import StoryGap
    from agents.blogging.shared.blog_job_store import (
        add_story_agent_message,
        get_blog_job,
        update_blog_job,
    )

    if not isinstance(draft_text, str):
        raise TypeError("draft_text must be a string")
    if "elicited_stories" in draft_input_kwargs:
        raise ValueError("draft_input_kwargs must not contain 'elicited_stories'")
    if not isinstance(plan, ContentPlan):
        raise TypeError("plan must be a ContentPlan")
    if llm_client is None:
        raise TypeError("llm_client must not be None")
    if not callable(getattr(draft_agent, "run", None)):
        raise TypeError("draft_agent must provide a callable run method")

    placeholders = _extract_story_placeholders(draft_text)
    if not placeholders:
        return WriterOutput(draft=draft_text), elicited_stories_text

    logger.info("Post-draft: found %d story placeholder(s) to fill", len(placeholders))
    job_updater(
        phase="story_elicitation",
        progress=35,
        status_text=f"Draft has {len(placeholders)} story placeholder(s) — waiting for your stories...",
    )

    ghost_agent = GhostWriterElicitationAgent(llm_client=llm_client)
    new_narratives: list[str] = []
    skipped_topics: list[str] = []

    # Build story gaps from placeholders
    gaps = []
    for _full_match, topic in placeholders:
        gaps.append(
            StoryGap(
                section_title=topic[:80],
                section_context=f"The draft needs a personal story about: {topic}",
                seed_question=(
                    f"Hey, there's a spot in the post where a personal story about {topic} "
                    f"would really bring it to life. Have you ever had a moment like that? "
                    f"I'd love to hear about it."
                ),
            )
        )

    for idx, gap in enumerate(gaps):
        job_data = get_blog_job(job_id)
        if job_data and job_data.get("status") in ("failed", "cancelled"):
            break

        # Expose only the current gap — one at a time.
        # Use gap_round tagging so the frontend filters by round.
        update_blog_job(
            job_id,
            story_gaps=[gap.model_dump()],
            current_story_gap_index=0,
            current_gap_round=idx,
            waiting_for_story_input=True,
        )
        job_updater(
            phase="story_elicitation",
            progress=min(35 + idx, 39),
            status_text=f"Chatting about your experience with: {gap.section_title}",
        )

        # Post seed question — pipeline pauses here until user responds
        add_story_agent_message(job_id, gap.seed_question, 0)

        # conduct_interview waits indefinitely for each user response
        result = ghost_agent.conduct_interview(
            gap=gap,
            job_id=job_id,
            gap_index=0,
            job_updater=job_updater,
            max_rounds=MAX_ROUNDS_POST_DRAFT,
        )

        if result.skipped:
            skipped_topics.append(gap.section_title)
            logger.info("Post-draft: user has no experience for '%s'", gap.section_title)
        elif result.narrative:
            new_narratives.append(f"[Story for section: {gap.section_title}]\n{result.narrative}")
            # Save to story bank for reuse across future posts
            try:
                from agents.blogging.shared.story_bank import save_story

                save_story(
                    narrative=result.narrative,
                    section_title=gap.section_title,
                    section_context=gap.section_context,
                    keywords=_extract_plan_keywords(plan),
                    source_job_id=job_id,
                    llm_client=llm_client,
                )
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                logger.warning("Story bank save failed (non-fatal): %s", e)
        else:
            # No narrative and not skipped — treat as no usable material
            skipped_topics.append(gap.section_title)

    update_blog_job(
        job_id,
        waiting_for_story_input=False,
        story_gaps=[],
        current_story_gap_index=0,
    )

    if not new_narratives and not skipped_topics:
        return WriterOutput(draft=draft_text), elicited_stories_text

    # Merge new narratives into elicited_stories_text
    if new_narratives:
        new_text = "\n\n".join(new_narratives)
        if elicited_stories_text:
            elicited_stories_text = elicited_stories_text + "\n\n" + new_text
        else:
            elicited_stories_text = new_text

    # Re-draft with the updated stories and skip instructions
    job_updater(
        phase="draft_initial",
        progress=40,
        status_text="Re-drafting with your stories and removing unsupported story sections...",
    )

    skip_instruction = ""
    if skipped_topics:
        skip_list = "; ".join(skipped_topics)
        skip_instruction = (
            f"\n\nSECTIONS WHERE THE AUTHOR HAS NO PERSONAL EXPERIENCE (rewrite these "
            f"sections using research facts, labeled hypotheticals, or straight explanation "
            f"instead of personal stories — remove any [Author: ...] placeholders): {skip_list}"
        )

    try:
        draft_input = WriterInput(
            **draft_input_kwargs,
            elicited_stories=(elicited_stories_text or "") + skip_instruction or None,
        )
        draft_output_path = (
            (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
        )
        redraft_result = draft_agent.run(
            draft_input,
            on_llm_request=lambda msg: job_updater(phase="draft_initial", status_text=msg),
            draft_output_path=draft_output_path,
        )
        logger.info(
            "Post-draft re-draft complete: %d new stories, %d skipped topics, length=%s",
            len(new_narratives),
            len(skipped_topics),
            len(redraft_result.draft),
        )
        return redraft_result, elicited_stories_text
    except CancelledError:
        raise
    except Exception as e:
        if _is_external_cancellation(e):
            raise
        logger.warning("Post-draft re-draft failed (keeping original): %s", e)
        return WriterOutput(draft=draft_text), elicited_stories_text


def _run_title_selection(
    plan: Any,
    llm_client: Any,
    job_id: Optional[str],
    job_updater: Optional[JobUpdater],
    _update: Callable,
) -> Optional[str]:
    """Run the title selection phase: present candidates, process feedback, return loved title.

    Args:
        plan: The content plan; its ``title_candidates`` drive the selection UI.
        llm_client: Resolved LLM client (used to regenerate candidates on feedback).
        job_id: Job identifier, or None to skip title selection.
        job_updater: UI progress callback, or None to skip title selection.
        _update: The phase-progress callback bound to ``job_updater``.

    Preconditions:
        - When title selection runs, both ``job_id`` and ``job_updater`` are non-None
          (either being None short-circuits to a no-op returning None).
    Postconditions:
        - Returns the author-selected title string, or None when title selection is
          skipped (missing job context) or no title is chosen.
    """
    if job_id is None or job_updater is None:
        return None

    try:
        from agents.blogging.shared.blog_job_store import (
            clear_pending_title_feedback,
            get_blog_job,
            get_pending_title_feedback,
            is_waiting_for_title_selection,
        )

        title_choices = [
            {"title": tc.title, "probability_of_success": tc.probability_of_success}
            for tc in plan.title_candidates
        ]

        all_ratings: list[dict] = []
        title_round = 0

        def _process_title_feedback(poll_job_id: str) -> bool:
            """Consume a pending like/dislike rating during a title-selection wait.

            Regenerates (or drops) the rated candidate via the LLM and re-presents
            the list. Returns True when a rating was handled so the poll loop
            re-checks immediately without sleeping; False when nothing was pending.
            """
            nonlocal title_choices, title_round
            pending = get_pending_title_feedback(poll_job_id)
            if not pending:
                return False
            clear_pending_title_feedback(poll_job_id)
            for fb in pending:
                all_ratings.append(fb)

            rated_title = pending[0].get("title", "")
            rating_type = pending[0].get("rating", "like")
            all_liked = [r["title"] for r in all_ratings if r.get("rating") == "like"]
            all_disliked = [r["title"] for r in all_ratings if r.get("rating") == "dislike"]
            all_previous = [r["title"] for r in all_ratings]

            logger.info(
                "Title feedback (round %s): %r rated %r — generating replacement",
                title_round,
                rated_title,
                rating_type,
            )

            feedback_prompt = (
                "Generate exactly 1 new blog post title candidate to replace one that was rated.\n\n"
                f"TOPIC (the article's core argument — the title MUST align with this): {plan.overarching_topic}\n\n"
            )
            if plan.target_reader:
                feedback_prompt += f"TARGET READER: {plan.target_reader}\n\n"
            section_titles = [sec.title for sec in sorted(plan.sections, key=lambda s: s.order)]
            if section_titles:
                feedback_prompt += "ARTICLE SECTIONS:\n"
                feedback_prompt += "\n".join(f"- {t}" for t in section_titles) + "\n\n"
            feedback_prompt += (
                "REQUIREMENTS:\n"
                "- The title MUST accurately reflect the topic above.\n"
                "- The title should promise the reader something concrete and valuable.\n"
                "- Be specific about what the reader will gain.\n\n"
            )
            if all_liked:
                feedback_prompt += (
                    "Titles the user LIKED (generate a title with a similar style/angle):\n"
                )
                feedback_prompt += "\n".join(f"- {t}" for t in all_liked) + "\n\n"
            if all_disliked:
                feedback_prompt += "Titles the user DISLIKED (avoid this style/angle):\n"
                feedback_prompt += "\n".join(f"- {t}" for t in all_disliked) + "\n\n"
            if all_previous:
                feedback_prompt += "DO NOT repeat any of these previous titles:\n"
                feedback_prompt += "\n".join(f"- {t}" for t in all_previous) + "\n\n"
            feedback_prompt += (
                "Return a JSON object with exactly one key: "
                '"titles": [{"title": "...", "probability_of_success": 0.0-1.0}]'
            )

            replacement = None
            try:
                data = llm_client.complete_json(
                    feedback_prompt,
                    temperature=0.7,
                    objective="regenerate blog titles",
                    think=False,
                )
                new_titles = data.get("titles", []) if data else []
                if new_titles and isinstance(new_titles, list):
                    t = new_titles[0]
                    if isinstance(t, dict) and t.get("title"):
                        replacement = {
                            "title": t["title"],
                            "probability_of_success": float(t.get("probability_of_success", 0.5)),
                        }
            except Exception as e:
                logger.warning("Failed to generate replacement title: %s", e)

            if replacement:
                title_choices = [
                    replacement if tc.get("title") == rated_title else tc for tc in title_choices
                ]
            else:
                title_choices = [tc for tc in title_choices if tc.get("title") != rated_title]

            title_round += 1
            job_updater(
                phase="title_selection",
                progress=get_phase_progress(BlogPhase.TITLE_SELECTION, 0.0),
                status_text=f"Rate titles (round {title_round}, {len(title_choices)} candidates)...",
                waiting_for_title_selection=True,
                title_choices=title_choices,
            )
            return True

        while True:
            title_round += 1
            _update(
                BlogPhase.TITLE_SELECTION,
                sub_progress=0.0,
                status_text=f"Rate titles (round {title_round}, {len(title_choices)} candidates)...",
                waiting_for_title_selection=True,
                title_choices=title_choices,
            )

            if _wait_for_hitl(
                job_id,
                is_waiting_for_title_selection,
                on_poll=_process_title_feedback,
            ):
                return None

            job_data = get_blog_job(job_id) or {}
            selected_title = job_data.get("selected_title")

            if selected_title:
                logger.info("Title loved (round %s): %r", title_round, selected_title)
                _update(
                    BlogPhase.TITLE_SELECTION,
                    sub_progress=1.0,
                    status_text=f"Title selected: {selected_title}",
                )
                return selected_title

    except CancelledError:
        raise
    except Exception as e:
        logger.warning("Title selection phase error (skipping): %s", e)
    return None


def _load_required_guidelines(action: str, *, phase: str = "draft") -> Tuple[str, str]:
    """Load the writing-style and brand-spec guideline files, failing loudly if absent.

    Preconditions:
        - ``action`` is a short phrase for the error message (e.g. "start drafting").
        - ``phase`` names the pipeline stage the failure should be attributed to.
    Postconditions:
        - Returns ``(writing_style_content, brand_spec_content)``, both non-empty.
        - Raises ``DraftError(phase=phase)`` naming each missing file when either
          cannot be loaded — agents must never run with silently-empty guidelines —
          so the job store's ``failed_phase`` points at the stage that actually failed.
    """
    # Deferred import: see module docstring.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import load_style_file

    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    if not writing_style_content or not brand_spec_content:
        missing_parts: list[str] = []
        if not writing_style_content:
            missing_parts.append(f"writing guidelines ({STYLE_GUIDE_PATH})")
        if not brand_spec_content:
            missing_parts.append(f"brand guidelines ({BRAND_SPEC_PROMPT_PATH})")
        missing_msg = ", ".join(missing_parts)
        raise DraftError(
            f"Cannot {action} without required guideline inputs. Missing: {missing_msg}.",
            cause=ValueError(missing_msg),
            phase=phase,
        )
    return writing_style_content, brand_spec_content


def _make_update(job_updater: Optional[JobUpdater]) -> Callable[..., None]:
    """Build the phase-progress ``_update`` callback bound to a job_updater.

    Preconditions:
        - ``job_updater`` is either a callable ``(**kwargs) -> None`` or None.
    Postconditions:
        - Returns a callable ``(phase, sub_progress=0.0, status_text="", **kwargs)``
          that forwards a computed overall progress to ``job_updater`` (no-op when
          ``job_updater`` is None). Re-raises CancelledError; swallows other
          job-update failures (identical to the pipeline's former inline closure).
    """

    def _update(
        phase: BlogPhase,
        sub_progress: float = 0.0,
        status_text: str = "",
        **kwargs: Any,
    ) -> None:
        if job_updater:
            try:
                progress = get_phase_progress(phase, sub_progress)
                job_updater(
                    phase=phase.value,
                    progress=progress,
                    status_text=status_text,
                    **kwargs,
                )
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to update job status: %s", e)

    return _update


def _save_narratives_to_story_bank(
    collected_story_pairs: List[Tuple["StoryGap", str]],
    *,
    topic_keywords: List[str],
    job_id: Optional[str],
    llm_client: Any,
) -> int:
    """Persist each elicited narrative to the story bank under its own story gap.

    The gap→narrative pairing is captured at collection time (see ``run_planning_stage``),
    so each narrative is stored against the exact gap it was elicited for — no substring
    re-matching, which was O(n*m) and could mis-associate a narrative with a gap whose
    ``section_title`` merely appeared as a substring of another section's story.

    Preconditions:
        - Each entry in ``collected_story_pairs`` is ``(gap, raw_narrative)`` where
          ``raw_narrative`` is the unformatted narrative text (no
          ``"[Story for section: ...]"`` prefix).
        - ``topic_keywords`` is the keyword list to tag every saved story with.

    Postconditions:
        - ``save_story`` is attempted exactly once per pair, using that pair's own gap
          ``section_title`` and ``section_context``.
        - A ``save_story`` failure for one pair is caught and logged (non-fatal); the batch
          continues so one bad story never loses the remaining saves.
        - Returns the count of narratives *successfully* persisted (0 ..
          ``len(collected_story_pairs)``).

    Raises:
        CancelledError: a Temporal-native (or otherwise external) cancellation propagates
            unchanged — it is never swallowed by the non-fatal per-pair guard.
    """
    from agents.blogging.shared.story_bank import save_story

    saved = 0
    for story_gap, raw_narrative in collected_story_pairs:
        try:
            save_story(
                narrative=raw_narrative,
                section_title=story_gap.section_title,
                section_context=story_gap.section_context,
                keywords=topic_keywords,
                source_job_id=job_id,
                llm_client=llm_client,
            )
            saved += 1
        except CancelledError:
            raise
        except Exception as e:  # non-fatal: one bad story must not lose the rest
            if _is_external_cancellation(e):
                raise
            logger.warning(
                "Story bank save failed for section %r (non-fatal): %s",
                story_gap.section_title,
                e,
            )
    return saved
