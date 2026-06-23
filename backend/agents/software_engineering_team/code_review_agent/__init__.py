"""Code review agent package.

Public surface for reviewing code produced by the coding agents against the
spec, standards, and conventions. ``CodeReviewAgent`` is the entry point; it
delegates to the map-reduce coordinator, which bounds every LLM call, re-checks
each finding against the whole submission to drop false positives
(``filter_false_positives`` over a ``CodebaseIndex``), and applies the
deterministic approval gate. The request/response models are re-exported for
callers that build inputs or inspect results.
"""

from .agent import CodeReviewAgent
from .chunk_reviewer import ChunkReviewAgent
from .false_positive_filter import CodebaseIndex, filter_false_positives
from .models import (
    ChunkReviewInput,
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewOutput,
    CodeReviewUnavailableError,
)

__all__ = [
    "ChunkReviewAgent",
    "ChunkReviewInput",
    "ChunkReviewOutput",
    "CodeReviewAgent",
    "CodeReviewInput",
    "CodeReviewOutput",
    "CodeReviewUnavailableError",
    "CodebaseIndex",
    "filter_false_positives",
]
