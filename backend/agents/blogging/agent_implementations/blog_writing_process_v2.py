"""Backward-compatible re-export shim over the ``pipeline`` package.

The pipeline implementation — context, constants, shared helpers, and one module per
stage (planning -> draft -> interactive user review -> copy-editor loop -> gates) —
lives in ``agent_implementations/pipeline/``. This module re-exports every symbol
consumed by external callers (Temporal activities, the job-runner, the API layer, and
the test suite) so existing import paths keep working with zero churn, including
``agents.blogging.temporal`` binding ``WORKFLOWS``/``ACTIVITIES`` through this shim.

A handful of the ``pipeline`` package's own functions look up one of their
collaborators (an agent class, a loader, a stage function, ...) via a deferred import
from *this* module rather than a normal top-level import from the collaborator's true
source or a sibling pipeline module. That is what keeps
``monkeypatch.setattr(blog_writing_process_v2, "BlogWriterAgent", ...)``-style test
patches effective now that the code that consumes them lives outside this file — see
``pipeline._common``'s module docstring for the full rationale. Every name reachable
that way must stay defined here, at this module's top level, for that mechanism to
keep working; do not move one of these imports into a stage module "for tidiness".

Do not add new pipeline logic here — extend the appropriate ``pipeline/*`` module
instead. This file should only ever grow re-exports.
"""

import time  # noqa: F401 -- re-exported so tests can patch time.sleep via this module.

from agents.blogging.blog_compliance_agent import BlogComplianceAgent
from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent
from agents.blogging.blog_publication_agent.models import PublishingPack
from agents.blogging.blog_research_agent.agent import ResearchAgent
from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.blog_writer_agent import BlogWriterAgent, ReviseWriterInput, WriterInput
from agents.blogging.shared.artifacts import write_artifact
from agents.blogging.shared.blog_job_store import (
    add_blog_pending_questions,
    get_blog_job,
    is_waiting_for_blog_answers,
    record_guideline_updates,
)
from agents.blogging.shared.brand_spec import load_brand_spec_prompt
from agents.blogging.shared.content_plan import (
    ContentPlan,
    PlanningInput,
    PlanningPhaseResult,
    content_plan_to_content_brief_markdown,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    build_planning_length_context,
    resolve_length_policy,
    series_context_block,
)
from agents.blogging.shared.errors import (
    BloggingError,
    ComplianceError,
    DraftError,
    FactCheckError,
    PlanningError,
)
from agents.blogging.shared.models import BlogPhase, get_phase_progress
from agents.blogging.shared.planning_config import (
    plan_critic_enabled,
    plan_critic_max_iterations,
    plan_critic_model_override,
    planning_model_override,
)
from agents.blogging.shared.run_pipeline_job import _is_external_cancellation
from agents.blogging.shared.style_loader import append_guidelines, load_style_file
from agents.blogging.validators.runner import run_validators_from_work_dir
from temporalio.exceptions import CancelledError

from llm_service import (
    LLMClientModel,
    OllamaLLMClient,
    get_strands_model,
    with_model_override,
)
from llm_service.interface import LLMClient, LLMRateLimitError, LLMTemporaryError
from shared.concurrency import parallel_map

from . import _path_setup  # noqa: F401
from .pipeline._common import (
    _apply_stage_model_override,
    _extract_plan_keywords,
    _extract_story_placeholders,
    _fill_story_placeholders,
    _load_required_guidelines,
    _make_update,
    _run_title_selection,
    _save_narratives_to_story_bank,
    _wait_for_hitl,
    build_plan_critic_agent,
    plan_critic_llm_client,
    planning_llm_client,
    run_planning,
)
from .pipeline.constants import (
    BRAND_SPEC_PROMPT_PATH,
    COPY_EDIT_ESCALATION_THRESHOLD,
    DRAFT_EDITOR_ITERATIONS,
    HITL_MAX_CONSECUTIVE_READ_ERRORS,
    HITL_POLL_INTERVAL_S,
    MAX_REWRITE_ITERATIONS,
    STYLE_GUIDE_PATH,
)
from .pipeline.context import JobUpdater, PipelineContext, PipelineStatus
from .pipeline.draft_stage import run_draft_stage
from .pipeline.gates_stage import run_gates_stage
from .pipeline.planning_stage import run_planning_stage
from .pipeline.runner import main, run_pipeline

__all__ = [
    "BRAND_SPEC_PROMPT_PATH",
    "COPY_EDIT_ESCALATION_THRESHOLD",
    "DRAFT_EDITOR_ITERATIONS",
    "HITL_MAX_CONSECUTIVE_READ_ERRORS",
    "HITL_POLL_INTERVAL_S",
    "MAX_REWRITE_ITERATIONS",
    "STYLE_GUIDE_PATH",
    "BlogComplianceAgent",
    "BlogCopyEditorAgent",
    "BlogFactCheckAgent",
    "BlogPhase",
    "BlogPlanCriticAgent",
    "BlogWriterAgent",
    "BloggingError",
    "CancelledError",
    "ComplianceError",
    "ContentPlan",
    "ContentProfile",
    "CopyEditorInput",
    "DraftError",
    "FactCheckError",
    "FeedbackItem",
    "JobUpdater",
    "LLMClient",
    "LLMClientModel",
    "LLMRateLimitError",
    "LLMTemporaryError",
    "LengthPolicy",
    "OllamaLLMClient",
    "PipelineContext",
    "PipelineStatus",
    "PlanningError",
    "PlanningInput",
    "PlanningPhaseResult",
    "PublishingPack",
    "ResearchAgent",
    "ResearchBriefInput",
    "ReviseWriterInput",
    "SeriesContext",
    "WriterInput",
    "_apply_stage_model_override",
    "_extract_plan_keywords",
    "_extract_story_placeholders",
    "_fill_story_placeholders",
    "_is_external_cancellation",
    "_load_required_guidelines",
    "_make_update",
    "_run_title_selection",
    "_save_narratives_to_story_bank",
    "_wait_for_hitl",
    "add_blog_pending_questions",
    "append_guidelines",
    "build_draft_length_instruction",
    "build_planning_length_context",
    "build_plan_critic_agent",
    "content_plan_to_content_brief_markdown",
    "content_plan_to_markdown_doc",
    "content_plan_to_outline_markdown",
    "get_blog_job",
    "get_phase_progress",
    "get_strands_model",
    "is_waiting_for_blog_answers",
    "load_brand_spec_prompt",
    "load_style_file",
    "main",
    "parallel_map",
    "plan_critic_enabled",
    "plan_critic_llm_client",
    "plan_critic_max_iterations",
    "plan_critic_model_override",
    "planning_llm_client",
    "planning_model_override",
    "record_guideline_updates",
    "resolve_length_policy",
    "run_draft_stage",
    "run_gates_stage",
    "run_pipeline",
    "run_planning",
    "run_planning_stage",
    "run_validators_from_work_dir",
    "series_context_block",
    "time",
    "with_model_override",
    "write_artifact",
]


if __name__ == "__main__":
    main()
