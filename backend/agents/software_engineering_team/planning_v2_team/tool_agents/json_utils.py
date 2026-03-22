"""Shared text completion and truncation handling for planning_v2_team tool agents.

Uses text-only LLM calls with continuation on truncation. No JSON is required;
consumers parse responses via output_templates (section markers).
"""

import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from software_engineering_team.shared.deduplication import dedupe_strings

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

MAX_CONTINUATION_CYCLES = 10
DEFAULT_CHUNK_SIZE = 4000


def complete_text_with_continuation(
    llm: "LLMClient",
    prompt: str,
    *,
    agent_name: str = "PlanningV2",
    max_continuation_cycles: int = MAX_CONTINUATION_CYCLES,
) -> str:
    """Call LLM with text mode; truncation is handled by the client (llm_service).

    Does not require or parse JSON. Callers should use output_templates to parse
    the returned text. Continuation on truncation is performed inside the
    Ollama client (same as complete_json); this wrapper delegates to llm.complete_text.
    """
    return llm.complete_text(prompt)


def attempt_fix_output_continuation(
    llm: "LLMClient",
    prompt: str,
    raw_text: str,
    agent_name: str,
    *,
    max_cycles: int = 2,
) -> str:
    """When parsed fix output looks truncated (content-level), attempt continuation.

    Treats raw_text as partial response and asks the model to continue. Callers
    should re-parse the returned content with parse_fix_output and re-check
    looks_like_truncated_file_content; only write to disk if not truncated.

    If the LLM client does not expose base_url/model/timeout (e.g. test double),
    returns raw_text unchanged and logs that continuation was skipped.

    Returns:
        Merged response content (raw_text + continuation chunks), or raw_text if
        continuation was skipped.
    """
    base_url = getattr(llm, "base_url", None)
    model = getattr(llm, "model", None)
    timeout = getattr(llm, "timeout", 60)
    if base_url is None or model is None:
        logger.warning(
            "%s: Fix output continuation skipped (llm lacks base_url/model).",
            agent_name,
        )
        return raw_text
    from software_engineering_team.shared.continuation import ResponseContinuator

    continuator = ResponseContinuator(
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_cycles=max_cycles,
    )
    result = continuator.attempt_continuation(
        original_prompt=prompt,
        partial_content=raw_text,
        json_mode=False,
        task_id=agent_name,
    )
    return result.content


def complete_with_continuation(
    llm: "LLMClient",
    prompt: str,
    *,
    agent_name: str = "PlanningV2",
    max_continuation_cycles: int = MAX_CONTINUATION_CYCLES,
    mode: str = "text",
    decompose_fn: Optional[Callable[[str], List[str]]] = None,
    merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    original_content: Optional[str] = None,
    chunk_prompt_template: Optional[str] = None,
) -> str:
    """Make an LLM call with truncation handling via continuation. Text only; no JSON.

    Use complete_text_with_continuation for new code. This wrapper is kept for
    backward compatibility and always returns the response as text. Parse with
    output_templates.
    """
    return complete_text_with_continuation(
        llm=llm,
        prompt=prompt,
        agent_name=agent_name,
        max_continuation_cycles=max_continuation_cycles,
    )


def parse_json_with_recovery(
    llm: "LLMClient",
    prompt: str,
    *,
    agent_name: str = "PlanningV2",
    decompose_fn: Optional[Callable[[str], List[str]]] = None,
    merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    original_content: Optional[str] = None,
    chunk_prompt_template: Optional[str] = None,
    on_chunk_progress: Optional[Callable[[int, int], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Call LLM for JSON with continuation; optionally decompose content into chunks and merge.

    If decompose_fn, merge_fn, original_content, and chunk_prompt_template are provided,
    decomposes original_content into chunks, gets JSON per chunk (with continuation),
    and merges with merge_fn. Otherwise calls the LLM once for the given prompt and
    returns parsed JSON or None on failure.
    """
    from llm_service import LLMJsonParseError, LLMTruncatedError
    from software_engineering_team.shared.llm import complete_json_with_continuation

    if (
        decompose_fn is not None
        and merge_fn is not None
        and original_content is not None
        and chunk_prompt_template is not None
    ):
        chunks = decompose_fn(original_content)
        if not chunks:
            try:
                return complete_json_with_continuation(llm, prompt, task_id=agent_name)
            except (LLMTruncatedError, LLMJsonParseError, Exception):
                return None
        results: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            if on_chunk_progress is not None:
                on_chunk_progress(i, len(chunks))
            chunk_prompt = chunk_prompt_template.format(chunk_content=chunk)
            try:
                data = complete_json_with_continuation(
                    llm,
                    chunk_prompt,
                    task_id=f"{agent_name}_chunk{i}",
                )
                if isinstance(data, dict):
                    results.append(data)
            except (LLMTruncatedError, LLMJsonParseError, Exception) as e:
                logger.warning(
                    "%s: Chunk %d JSON failed: %s",
                    agent_name,
                    i,
                    str(e)[:200],
                )
                return None
        return merge_fn(results) if results else None
    try:
        return complete_json_with_continuation(llm, prompt, task_id=agent_name)
    except (LLMTruncatedError, LLMJsonParseError, Exception):
        return None


def default_decompose_by_sections(content: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> List[str]:
    """Split content into sections by markdown headers or fixed-size chunks.

    First tries to split by ## headers. If that produces only one section,
    falls back to fixed-size chunking.
    """
    # Try splitting by ## headers
    sections = re.split(r"\n(?=## )", content)
    if len(sections) > 1:
        return [s.strip() for s in sections if s.strip()]

    # Try splitting by # headers
    sections = re.split(r"\n(?=# )", content)
    if len(sections) > 1:
        return [s.strip() for s in sections if s.strip()]

    # Fall back to fixed-size chunks
    chunks = []
    for i in range(0, len(content), chunk_size):
        chunk = content[i : i + chunk_size]
        if chunk.strip():
            chunks.append(chunk)
    return chunks if chunks else [content]


def default_merge_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge results by concatenating list fields and taking first scalar fields.

    This is a generic merge that works for most JSON structures where:
    - Lists should be concatenated
    - Scalars (strings, numbers, bools) take the first non-empty value
    - Nested dicts are merged recursively
    """
    if not results:
        return {}

    merged: Dict[str, Any] = {}

    for result in results:
        for key, value in result.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(value, list) and isinstance(merged[key], list):
                # Concatenate lists
                merged[key].extend(value)
                # Apply semantic dedup for string lists
                if merged[key] and all(isinstance(x, str) for x in merged[key]):
                    merged[key] = dedupe_strings(merged[key])
            elif isinstance(value, dict) and isinstance(merged[key], dict):
                # Recursively merge dicts
                for k, v in value.items():
                    if k not in merged[key]:
                        merged[key][k] = v
                    elif isinstance(v, list) and isinstance(merged[key][k], list):
                        merged[key][k].extend(v)
                        if merged[key][k] and all(
                            isinstance(x, str) for x in merged[key][k]
                        ):
                            merged[key][k] = dedupe_strings(merged[key][k])
            # For scalars, keep the first non-empty value
            elif not merged[key] and value:
                merged[key] = value

    return merged
