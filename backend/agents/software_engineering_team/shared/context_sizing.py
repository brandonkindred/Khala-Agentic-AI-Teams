"""
Compute max chunk sizes from model context for analysis agents.

Uses chars_per_token ~3.5 (conservative for code/spec text) and reserves
tokens for system prompt, task/spec context, and response.

Reserved values are sized to exceed actual agent prompt token counts so that
chunk + prompt + response stays within the model context window.
"""

from __future__ import annotations

from llm_service import LLMClient

# Re-exported from the shared typed env-config helper so the int-knob parser
# has a single implementation; exposed here under the name ``parse_env_int``,
# which the code-review coordinator and tests import from this module.
from software_engineering_team.shared.env_config import env_int as parse_env_int

# Conservative chars per token for code/spec (used for token estimates from char counts)
CHARS_PER_TOKEN = 3.5

# Absolute ceilings for code-review map calls, independent of model context.
# Large-context models (1M tokens) make the context-derived sizes useless as a
# bound: a single review prompt can carry ~200K tokens of code and the model
# burns its whole completion budget on reasoning before emitting any findings.
# ~80K chars is ~23K tokens of code per map call, leaving completion-budget
# headroom for reasoning plus the JSON findings payload.
# Each default is env-overridable (see docs/ENV_VARS.md); the env var named
# next to each constant is parsed defensively and clamped to the given floor.
CODE_REVIEW_ABS_CHUNK_CHARS = 80_000  # CODE_REVIEW_MAP_CHUNK_CHARS, floor 10_000
CODE_REVIEW_SPEC_EXCERPT_ABS_CHARS = 16_000  # CODE_REVIEW_SPEC_EXCERPT_CHARS, floor 1_000
CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS = 4_000  # CODE_REVIEW_ARCH_OVERVIEW_CHARS, floor 500
CODE_REVIEW_EXISTING_ABS_CHARS = 8_000  # CODE_REVIEW_EXISTING_CHARS, floor 500

# Default cap on the cross-file "sibling surface" block added to every map prompt
# (the top-level symbols the other changed files define/export). Reserved in the
# per-chunk budget below and used by the chunk reviewer + coordinator to bound
# the block, so a single value keeps the reservation and the truncation in sync
# (the sibling surface carries only symbol names, so a small cap suffices).
CODE_REVIEW_SIBLING_SURFACE_ABS_CHARS = 2_000  # CODE_REVIEW_SIBLING_SURFACE_CHARS, floor 0
# Shared floor for the CODE_REVIEW_MAP_CHUNK_CHARS env override.
_CODE_REVIEW_MAP_CHUNK_CHARS_FLOOR = 10_000


def code_review_map_chunk_chars_cap() -> int:
    """Absolute code-inline ceiling for map calls.

    Postconditions:
        - Reads ``CODE_REVIEW_MAP_CHUNK_CHARS`` (default
          ``CODE_REVIEW_ABS_CHUNK_CHARS``, floor
          ``_CODE_REVIEW_MAP_CHUNK_CHARS_FLOOR``). Never raises.
    """
    return parse_env_int(
        "CODE_REVIEW_MAP_CHUNK_CHARS",
        CODE_REVIEW_ABS_CHUNK_CHARS,
        _CODE_REVIEW_MAP_CHUNK_CHARS_FLOOR,
    )


def compute_code_review_sibling_surface_chars() -> int:
    """Max chars of the sibling-surface block per map prompt.

    Postconditions:
        - Reads ``CODE_REVIEW_SIBLING_SURFACE_CHARS`` (default
          ``CODE_REVIEW_SIBLING_SURFACE_ABS_CHARS``, floor ``0``). ``0`` drops the
          block entirely. A single reader keeps the budget reservation, the
          ``_sibling_surface`` truncation, and the prompt slice in sync.
    """
    return parse_env_int(
        "CODE_REVIEW_SIBLING_SURFACE_CHARS", CODE_REVIEW_SIBLING_SURFACE_ABS_CHARS, 0
    )


