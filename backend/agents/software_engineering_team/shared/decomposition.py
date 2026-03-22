"""Unified decomposition framework for handling truncated LLM responses.

This module provides a generic mechanism for recursively decomposing tasks
when LLM responses are truncated due to token limits. Instead of attempting
partial recovery (e.g., repairing truncated JSON), it decomposes the task
into smaller pieces that can be processed without truncation.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Generic, List, Optional, TypeVar

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_MAX_DECOMPOSITION_DEPTH = 20
DEFAULT_CHUNK_SIZE = 4000

T = TypeVar("T")


@dataclass
class DecompositionContext:
    """Tracks the state of recursive decomposition.

    Attributes:
        original_task: Description of the original task being processed.
        original_content: The original content that triggered decomposition.
        depth: Current recursion depth (0 = root).
        max_depth: Maximum allowed recursion depth.
        parent_context: Reference to parent context for nested decomposition.
        chunks_processed: Number of chunks processed so far.
        total_chunks: Total number of chunks in current decomposition.
        decomposition_reason: Why decomposition was triggered (e.g., "truncated").
        continuation_attempted: Whether continuation was attempted before decomposition.
        partial_responses: List of partial responses collected for post-mortem.
    """

    original_task: str
    original_content: str = ""
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH
    parent_context: Optional["DecompositionContext"] = None
    chunks_processed: int = 0
    total_chunks: int = 0
    decomposition_reason: str = "truncated"
    continuation_attempted: bool = False
    _decomposition_history: List[str] = field(default_factory=list)
    _partial_responses: List[str] = field(default_factory=list)

    def create_child(self, chunk_index: int, total_chunks: int) -> "DecompositionContext":
        """Create a child context for processing a chunk."""
        child = DecompositionContext(
            original_task=self.original_task,
            original_content=self.original_content,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            parent_context=self,
            chunks_processed=0,
            total_chunks=0,
            decomposition_reason=self.decomposition_reason,
            continuation_attempted=self.continuation_attempted,
            _decomposition_history=self._decomposition_history.copy(),
            _partial_responses=self._partial_responses,
        )
        child._decomposition_history.append(f"depth_{self.depth}_chunk_{chunk_index + 1}_of_{total_chunks}")
        return child

    def add_partial_response(self, content: str) -> None:
        """Add a partial response to the tracking list."""
        self._partial_responses.append(content)

    def mark_continuation_attempted(self) -> None:
        """Mark that continuation was attempted."""
        self.continuation_attempted = True

    def can_decompose(self) -> bool:
        """Check if further decomposition is allowed."""
        return self.depth < self.max_depth

    def get_decomposition_path(self) -> str:
        """Return a string describing the decomposition path."""
        if not self._decomposition_history:
            return "root"
        return " -> ".join(self._decomposition_history)

    def log_decomposition(self, agent_name: str, num_chunks: int) -> None:
        """Log the decomposition event."""
        logger.info(
            "%s: Decomposing content (depth %d/%d) into %d chunks. Path: %s",
            agent_name,
            self.depth + 1,
            self.max_depth,
            num_chunks,
            self.get_decomposition_path(),
        )


class DecompositionStrategy(ABC, Generic[T]):
    """Abstract base class for decomposition strategies.

    Subclasses define how to split content into smaller pieces
    and how to merge results from those pieces.
    """

    @abstractmethod
    def decompose(self, content: str, context: DecompositionContext) -> List[str]:
        """Split content into smaller pieces.

        Args:
            content: The content to decompose.
            context: Current decomposition context.

        Returns:
            List of content chunks. Should return at least 2 chunks,
            or the original content if it cannot be decomposed further.
        """

    @abstractmethod
    def merge(self, results: List[T]) -> T:
        """Merge results from processed chunks.

        Args:
            results: List of results from each chunk.

        Returns:
            Merged result.
        """

    def create_chunk_prompt(
        self,
        original_prompt: str,
        chunk: str,
        chunk_index: int,
        total_chunks: int,
    ) -> str:
        """Create a prompt for processing a single chunk.

        Override this method to customize chunk prompts.

        Args:
            original_prompt: The original prompt that caused truncation.
            chunk: The chunk content to process.
            chunk_index: Index of this chunk (0-based).
            total_chunks: Total number of chunks.

        Returns:
            Prompt for processing this chunk.
        """
        return f"""Process this portion of the content (chunk {chunk_index + 1} of {total_chunks}).

CONTENT CHUNK:
---
{chunk}
---

