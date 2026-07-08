"""Code review agent package.

Public surface for reviewing code produced by the coding agents against the
spec, standards, and conventions. ``CodeReviewAgent`` is the entry point; it
delegates to the map-reduce coordinator, which bounds every LLM call, re-checks
each finding against the whole submission to drop false positives
(``filter_false_positives`` over a ``CodebaseIndex``), and applies the
deterministic approval gate. The request/response models are re-exported for
callers that build inputs or inspect results.
"""

from typing import TYPE_CHECKING, Any

from .models import (
    ChunkReviewInput,
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewOutput,
    CodeReviewUnavailableError,
)
from .profiles import ReviewProfile, build_review_system_prompt

# ``agent``, ``chunk_reviewer``, and ``false_positive_filter`` import ``strands``
# and ``llm_service`` at module scope, which transitively pull in ``boto3``/
# ``botocore``/``httpx``. Importing this package (e.g. as an ancestor of
# ``code_review_agent.temporal.workflows``, which the Temporal workflow sandbox
# must import to register ``CodeReviewWorkflow``) would otherwise import that
# whole LLM stack as a side effect — which the sandbox cannot safely replay
# (botocore's import-time thread-lock/dynamic-class setup, httpx's module-scope
# ``class _CookieCompatRequest(urllib.request.Request)``). Resolve these lazily
# via PEP 562 ``__getattr__`` (mirroring ``llm_service.__init__``'s treatment of
# ``strands_adapter``) so a plain ``from code_review_agent import CodeReviewAgent``
# still works, but merely importing this package does not.
if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .agent import CodeReviewAgent  # noqa: F401
    from .chunk_reviewer import ChunkReviewAgent  # noqa: F401
    from .false_positive_filter import CodebaseIndex, filter_false_positives  # noqa: F401

_LAZY_EXPORTS = {
    "CodeReviewAgent": "agent",
    "ChunkReviewAgent": "chunk_reviewer",
    "CodebaseIndex": "false_positive_filter",
    "filter_false_positives": "false_positive_filter",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "ChunkReviewAgent",
    "ChunkReviewInput",
    "ChunkReviewOutput",
    "CodeReviewAgent",
    "CodeReviewInput",
    "CodeReviewOutput",
    "CodeReviewUnavailableError",
    "CodebaseIndex",
    "ReviewProfile",
    "build_review_system_prompt",
    "filter_false_positives",
]