def compute_max_chunk_chars(
    context_tokens: int,
    *,
    reserved_prompt_tokens: int = 6000,
    reserved_response_tokens: int = 4096,
    chars_per_token: float = CHARS_PER_TOKEN,
    min_chars: int = 8000,
    num_chunks: int = 1,
    max_fraction_of_context: float = 0.4,
) -> int:
    """
    Compute max chars for analysis chunk(s) given model context size.

    Args:
        context_tokens: Model's max context (from llm.get_max_context_tokens()).
        reserved_prompt_tokens: Tokens for system prompt, task, spec excerpt, etc.
        reserved_response_tokens: Tokens reserved for LLM response.
        chars_per_token: Conservative chars-per-token (~3.5 for code/spec).
        min_chars: Minimum chunk size (fallback for small models).
        num_chunks: When >1, divides available space so multiple chunks fit in one prompt.
        max_fraction_of_context: Per-section ceiling as a fraction of the model
            context. Sections are sized independently but agents combine
            several in one prompt (e.g. spec content + existing code in
            BackendAgent._plan_task); without this ceiling, large-context
            models (1M tokens) let each section claim nearly the whole window
            and the combination overflows. 0.4 guarantees any two sections
            plus reserves fit.

    Returns:
        Max chars to use per chunk.
    """
    available_tokens = context_tokens - reserved_prompt_tokens - reserved_response_tokens
    available_tokens = min(available_tokens, int(context_tokens * max_fraction_of_context))
    if available_tokens < 512:
        available_tokens = 512  # ensure some room for tiny models
    if num_chunks > 1:
        available_tokens = available_tokens // num_chunks
    return max(min_chars, int(available_tokens * chars_per_token))


def compute_code_review_chunk_chars(llm: LLMClient) -> int:
    """
    Max chars per code review chunk. Reserves for CODE_REVIEW_PROMPT (~2K),
    task (~1K), the scaled spec/arch/existing excerpts that are in every chunk,
    and the cross-file sibling-surface block (``CODE_REVIEW_SIBLING_SURFACE_CHARS``).

    Postconditions:
        - The context-derived size is bounded by the absolute map-call ceiling
          (``CODE_REVIEW_MAP_CHUNK_CHARS`` env override, default
          ``CODE_REVIEW_ABS_CHUNK_CHARS``), so no caller can ever build an
          unbounded review prompt regardless of model context.
    """
    ctx = llm.get_max_context_tokens()
    spec_chars = compute_code_review_spec_excerpt_chars(llm)
    arch_chars = compute_code_review_arch_overview_chars(llm)
    existing_chars = compute_code_review_existing_codebase_chars(llm)
    excerpt_tokens = int((spec_chars + arch_chars + existing_chars) / CHARS_PER_TOKEN)
    sibling_tokens = int(compute_code_review_sibling_surface_chars() / CHARS_PER_TOKEN)
    # prompt + task + spec/arch/existing excerpts + the cross-file sibling surface
    reserved_prompt = 3000 + excerpt_tokens + sibling_tokens
    derived = compute_max_chunk_chars(
        ctx,
        reserved_prompt_tokens=reserved_prompt,
        reserved_response_tokens=4096,
        min_chars=12000,
    )
    cap = code_review_map_chunk_chars_cap()
    return min(derived, cap)


def compute_code_review_map_chunk_chars(llm: LLMClient) -> int:
    """Max chars of code per map call in the code-review coordinator.

    Alias of ``compute_code_review_chunk_chars`` (the absolute cap now lives
    there, so the capped and "raw" sizes can never diverge again).
    """
    return compute_code_review_chunk_chars(llm)


def compute_spec_chunk_chars(llm: LLMClient) -> int:
    """
    Max chars per spec chunk (SpecChunkAnalyzer). Reserves ~4K for
    SPEC_CHUNK_ANALYZER_PROMPT + requirements header + chunk metadata,
    ~4K for response. Kept tight for faster planning.
    """
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=4000,
        reserved_response_tokens=4096,
        min_chars=6000,
    )


def _scale_with_context(llm: LLMClient, base_at_16k: int, max_chars: int = 700_000) -> int:
    """
    Scale a 16K-context base value by actual model context.
    Capped at max_chars so 256K models can use full context (~256k * 3.5 chars/token).
    """
    ctx = llm.get_max_context_tokens()
    scaled = max(base_at_16k, int(base_at_16k * ctx / 16384))
    return min(scaled, max_chars)


def compute_code_review_spec_excerpt_chars(llm: LLMClient) -> int:
    """Max chars for spec excerpt in code review.

    Scales with model context but absolutely capped: this excerpt repeats in
    every map call of the review coordinator, so an uncapped 1M-context scale
    (~488K chars) would dominate each chunk prompt.
    """
    cap = parse_env_int("CODE_REVIEW_SPEC_EXCERPT_CHARS", CODE_REVIEW_SPEC_EXCERPT_ABS_CHARS, 1_000)
    return _scale_with_context(llm, 8_000, max_chars=cap)


