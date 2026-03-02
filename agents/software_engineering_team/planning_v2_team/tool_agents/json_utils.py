"""Shared text completion and truncation handling for planning_v2_team tool agents.

Uses text-only LLM calls with continuation on truncation. No JSON is required;
consumers parse responses via output_templates (section markers).
"""

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Union, TYPE_CHECKING

from shared.deduplication import dedupe_strings

if TYPE_CHECKING:
    from shared.llm import LLMClient

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
    """Call LLM with text mode; on truncation, continue until complete.

    Does not require or parse JSON. Callers should use output_templates to parse
    the returned text. Matches the truncated-response approach used by backend_code_v2
    and frontend_code_v2 teams.

    Returns:
        Full response text (possibly concatenated from continuation cycles).

    Raises:
        RuntimeError: If continuation is exhausted and response is still truncated.
    """
    from shared.llm import LLMTruncatedError
    from shared.continuation import ResponseContinuator
    from shared.post_mortem import write_post_mortem

    try:
        return llm.complete_text(prompt)
    except LLMTruncatedError as e:
        logger.info(
            "%s: Response truncated (%d chars). Starting continuation loop...",
            agent_name,
            len(e.partial_content),
        )
        continuator = ResponseContinuator(
            base_url=llm.base_url,
            model=llm.model,
            timeout=llm.timeout,
            max_cycles=max_continuation_cycles,
        )
        result = continuator.attempt_continuation(
            original_prompt=prompt,
            partial_content=e.partial_content,
            json_mode=False,
            task_id=agent_name,
        )
        if result.success:
            logger.info(
                "%s: Continuation successful after %d cycles (%d chars total)",
                agent_name,
                result.cycles_used,
                len(result.content),
            )
            return result.content
        logger.warning(
            "%s: Continuation exhausted after %d cycles (%d chars accumulated).",
            agent_name,
            result.cycles_used,
            len(result.content),
        )
        write_post_mortem(
            agent_name=agent_name,
            task_description=f"Planning V2 - {agent_name}",
            original_prompt=prompt,
            partial_responses=result.partial_responses,
            continuation_attempts=result.cycles_used,
            decomposition_depth=0,
            error=e,
        )
        raise RuntimeError(
            f"{agent_name}: Response truncated and continuation exhausted after {result.cycles_used} cycles. "
            "See post_mortems/POST_MORTEMS.md for details."
        ) from e


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
