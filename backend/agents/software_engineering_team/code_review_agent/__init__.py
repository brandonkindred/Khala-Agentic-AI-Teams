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

# Every submodule here reaches ``strands``/``llm_service``/``httpx``/``boto3``
# one way or another — directly (``agent``, ``chunk_reviewer``,
# ``false_positive_filter`` import ``strands``/``llm_service`` at module scope)
# or transitively (``profiles`` imports ``coding_standards``/``prompt_utils``/
# ``prompts.requirement_citation`` from ``software_engineering_team.shared``, and
# importing ANY submodule of ``software_engineering_team.shared`` first
# executes ``shared/__init__.py``, which eagerly imports ``.llm`` — itself a
# top-level ``from strands import Agent``). Since importing ``code_review_agent``
# (e.g. as an ancestor of ``code_review_agent.temporal.workflows``, which the
# Temporal workflow sandbox must import to register ``CodeReviewWorkflow``)
# always runs this file top to bottom first, ANY eager submodule import here —
# even one that looks "safe" — reintroduces the same unsafe chain. botocore's
# import-time thread-lock/dynamic-class setup and httpx's module-scope
# ``class _CookieCompatRequest(urllib.request.Request)`` are not things the
# sandbox can replay. So every re-export here is resolved lazily via PEP 562
# ``__getattr__`` (mirroring ``llm_service.__init__``'s treatment of
# ``strands_adapter``): a plain ``from code_review_agent import CodeReviewAgent``
# still works, but merely importing this package touches no submodule at all.
if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .agent import CodeReviewAgent  # noqa: F401
    from .architecture_consistency_pass import (  # noqa: F401
        find_architecture_and_redundancy_issues,
    )
    from .chunk_reviewer import ChunkReviewAgent  # noqa: F401
    from .false_positive_filter import CodebaseIndex, filter_false_positives  # noqa: F401
    from .merged_architecture_side_effect_pass import (  # noqa: F401
        find_architecture_and_side_effect_issues,
    )
    from .models import (  # noqa: F401
        ChunkReviewInput,
        ChunkReviewOutput,
        CodeReviewInput,
        CodeReviewOutput,
        CodeReviewUnavailableError,
    )
    from .profiles import ReviewProfile, build_review_system_prompt  # noqa: F401
    from .scope_filter import (  # noqa: F401
        ScopeVerdict,
        apply_scope_verdicts,
        apply_scope_verification,
        finding_overlaps_changed_lines,
    )
    from .side_effect_impact_pass import (  # noqa: F401
        find_side_effect_impact_issues,
    )

_LAZY_EXPORTS = {
    "CodeReviewAgent": "agent",
    "ChunkReviewAgent": "chunk_reviewer",
    "CodebaseIndex": "false_positive_filter",
    "filter_false_positives": "false_positive_filter",
    "find_architecture_and_redundancy_issues": "architecture_consistency_pass",
    "find_side_effect_impact_issues": "side_effect_impact_pass",
    "find_architecture_and_side_effect_issues": "merged_architecture_side_effect_pass",
    "ChunkReviewInput": "models",
    "ChunkReviewOutput": "models",
    "CodeReviewInput": "models",
    "CodeReviewOutput": "models",
    "CodeReviewUnavailableError": "models",
    "ReviewProfile": "profiles",
    "build_review_system_prompt": "profiles",
    "apply_scope_verification": "scope_filter",
    "apply_scope_verdicts": "scope_filter",
    "finding_overlaps_changed_lines": "scope_filter",
    "ScopeVerdict": "scope_filter",
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
    "ScopeVerdict",
    "apply_scope_verdicts",
    "apply_scope_verification",
    "build_review_system_prompt",
    "filter_false_positives",
    "find_architecture_and_redundancy_issues",
    "find_side_effect_impact_issues",
    "find_architecture_and_side_effect_issues",
    "finding_overlaps_changed_lines",
]