def compute_code_review_arch_overview_chars(llm: LLMClient) -> int:
    """Max chars for architecture overview in code review (scaled, absolutely capped)."""
    cap = parse_env_int("CODE_REVIEW_ARCH_OVERVIEW_CHARS", CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS, 500)
    return _scale_with_context(llm, 2_000, max_chars=cap)


def compute_code_review_existing_codebase_chars(llm: LLMClient) -> int:
    """Max chars for existing codebase excerpt in code review (scaled, absolutely capped)."""
    cap = parse_env_int("CODE_REVIEW_EXISTING_CHARS", CODE_REVIEW_EXISTING_ABS_CHARS, 500)
    return _scale_with_context(llm, 4_000, max_chars=cap)


def compute_existing_code_chars(llm: LLMClient) -> int:
    """
    Max chars for existing codebase when passed to coding agents.
    Reserves ~12K for BACKEND_PROMPT/FRONTEND_PROMPT (~5K) + task + spec + architecture.
    """
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=12_000,
        reserved_response_tokens=8192,
        min_chars=20_000,
    )


def compute_spec_content_chars(llm: LLMClient) -> int:
    """
    Max chars for spec content in agent prompts.
    Reserves ~12K for agent prompt + task + architecture.
    """
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=12_000,
        reserved_response_tokens=8192,
        min_chars=15_000,
    )


def compute_spec_excerpt_chars(llm: LLMClient) -> int:
    """Max chars for spec excerpt in refine_task and similar (smaller prompts)."""
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=4000,
        reserved_response_tokens=4096,
        min_chars=8_000,
    )


def compute_pra_spec_review_spec_chars(llm: LLMClient) -> int:
    """Max chars for spec in PRA spec review (large prompt template + response)."""
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=55_000,
        reserved_response_tokens=8192,
        min_chars=20_000,
    )


def compute_prd_snippet_chars(llm: LLMClient) -> int:
    """Max chars per PRD input snippet (cleaned_spec, answered_summary, specialist_plan)."""
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=20_000,
        reserved_response_tokens=8192,
        min_chars=20_000,
        num_chunks=3,
    )


def compute_build_errors_chars(llm: LLMClient) -> int:
    """Max chars for build/test errors in retry context."""
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=12000,
        reserved_response_tokens=8192,
        min_chars=4_000,
    )


def compute_api_spec_chars(llm: LLMClient) -> int:
    """Max chars for API spec/endpoints in frontend context."""
    return _scale_with_context(llm, 20_000)


def compute_task_generator_spec_chars(llm: LLMClient) -> int:
    """
    Max chars for spec/codebase/existing in Task Generator prompt.
    Reserves ~110K for TECH_LEAD_PROMPT (~11K) + requirements + project_overview
    + features (scaled, capped) + merged_spec_analysis + arch (scaled, capped).
    Divides available space by 3 since spec, codebase, and existing share the prompt.
    """
    return compute_max_chunk_chars(
        llm.get_max_context_tokens(),
        reserved_prompt_tokens=110_000,
        reserved_response_tokens=8192,
        min_chars=12_000,
        num_chunks=3,
    )


def compute_task_generator_existing_chars(llm: LLMClient) -> int:
    """Max chars for existing code in Task Generator prompt."""
    return compute_task_generator_spec_chars(llm)


def compute_task_generator_features_chars(llm: LLMClient) -> int:
    """Max chars for features doc in Task Generator prompt. Tighter for faster planning."""
    return _scale_with_context(llm, 6_000)


def compute_task_generator_arch_chars(llm: LLMClient) -> int:
    """Max chars for architecture doc in Task Generator prompt."""
    return _scale_with_context(llm, 5_000)


def compute_spec_outline_chars(llm: LLMClient) -> int:
    """Max chars for spec outline in SpecAnalysisMerger. Tighter for faster planning."""
    return _scale_with_context(llm, 1_500)


def compute_repo_summary_chars(llm: LLMClient) -> int:
    """Max chars for repo state summary in Project Planning."""
    return _scale_with_context(llm, 2_000)


def compute_requirement_mapping_chars(llm: LLMClient) -> int:
    """Max chars for requirement-task mapping in prompts."""
    return _scale_with_context(llm, 2_000)
