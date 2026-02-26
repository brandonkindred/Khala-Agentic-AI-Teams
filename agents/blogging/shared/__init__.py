"""
Shared utilities for the blogging agent suite.

Provides artifact persistence, brand spec loading, error handling, and other common functionality.
"""

from .artifacts import (
    ARTIFACT_NAMES,
    read_artifact,
    write_artifact,
)
from .brand_spec import BrandSpec, load_brand_spec
from .errors import (
    BloggingError,
    ComplianceError,
    CopyEditError,
    DraftError,
    FactCheckError,
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMUnreachableError,
    PublicationError,
    ResearchError,
    ReviewError,
    ValidationError,
)
from .blog_job_store import (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_NEEDS_REVIEW,
    create_blog_job,
    get_blog_job,
    list_blog_jobs,
    update_blog_job,
    start_blog_job,
    complete_blog_job,
    fail_blog_job,
    delete_blog_job,
)
from .models import (
    BlogPhase,
    PHASE_PROGRESS_RANGES,
    PHASE_ORDER,
    get_phase_progress,
    get_completed_phases,
)

__all__ = [
    "ARTIFACT_NAMES",
    "BrandSpec",
    "BloggingError",
    "ComplianceError",
    "CopyEditError",
    "DraftError",
    "FactCheckError",
    "LLMError",
    "LLMJsonParseError",
    "LLMRateLimitError",
    "LLMTemporaryError",
    "LLMUnreachableError",
    "PublicationError",
    "ResearchError",
    "ReviewError",
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
    "load_brand_spec",
    "read_artifact",
    "write_artifact",
]
