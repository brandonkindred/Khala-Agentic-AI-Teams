"""Shared JSON parsing utilities for planning_v2_team tool agents."""

import logging
import re
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_DECOMPOSITION_DEPTH = 20
DEFAULT_CHUNK_SIZE = 4000


def parse_json_with_recovery(
    llm: "LLMClient",
    prompt: str,
    agent_name: str = "ToolAgent",
    decompose_fn: Optional[Callable[[str], List[str]]] = None,
    merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    original_content: Optional[str] = None,
    chunk_prompt_template: Optional[str] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    """Parse LLM JSON response with immediate decomposition on errors.

    When truncation or JSON parse errors are detected, immediately decomposes
    the task into smaller chunks rather than retrying. Only transient network
    errors trigger retries.

    Args:
        llm: LLM client for completions
        prompt: The prompt to send to the LLM
        agent_name: Name for logging purposes
        decompose_fn: Optional function to split content into chunks
        merge_fn: Optional function to merge results from chunks
        original_content: The original content being processed (for decomposition)
        chunk_prompt_template: Template for chunk prompts (must have {chunk_content})
        _depth: Internal recursion depth tracker

    Returns:
        Parsed JSON dict.

    Raises:
        RuntimeError: If decomposition fails after max depth reached.
    """
    from shared.llm import LLMTruncatedError, LLMJsonParseError

    try:
        return llm.complete_json(prompt)
    except (LLMTruncatedError, LLMJsonParseError) as e:
        error_type = type(e).__name__
        if isinstance(e, LLMTruncatedError):
            logger.warning(
                "%s: %s (%d chars partial). Next step -> Decomposing task (depth %d/%d)",
                agent_name,
                error_type,
                len(e.partial_content),
                _depth + 1,
                MAX_DECOMPOSITION_DEPTH,
            )
        else:
            logger.warning(
                "%s: %s detected. Next step -> Decomposing task (depth %d/%d)",
                agent_name,
                error_type,
                _depth + 1,
                MAX_DECOMPOSITION_DEPTH,
            )

        result = _decompose_and_process(
            llm=llm,
            prompt=prompt,
            agent_name=agent_name,
            decompose_fn=decompose_fn,
            merge_fn=merge_fn,
            original_content=original_content,
            chunk_prompt_template=chunk_prompt_template,
            _depth=_depth,
        )
        if result:
            return result

        raise RuntimeError(
            f"{agent_name}: Decomposition exhausted at depth {_depth}/{MAX_DECOMPOSITION_DEPTH}. "
            f"Original error: {e}"
        ) from e


def _decompose_and_process(
    llm: "LLMClient",
    prompt: str,
    agent_name: str,
    decompose_fn: Optional[Callable[[str], List[str]]],
    merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]],
    original_content: Optional[str],
    chunk_prompt_template: Optional[str],
    _depth: int,
) -> Dict[str, Any]:
    """Decompose content into chunks and process each recursively.

    Raises:
        RuntimeError: If max decomposition depth is reached.
    """
    if decompose_fn is None or merge_fn is None:
        logger.warning(
            "%s: Decomposition not available (no decompose/merge functions)",
            agent_name,
        )
        return {}

    if not original_content:
        logger.warning(
            "%s: Decomposition not possible (no content to decompose)",
            agent_name,
        )
        return {}

    if _depth >= MAX_DECOMPOSITION_DEPTH:
        raise RuntimeError(
            f"{agent_name}: Maximum decomposition depth ({MAX_DECOMPOSITION_DEPTH}) "
            "reached without successful response"
        )

    chunks = decompose_fn(original_content)
    if len(chunks) <= 1:
        logger.warning(
            "%s: Cannot decompose further (only %d chunk)",
            agent_name,
            len(chunks),
        )
        return {}

    logger.info(
        "%s: Decomposing into %d chunks (depth %d/%d)",
        agent_name,
        len(chunks),
        _depth + 1,
        MAX_DECOMPOSITION_DEPTH,
    )

    results: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        logger.debug(
            "%s: Processing chunk %d/%d (%d chars)",
            agent_name,
            i + 1,
            len(chunks),
            len(chunk),
        )

        if chunk_prompt_template:
            chunk_prompt = chunk_prompt_template.format(chunk_content=chunk)
        else:
            chunk_prompt = _create_generic_chunk_prompt(prompt, chunk)

        try:
            chunk_result = parse_json_with_recovery(
                llm=llm,
                prompt=chunk_prompt,
                agent_name=f"{agent_name}_chunk{i + 1}",
                decompose_fn=decompose_fn,
                merge_fn=merge_fn,
                original_content=chunk,
                chunk_prompt_template=chunk_prompt_template,
                _depth=_depth + 1,
            )
            if chunk_result:
                results.append(chunk_result)
        except RuntimeError as e:
            logger.warning(
                "%s: Chunk %d/%d failed: %s",
                agent_name,
                i + 1,
                len(chunks),
                str(e)[:100],
            )

    if results:
        merged = merge_fn(results)
        logger.info(
            "%s: Merged %d chunk results successfully",
            agent_name,
            len(results),
        )
        return merged

    logger.warning("%s: Chunk processing produced no valid results", agent_name)
    return {}


def _create_generic_chunk_prompt(original_prompt: str, chunk: str) -> str:
    """Create a generic chunk prompt based on the original prompt."""
    # Extract the JSON schema hint from the original prompt if present
    schema_match = re.search(r"(\{[^}]*\"[^\"]+\"[^}]*\})", original_prompt)
    schema_hint = schema_match.group(1) if schema_match else ""

    return f"""Process this portion of the content and return JSON with the same structure.

CONTENT CHUNK:
---
{chunk}
---

{f"Expected JSON structure: {schema_hint}" if schema_hint else "Return JSON with the relevant fields found in this chunk."}

Keep your response concise. Only include findings from THIS chunk.
"""


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
            elif isinstance(value, dict) and isinstance(merged[key], dict):
                # Recursively merge dicts
                for k, v in value.items():
                    if k not in merged[key]:
                        merged[key][k] = v
                    elif isinstance(v, list) and isinstance(merged[key][k], list):
                        merged[key][k].extend(v)
            # For scalars, keep the first non-empty value
            elif not merged[key] and value:
                merged[key] = value

    return merged