Return your response in the same format as the original request.
Keep your response concise. Only include findings from THIS chunk.
"""


class SectionDecompositionStrategy(DecompositionStrategy[Dict[str, Any]]):
    """Decompose content by markdown sections, falling back to fixed-size chunks.

    This is the default strategy for processing structured documents.
    """

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.chunk_size = chunk_size

    def decompose(self, content: str, context: DecompositionContext) -> List[str]:
        """Split by markdown headers, then by size if needed."""
        if not content or not content.strip():
            return [content] if content else []

        # Try splitting by ## headers first (most specific)
        sections = re.split(r"\n(?=## )", content)
        if len(sections) > 1:
            return [s.strip() for s in sections if s.strip()]

        # Try splitting by # headers
        sections = re.split(r"\n(?=# )", content)
        if len(sections) > 1:
            return [s.strip() for s in sections if s.strip()]

        # Fall back to fixed-size chunks
        return self._chunk_by_size(content)

    def _chunk_by_size(self, content: str) -> List[str]:
        """Split content into fixed-size chunks."""
        chunks = []
        for i in range(0, len(content), self.chunk_size):
            chunk = content[i : i + self.chunk_size]
            if chunk.strip():
                chunks.append(chunk)
        return chunks if chunks else [content]

    def merge(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge dictionaries by concatenating lists and merging nested dicts."""
        if not results:
            return {}

        merged: Dict[str, Any] = {}

        for result in results:
            if not isinstance(result, dict):
                continue
            for key, value in result.items():
                if key not in merged:
                    merged[key] = value
                elif isinstance(value, list) and isinstance(merged[key], list):
                    merged[key].extend(value)
                elif isinstance(value, dict) and isinstance(merged[key], dict):
                    self._merge_dicts(merged[key], value)
                elif not merged[key] and value:
                    merged[key] = value

        return merged

    def _merge_dicts(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:
        """Recursively merge source dict into target dict."""
        for key, value in source.items():
            if key not in target:
                target[key] = value
            elif isinstance(value, list) and isinstance(target[key], list):
                target[key].extend(value)
            elif isinstance(value, dict) and isinstance(target[key], dict):
                self._merge_dicts(target[key], value)


class FileBasedDecompositionStrategy(DecompositionStrategy[Dict[str, str]]):
    """Decompose file generation tasks into per-file requests.

    This strategy is useful when generating multiple files, splitting
    the task so each file is generated in a separate request.
    """

    def decompose(self, content: str, context: DecompositionContext) -> List[str]:
        """Split task into per-file descriptions.

        Looks for file paths in the content and creates separate tasks.
        """
        # Look for file paths in various formats
        file_patterns = [
            r"(?:^|\n)[-*]\s*[`']?([^`'\n]+\.[a-zA-Z]+)[`']?",  # - file.ext or * file.ext
            r"(?:^|\n)(\d+\.)\s*[`']?([^`'\n]+\.[a-zA-Z]+)[`']?",  # 1. file.ext
            r"[`']([^`'\s]+\.[a-zA-Z]+)[`']",  # `file.ext` or 'file.ext'
        ]

        files = set()
        for pattern in file_patterns:
            matches = re.findall(pattern, content)
            for match in matches:
                if isinstance(match, tuple):
                    file_path = match[-1]
                else:
                    file_path = match
                if file_path and "/" in file_path or "." in file_path:
                    files.add(file_path)

        if len(files) > 1:
            return [f"Generate file: {f}\n\n{content}" for f in sorted(files)]

        # Cannot decompose by files; fall back to content sections
        return SectionDecompositionStrategy().decompose(content, context)

    def merge(self, results: List[Dict[str, str]]) -> Dict[str, str]:
        """Merge file dictionaries."""
        merged: Dict[str, str] = {}
        for result in results:
            if isinstance(result, dict):
                merged.update(result)
        return merged


class RecursiveProcessor(Generic[T]):
    """Processes content with automatic decomposition on truncation.

    This class wraps LLM calls and automatically decomposes tasks when
    truncation is detected (via LLMTruncatedError).
    """

    def __init__(
        self,
        strategy: DecompositionStrategy[T],
        max_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
    ):
        self.strategy = strategy
        self.max_depth = max_depth

    def process(
        self,
        llm: "LLMClient",
        prompt: str,
        content: str,
        agent_name: str = "Agent",
        process_fn: Optional[Callable[[str], T]] = None,
        context: Optional[DecompositionContext] = None,
    ) -> T:
        """Process content with recovery on truncation.

        Truncation is handled by the LLM client (continuation in llm_service).
        If the client still raises LLMTruncatedError after its continuation:
        1. Decompose task into smaller chunks (up to max_depth)
        2. If decomposition fails: Write post-mortem and raise error

        Args:
            llm: LLM client for making requests.
            prompt: The prompt to send to the LLM.
            content: The content being processed (used for decomposition).
            agent_name: Name for logging purposes.
            process_fn: Optional custom processing function. If not provided,
                       uses llm.complete_json.
            context: Existing decomposition context (for recursive calls).

        Returns:
            Processed result, potentially merged from multiple chunks.

        Raises:
            LLMTruncatedError: If max decomposition depth is exceeded and
                              response is still truncated.
        """
        from llm_service import LLMTruncatedError
        from software_engineering_team.shared.continuation import MAX_CONTINUATION_CYCLES
        from software_engineering_team.shared.post_mortem import write_post_mortem

        if context is None:
            context = DecompositionContext(
                original_task=prompt[:200],
                original_content=content,
                max_depth=self.max_depth,
            )

        try:
            if process_fn:
                return process_fn(prompt)
            return llm.complete_json(prompt)
        except LLMTruncatedError as e:
            context.add_partial_response(e.partial_content)
            if not context.continuation_attempted:
                context.mark_continuation_attempted()
            logger.warning(
                "%s: Response truncated (%d chars). Client already attempted continuation; decomposing task",
                agent_name,
                len(e.partial_content),
            )

            if not context.can_decompose():
                logger.error(
                    "%s: Max decomposition depth (%d) reached. Cannot decompose further. "
                    "Path: %s",
                    agent_name,
                    self.max_depth,
                    context.get_decomposition_path(),
                )

                write_post_mortem(
                    agent_name=agent_name,
                    task_description=context.original_task,
                    original_prompt=prompt,
                    partial_responses=context._partial_responses,
                    continuation_attempts=MAX_CONTINUATION_CYCLES,
                    decomposition_depth=context.depth,
                    error=e,
                )

                raise

            return self._decompose_and_process(
                llm, prompt, content, agent_name, process_fn, context
            )

    def _decompose_and_process(
        self,
        llm: "LLMClient",
        original_prompt: str,
        content: str,
        agent_name: str,
        process_fn: Optional[Callable[[str], T]],
        context: DecompositionContext,
    ) -> T:
        """Decompose content and process each chunk."""
        chunks = self.strategy.decompose(content, context)

        if len(chunks) <= 1:
            logger.warning(
                "%s: Cannot decompose further (only %d chunk). Returning empty result.",
                agent_name,
                len(chunks),
            )
            return self.strategy.merge([])

        context.log_decomposition(agent_name, len(chunks))
        context.total_chunks = len(chunks)

        results: List[T] = []
        for i, chunk in enumerate(chunks):
            context.chunks_processed = i + 1
            child_context = context.create_child(i, len(chunks))

            logger.debug(
                "%s: Processing chunk %d/%d (%d chars)",
                agent_name,
                i + 1,
                len(chunks),
                len(chunk),
            )

            chunk_prompt = self.strategy.create_chunk_prompt(
                original_prompt, chunk, i, len(chunks)
            )

            try:
                chunk_result = self.process(
                    llm,
                    chunk_prompt,
                    chunk,
                    f"{agent_name}_chunk{i + 1}",
                    process_fn,
                    child_context,
                )
                if chunk_result:
                    results.append(chunk_result)
            except Exception as e:
                logger.warning(
                    "%s: Chunk %d/%d failed: %s. Continuing with remaining chunks.",
                    agent_name,
                    i + 1,
                    len(chunks),
                    str(e)[:100],
                )

        if results:
            merged = self.strategy.merge(results)
            logger.info(
                "%s: Successfully merged %d/%d chunk results",
                agent_name,
                len(results),
                len(chunks),
            )
            return merged

        logger.warning("%s: All chunks failed. Returning empty result.", agent_name)
        return self.strategy.merge([])


def process_with_decomposition(
    llm: "LLMClient",
    prompt: str,
    content: str,
    agent_name: str = "Agent",
    strategy: Optional[DecompositionStrategy] = None,
    max_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
) -> Dict[str, Any]:
    """Convenience function for processing with decomposition.

    This is a simple wrapper around RecursiveProcessor for common use cases.

    Args:
        llm: LLM client for making requests.
        prompt: The prompt to send to the LLM.
        content: The content being processed.
        agent_name: Name for logging.
        strategy: Decomposition strategy (defaults to SectionDecompositionStrategy).
        max_depth: Maximum recursion depth.

    Returns:
        Processed result as a dictionary.
    """
    if strategy is None:
        strategy = SectionDecompositionStrategy()

    processor: RecursiveProcessor[Dict[str, Any]] = RecursiveProcessor(
        strategy=strategy,
        max_depth=max_depth,
    )

    return processor.process(llm, prompt, content, agent_name)
