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

    Invariants:
        * `depth` only increases across a `create_child` chain and never
          decreases; `can_decompose()` is the sole gate on how deep that
          chain may go (`depth < max_depth`).
        * `_partial_responses` is shared by reference with every child
          created via `create_child`, so appending to it via
          `add_partial_response` on any node in the chain is visible to
          all others; `_decomposition_history` is copied per child instead,
          so each node's history reflects only its own path from the root.
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
        """Create a child context for processing a chunk.

        Preconditions:
            * `chunk_index` is the 0-based index of the chunk being
              processed and `total_chunks` is the total chunk count for
              this decomposition; both are used only for the history
              label, so out-of-range values are not validated.
        Postconditions:
            * Returns a new `DecompositionContext` with `depth` one greater
              than `self.depth`, `parent_context` set to `self`, and
              `chunks_processed`/`total_chunks` reset to 0 (the child
              tracks its own sub-decomposition, if any).
            * The child's `_partial_responses` is the SAME list object as
              `self._partial_responses` (shared, not copied); its
              `_decomposition_history` is a shallow copy of `self`'s with
              one new entry appended describing this chunk's position.
            * Does not mutate `self` or `total_chunks`/`chunks_processed`
              on `self`.
        """
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
        child._decomposition_history.append(
            f"depth_{self.depth}_chunk_{chunk_index + 1}_of_{total_chunks}"
        )
        return child

    def add_partial_response(self, content: str) -> None:
        """Add a partial response to the tracking list.

        Preconditions:
            * None; `content` may be empty.
        Postconditions:
            * Appends `content` to `self._partial_responses` in place.
              Since that list is shared by reference across a
              `create_child` chain, the appended entry becomes visible
              through every context in the chain, not just `self`.
        """
        self._partial_responses.append(content)

    def mark_continuation_attempted(self) -> None:
        """Mark that continuation was attempted.

        Preconditions:
            * None.
        Postconditions:
            * Sets `self.continuation_attempted` to `True` unconditionally;
              idempotent if already `True`. Only affects `self`, not any
              parent or child context.
        """
        self.continuation_attempted = True

    def can_decompose(self) -> bool:
        """Check if further decomposition is allowed.

        Preconditions:
            * None.
        Postconditions:
            * Returns `self.depth < self.max_depth`. A context at
              `depth == max_depth` already cannot decompose further (the
              comparison is strict), so `max_depth` is an exclusive bound
              on reachable depth, not an inclusive one.
        """
        return self.depth < self.max_depth

    def get_decomposition_path(self) -> str:
        """Return a string describing the decomposition path.

        Preconditions:
            * None.
        Postconditions:
            * Returns the literal string `"root"` if
              `self._decomposition_history` is empty (i.e. this context has
              no recorded ancestry, whether or not `self.depth` is 0).
            * Otherwise returns the history entries joined with `" -> "`,
              in the order they were appended (oldest first).
        """
        if not self._decomposition_history:
            return "root"
        return " -> ".join(self._decomposition_history)

    def log_decomposition(self, agent_name: str, num_chunks: int) -> None:
        """Log the decomposition event.

        Preconditions:
            * `agent_name` identifies the caller for the log line;
              `num_chunks` is the chunk count about to be processed.
              Neither is validated (e.g. `num_chunks` may be 0 or
              negative; it is only interpolated into the message).
        Postconditions:
            * Emits exactly one `logger.info` call describing this
              context's `depth + 1` (1-based, out of `max_depth`),
              `num_chunks`, and `get_decomposition_path()`. Does not
              mutate `self` or raise on logging failure beyond whatever
              the standard `logging` module itself may raise.
        """
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

    Invariants:
        * Implementations are expected to be stateless with respect to a
          given `decompose`/`merge` call pair (concrete subclasses in this
          module hold only immutable configuration, e.g. `chunk_size`).
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

        Preconditions:
            * `content` may be empty; `context` is the caller's current
              decomposition context (not required to be inspected by
              every implementation).
        Postconditions:
            * Implementations must not mutate `content` or `context`.
            * A returned list of length <= 1 is the contract's signal
              that content could not be decomposed further; callers
              (e.g. `RecursiveProcessor._decompose_and_process`) rely on
              this to stop recursing rather than looping forever.
        """

    @abstractmethod
    def merge(self, results: List[T]) -> T:
        """Merge results from processed chunks.

        Args:
            results: List of results from each chunk.

        Returns:
            Merged result.

        Preconditions:
            * `results` may be empty (e.g. when every chunk failed to
              process).
        Postconditions:
            * Must return a value of the same shape callers expect for a
              single unmerged result (concrete subclasses in this module
              return an empty `dict` for an empty `results`).
            * Must not mutate the elements of `results` in place from the
              caller's perspective beyond what merging into the returned
              value requires.
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

        Preconditions:
            * `chunk_index` is 0-based and `total_chunks` is the total
              chunk count for the current decomposition; neither is
              validated against the other (e.g. `chunk_index >=
              total_chunks` is not checked here).
        Postconditions:
            * Pure function (no side effects); returns a new prompt string
              embedding `chunk` verbatim plus the 1-based chunk position
              (`chunk_index + 1` of `total_chunks`). Does not modify
              `original_prompt` or `chunk`.
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
        """Split by markdown headers, then by size if needed.

        Preconditions:
            * `content` may be empty or whitespace-only; `context` is
              accepted per the base class signature but unused here.
        Postconditions:
            * Returns `[]` if `content` is falsy (e.g. `""`).
            * Returns `[content]` unchanged (not stripped) if `content` is
              truthy but whitespace-only.
            * Otherwise tries, in order: splitting on `\\n## ` boundaries,
              then `\\n# ` boundaries (each stripped of surrounding
              whitespace, empty pieces dropped); if either split yields
              more than 1 section, that result is returned immediately.
            * If no header split applies, falls back to
              `_chunk_by_size(content)`.
        """
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
        """Split content into fixed-size chunks.

        Preconditions:
            * `content` is non-empty (callers only reach this after the
              emptiness/whitespace-only checks in `decompose`).
        Postconditions:
            * Splits `content` into consecutive slices of `self.chunk_size`
              characters (last slice may be shorter); slices that are
              whitespace-only after stripping are dropped from the result,
              though the returned chunks themselves are NOT stripped.
            * Returns `[content]` (the whole, unmodified string) if every
              slice was whitespace-only, so the result is never empty for
              non-empty input.
        """
        chunks = []
        for i in range(0, len(content), self.chunk_size):
            chunk = content[i : i + self.chunk_size]
            if chunk.strip():
                chunks.append(chunk)
        return chunks if chunks else [content]

    def merge(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge dictionaries by concatenating lists and merging nested dicts.

        Preconditions:
            * `results` may be empty. Non-dict elements are tolerated (see
              postconditions) rather than rejected.
        Postconditions:
            * Returns `{}` if `results` is empty.
            * Non-dict elements of `results` are silently skipped.
            * For each key across all dict elements, in first-seen order:
              if the key is new, its value is taken as-is (by reference,
              not copied); if both the existing and new values are lists,
              the new list's items are appended (`extend`, not replace);
              if both are dicts, they are merged recursively via
              `_merge_dicts`; otherwise, the new value replaces the
              existing one only if the existing value is falsy and the
              new one is truthy (first non-empty value wins, not
              last-write-wins).
        """
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
        """Recursively merge source dict into target dict.

        Preconditions:
            * `target` and `source` are both `dict`; either may be empty.
        Postconditions:
            * Mutates `target` in place; returns `None`.
            * For each key in `source`: if absent from `target`, it is
              added (value taken by reference); if both values are lists,
              `target`'s list is extended with `source`'s items; if both
              are dicts, merges recursively. Unlike `merge`, a key present
              in `target` whose value is neither a matching list nor a
              matching dict is left UNCHANGED (no falsy/truthy fallback
              here) — `source`'s value for that key is silently dropped.
        """
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

        Preconditions:
            * `content` may be empty or contain no recognizable file
              paths; `context` is passed through to the fallback
              strategy unused otherwise.
        Postconditions:
            * Scans `content` with three regex patterns (list-item paths,
              numbered-list paths, and backtick/quoted paths), collecting
              matches into a `set` (so duplicates across patterns collapse
              and result order is by sorted filename, not first-seen
              order).
            * A matched candidate is kept only if it contains `/` or `.`
              (`"/" in file_path or "." in file_path` — note this is `(a
              and b) or c` by operator precedence, i.e. `file_path and
              ("/" in file_path) or ("." in file_path)`, so any truthy
              `file_path` containing `.` is kept regardless of `/`).
            * If more than 1 distinct file is found, returns one prompt
              per file (`"Generate file: {f}\\n\\n{content}"`), sorted by
              filename; each embeds the FULL original `content`, not a
              per-file excerpt.
            * If 0 or 1 files are found, falls back to
              `SectionDecompositionStrategy().decompose(content, context)`
              instead (a fresh instance with the default chunk size,
              independent of any strategy the caller is using).
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
        """Merge file dictionaries.

        Preconditions:
            * `results` may be empty. Non-dict elements are tolerated
              rather than rejected.
        Postconditions:
            * Returns `{}` if `results` is empty.
            * Non-dict elements are silently skipped.
            * Merges via `dict.update`, in list order: for a key
              appearing in more than one result, the LAST result's value
              wins (last-write-wins), unlike
              `SectionDecompositionStrategy.merge`'s first-non-empty-wins
              rule.
        """
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
        """Initialize the processor with a decomposition strategy.

        Preconditions:
            * `strategy` is expected to be a usable
              `DecompositionStrategy` instance; not validated here.
            * `max_depth` is expected to be `>= 0`; a non-positive value is
              accepted without error but means any `DecompositionContext`
              created with it (depth starts at 0) will immediately fail
              `can_decompose()`.
        Postconditions:
            * `self.strategy` and `self.max_depth` are set exactly to the
              given arguments (no copying, no validation, no defaulting
              beyond the parameter default).
        """
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
                       resolves ``llm`` via ``resolve_strands_model`` and runs
                       a one-shot Strands agent on ``prompt``; the agent's raw
                       response is then parsed as JSON via
                       ``extract_json_from_response``.
            context: Existing decomposition context (for recursive calls).

        Returns:
            Processed result, potentially merged from multiple chunks.

        Raises:
            LLMTruncatedError: If max decomposition depth is exceeded and
                              response is still truncated.
            LLMJsonParseError: If ``process_fn`` is not provided and the
                              default LLM call's response cannot be parsed as
                              JSON at all (via ``extract_json_from_response``).

        Preconditions:
            * `llm` must be usable by `process_fn` if provided, or by
              `resolve_strands_model` otherwise; not validated here.
            * `context`, if provided, is assumed to belong to the same
              decomposition tree as prior recursive calls (this method
              does not verify `context.original_task`/`original_content`
              match `prompt`/`content`); if omitted, a fresh root context
              (`depth=0`) is created from `prompt`, `content`, and
              `self.max_depth`.
        Postconditions:
            * On success (no truncation), returns the raw result of
              `process_fn(prompt)` if given, or the result of parsing a
              one-shot Strands agent's response as JSON via
              `extract_json_from_response`.
            * On `LLMTruncatedError`: records the partial content on
              `context`, marks continuation as attempted (idempotent),
              and logs a warning. If `context.can_decompose()` is `False`,
              writes a post-mortem file and RE-RAISES the original
              exception. Otherwise, delegates to
              `_decompose_and_process` and returns its (merged) result.
            * Does not mutate `prompt` or `content`; may mutate the
              (possibly caller-supplied) `context` object as described
              above.
        """
        from llm_service import LLMTruncatedError
        from software_engineering_team.shared.continuation import MAX_CONTINUATION_CYCLES
        from software_engineering_team.shared.post_mortem import write_post_mortem

        if context is None:
            context = DecompositionContext(
                original_task=prompt,
                original_content=content,
                max_depth=self.max_depth,
            )

        try:
            if process_fn:
                return process_fn(prompt)
            from strands import Agent as _Agent

            from llm_service.strands_model import resolve_strands_model
            from software_engineering_team.shared.llm import extract_json_from_response

            _agent = _Agent(model=resolve_strands_model(llm))
            _result = _agent(prompt)
            _raw = str(_result).strip()
            return extract_json_from_response(_raw)
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
                    "%s: Max decomposition depth (%d) reached. Cannot decompose further. Path: %s",
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
        """Decompose content and process each chunk.

        Preconditions:
            * `context.can_decompose()` is assumed `True` (the caller,
              `process`, only reaches this method after checking that);
              not re-checked here.
        Postconditions:
            * If `self.strategy.decompose(content, context)` yields <= 1
              chunk, logs a warning and returns `self.strategy.merge([])`
              — an EMPTY merge result, NOT the single chunk's content and
              NOT a raised error. This is the base case that prevents
              infinite recursion when content cannot be split further.
            * Otherwise, updates `context.total_chunks` and, for each
              chunk in order: creates a child context via
              `context.create_child`, builds a chunk prompt via
              `self.strategy.create_chunk_prompt`, and recursively calls
              `self.process(...)` with that chunk's own prompt/content/
              child context. A chunk whose processing raises any
              `Exception` is logged and skipped (not retried, not
              propagated) — remaining chunks still run.
            * `context.chunks_processed` is updated (1-based) before each
              chunk is dispatched, reflecting the LAST chunk started, not
              necessarily the last chunk completed.
            * Returns `self.strategy.merge(results)` over whatever chunk
              results succeeded, even if some chunks failed; only returns
              `self.strategy.merge([])` (empty) if ALL chunks failed.
        """
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

            chunk_prompt = self.strategy.create_chunk_prompt(original_prompt, chunk, i, len(chunks))

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
                    str(e),
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

    Preconditions:
        * Same as `RecursiveProcessor.process` (with `context=None`,
          `process_fn=None`), since this is a thin passthrough that
          always starts a fresh root context.
    Postconditions:
        * Constructs a new `RecursiveProcessor` with `strategy` (defaulting
          to a fresh `SectionDecompositionStrategy()` if `strategy` is
          `None`) and `max_depth`, then returns
          `processor.process(llm, prompt, content, agent_name)` — i.e. the
          same result/exception behavior as `RecursiveProcessor.process`.
    """
    if strategy is None:
        strategy = SectionDecompositionStrategy()

    processor: RecursiveProcessor[Dict[str, Any]] = RecursiveProcessor(
        strategy=strategy,
        max_depth=max_depth,
    )

    return processor.process(llm, prompt, content, agent_name)
