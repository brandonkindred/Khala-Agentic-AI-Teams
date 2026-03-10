"""
Compute max chunk sizes from model context for analysis agents.

Uses chars_per_token ~3.5 (conservative for code/spec text) and reserves
tokens for system prompt, task/spec context, and response.

Reserved values are sized to exceed actual agent prompt token counts so that
chunk + prompt + response stays within the model context window.
"""

from __future__ import annotations

from software_engineering_team.shared.llm import LLMClient

# Conservative chars per token for code/spec (used for token estimates from char counts)
CHARS_PER_TOKEN = 3.5


def compute_max_chunk_chars(
    context_tokens: int,
    *,
    reserved_prompt_tokens: int = 6000,
    reserved_response_tokens: int = 4096,
    chars_per_token: float = CHARS_PER_TOKEN,
    min_chars: int = 8000,
    num_chunks: int = 1,
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

    Returns:
        Max chars to use per chunk.
    """
    available_tokens = context_tokens - reserved_prompt_tokens - reserved_response_tokens
    if available_tokens < 512:
        available_tokens = 512  # ensure some room for tiny models
    if num_chunks > 1:
        available_tokens = available_tokens // num_chunks
    return max(min_chars, int(available_tokens * chars_per_token))


def compute_code_review_chunk_chars(llm: LLMClient) -> int:
    """
    Max chars per code review chunk. Reserves for CODE_REVIEW_PROMPT (~2K),
    task (~1K), and the scaled spec/arch/existing excerpts that are in every chunk.
    """
    ctx = llm.get_max_context_tokens()
    spec_chars = compute_code_review_spec_excerpt_chars(llm)
    arch_chars = compute_code_review_arch_overview_chars(llm)
    existing_chars = compute_code_review_existing_codebase_chars(llm)
    excerpt_tokens = int((spec_chars + arch_chars + existing_chars) / CHARS_PER_TOKEN)
    reserved_prompt = 3000 + excerpt_tokens  # prompt + task + spec/arch/existing excerpts
    return compute_max_chunk_chars(
        ctx,
        reserved_prompt_tokens=reserved_prompt,
        reserved_response_tokens=4096,
        min_chars=12000,
    )


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
    """Max chars for spec excerpt in code review (scales with model context)."""
    return _scale_with_context(llm, 8_000)


def compute_code_review_arch_overview_chars(llm: LLMClient) -> int:
    """Max chars for architecture overview in code review (scales with model context)."""
    return _scale_with_context(llm, 2_000)


def compute_code_review_existing_codebase_chars(llm: LLMClient) -> int:
    """Max chars for existing codebase excerpt in code review (scales with model context)."""
    return _scale_with_context(llm, 4_000)


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


def compute_code_review_total_chars(llm: LLMClient) -> int:
    """Max total code chars for code review (fits within 256K context when available)."""
    return _scale_with_context(llm, 150_000)
