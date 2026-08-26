"""
Shared utilities for the blogging agent suite.

Provides artifact persistence, brand spec loading, error handling, and other common functionality.
"""

from .agent_base import _BlogAgentBase
from .artifacts import (
    ARTIFACT_NAMES,
    read_artifact,
    write_artifact,
)
from .blog_job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_NEEDS_REVIEW,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    complete_blog_job,
    create_blog_job,
    delete_blog_job,
    fail_blog_job,
    get_blog_job,
    list_blog_jobs,
    start_blog_job,
    update_blog_job,
)
from .brand_spec import BrandSpec, load_brand_spec_prompt
from .content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    build_planning_length_context,  # noqa: F401
    build_review_length_context,
    resolve_length_policy,
    resolve_length_policy_from_request_dict,
    series_context_block,
)
from .errors import (
    BloggingError,
    ComplianceError,
    CopyEditError,
    DraftError,
    FactCheckError,
    PlanningError,
    PublicationError,
    ValidationError,
)
from .gate_report import (
    GateReport,
    GateSeverity,
    GateStatus,
    GateViolation,
)
from .json_retry import (
    AgentFactory,
    AgentInvoker,
    call_json_with_retry,
    run_json_gate,
)
from .models import (
    PHASE_ORDER,
    PHASE_PROGRESS_RANGES,
    BlogPhase,
    get_completed_phases,
    get_phase_progress,
)
from .style_loader import load_style_file
from .word_count import count_words

__all__ = [
    "_BlogAgentBase",
    "ARTIFACT_NAMES",
    "BrandSpec",
    "BloggingError",
    "ComplianceError",
    "CopyEditError",
    "DraftError",
    "FactCheckError",
    "PublicationError",
    "PlanningError",
    "ValidationError",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_NEEDS_REVIEW",
    "create_blog_job",
    "get_blog_job",
    "list_blog_jobs",
    "update_blog_job",
    "start_blog_job",
    "complete_blog_job",
    "fail_blog_job",
    "delete_blog_job",
    "BlogPhase",
    "PHASE_PROGRESS_RANGES",
    "PHASE_ORDER",
    "get_phase_progress",
    "get_completed_phases",
    "load_brand_spec_prompt",
    "load_style_file",
    "read_artifact",
    "write_artifact",
    "GateReport",
    "GateSeverity",
    "GateStatus",
    "GateViolation",
    "AgentFactory",
    "AgentInvoker",
    "call_json_with_retry",
    "run_json_gate",
    "ContentProfile",
    "LengthPolicy",
    "SeriesContext",
    "build_draft_length_instruction",
    "build_review_length_context",
    "resolve_length_policy",
    "resolve_length_policy_from_request_dict",
    "series_context_block",
    "count_words",
]
