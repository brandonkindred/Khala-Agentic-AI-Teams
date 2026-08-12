"""Shared runner for once-per-submission additive code-review passes.

The additive passes (`architecture_consistency_pass.py`,
`side_effect_impact_pass.py`, `merged_architecture_side_effect_pass.py`) each
run one or more `strands.Agent` calls over the whole changed-file set, on top
of the map-phase chunk review. All three construct those calls only through
this module: think-then-format ``Agent`` invocation and reactive overflow
recovery (bisecting the file list when a call overflows the model's context)
— so a pass only supplies its prompt/tools/parser.

This runner does not cap prompt, response, body, inline, or manifest size.
Callers must render full content; the only length backstop is the LLM
provider's context window (surfaced as an overflow-shaped error, then
recovered by sending fewer whole files per call). Future spend controls are
expected to be monetary, not character/token caps.

This module is self-contained and importable in isolation.

Invariants:

    - **Additive-only, fail-safe.** :func:`run_submission_pass` never raises
      from a per-batch failure. A batch that cannot be recovered contributes
      nothing to the result and is logged, not raised — the caller (an
      additive pass) keeps whatever other batches produced, exactly like the
      pre-runner fail-safe posture each pass already has.
    - **No character/token packing.** The full changed-file set is attempted
      in one call; reactive recovery only grows the call count for a batch
      that has already overflowed, and is depth-bounded so it can never
      recurse unboundedly.
    - **Callers own content, not mechanics.** The runner never builds a
      `CodebaseIndex`, never invents prompt text, and never validates parsed
      findings — those stay entirely with the calling pass via the
      `build_prompt`/`tools`/`parse` callbacks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Tuple, TypeVar, Union

from llm_service import LLMClient, LLMTruncatedError

from .model_resolution import resolve_code_review_model
from .via_reasoning import run_agent_via_reasoning

try:
    from strands.models.model import Model as _StrandsModel
except ImportError:  # pragma: no cover - strands is a required dependency
    _StrandsModel = object  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Depth cap for reactive file-group bisection: at most this many successive
# halvings of a batch before recovery gives up instead of continuing to
# split. Bounds worst-case call count per submission to O(2 ** depth) leaf
# batches; not env-overridable, unlike the map-phase bisector, since this
# operates at whole-file granularity where a handful of levels already
# isolates a single culprit file.
_MAX_BATCH_BISECT_DEPTH = 4

# Provider/API error text that means "input or completion ran out of room"
# even when the exception type is a generic 4xx wrapper rather than a Strands
# overflow class. Matched case-insensitively against the exception chain.
_OVERFLOW_MESSAGE_MARKERS: Tuple[str, ...] = (
    "context window",
    "context length",
    "context_length",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
    "input is too long",
    "input too long",
    "exceeds the context",
    "exceeds the maximum",
    "request too large",
)


def _strands_overflow_errors() -> Tuple[type, ...]:
    """Collect the Strands context-overflow exception types available here.

    Preconditions: none (import of ``strands.types.exceptions`` may fail).
    Postconditions: returns a (possibly empty) tuple of exception classes
        present on the installed ``strands-agents`` package, so this module
        still imports under the declared floor (``strands-agents>=1.35``)
        even if a symbol is renamed/removed in a future release.
    """
    try:
        from strands.types import exceptions as strands_exc
    except ImportError:  # pragma: no cover - strands is a required dependency
        return ()
    names = ("ContextWindowOverflowException", "MaxTokensReachedException")
    found: List[type] = []
    for name in names:
        cls = getattr(strands_exc, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


# Overflow-shaped failures that trigger reactive recovery (bisect)
# rather than an immediate skip. ``LLMTruncatedError`` (finish_reason
# "length") is included alongside the native Strands exceptions because the
# injected-``LLMClientModel`` test/production path can raise it for the same
# "ran out of room" condition a bare Strands model raises the other two for.
_OVERFLOW_ERRORS: Tuple[type, ...] = (LLMTruncatedError, *_strands_overflow_errors())


def _is_overflow_shaped(exc: BaseException) -> bool:
    """True when ``exc`` signals the call ran out of context/output room.

    Postconditions:
        - True for :data:`_OVERFLOW_ERRORS` instances.
        - True when any exception in the ``__cause__`` / ``__context__`` chain
          (bounded) has a message matching :data:`_OVERFLOW_MESSAGE_MARKERS`
          — covers provider 400 wrappers that are not typed as overflow.
        - False for other failures (malformed JSON, programming bugs, etc.).
    """
    if isinstance(exc, _OVERFLOW_ERRORS):
        return True
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            break
        text = str(current).lower()
        if any(marker in text for marker in _OVERFLOW_MESSAGE_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class FileBatch:
    """One batch of changed files for a single submission-pass call.

    Attributes:
        items: ``(path, content)`` pairs in this batch, a subset of the full
            changed-file set in submission order. Content is never truncated
            by the runner; callers must render each item in full.
        index: 1-based position of this batch among ``total``.
        total: Total batch count for this submission-pass invocation (always
            ``1`` on the initial attempt; recovery bisects keep the parent's
            index/total).
        is_partial: True when this batch is a reactive-recovery bisect child
            rather than the initial full-set batch. A partial batch's
            ``items`` never represents the full submission — ``build_prompt``
            must not claim "batch 1 of 1" means every changed file when a
            recovery split leaves only some files in ``items``.
    """

    items: List[Tuple[str, str]]
    index: int
    total: int
    is_partial: bool = False


def _call_agent(
    model: "Union[LLMClient, _StrandsModel]",
    reasoning_system_prompt: str,
    formatting_instructions: str,
    tools: list,
    prompt: str,
    parse: Callable[[str], T],
) -> T:
    """Run one think-then-format submission pass call and parse the JSON reply.

    Preconditions:
        - ``reasoning_system_prompt`` and ``formatting_instructions`` are non-empty.
        - ``prompt`` is the user message for the reasoning pass.

    Postconditions:
        - Returns ``parse``'s result from the formatting pass.
        - Tools are attached only to the reasoning pass (call 1).
        - Raises whatever ``run_agent_via_reasoning`` or ``parse`` raises —
          recovery is entirely the caller's concern.
    """
    return run_agent_via_reasoning(
        model=model,
        reasoning_prompt=prompt,
        reasoning_system_prompt=reasoning_system_prompt,
        formatting_instructions=formatting_instructions,
        parse=parse,
        tools=tools,
        reasoning_think=True,
    )


def _run_batch_with_recovery(
    model: "Union[LLMClient, _StrandsModel]",
    reasoning_system_prompt: str,
    formatting_instructions: str,
    tools: list,
    build_prompt: Callable[[FileBatch], str],
    parse: Callable[[str], T],
    batch: FileBatch,
    pass_label: str,
    *,
    depth: int,
) -> List[T]:
    """Run one batch call, recovering from an overflow-shaped failure; never raises.

    Preconditions: ``batch.items`` is non-empty.

    Postconditions:
        - Returns ``[result]`` on success.
        - Returns ``[]`` immediately (no retry) when the call raises a
          non-overflow-shaped exception — matches the pre-runner fail-safe
          posture (a bad batch must not discard other batches' findings).
        - On an overflow-shaped exception, recovers via
          :func:`_recover_from_overflow` (bisect while possible), returning
          whatever that recovers (possibly ``[]``). Content is never
          truncated as a recovery strategy.
    """
    try:
        prompt = build_prompt(batch)
        result = _call_agent(
            model,
            reasoning_system_prompt,
            formatting_instructions,
            tools,
            prompt,
            parse,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: recover or skip, never raise
        if not _is_overflow_shaped(exc):
            logger.warning(
                "%s: batch %s/%s failed (%s: %s); skipping",
                pass_label,
                batch.index,
                batch.total,
                type(exc).__name__,
                exc,
            )
            return []
        return _recover_from_overflow(
            model,
            reasoning_system_prompt,
            formatting_instructions,
            tools,
            build_prompt,
            parse,
            batch,
            pass_label,
            exc,
            depth=depth,
        )
    return [result]


def _recover_from_overflow(
    model: "Union[LLMClient, _StrandsModel]",
    reasoning_system_prompt: str,
    formatting_instructions: str,
    tools: list,
    build_prompt: Callable[[FileBatch], str],
    parse: Callable[[str], T],
    batch: FileBatch,
    pass_label: str,
    exc: BaseException,
    *,
    depth: int,
) -> List[T]:
    """Reactively recover an overflow-shaped batch failure; never raises.

    Preconditions: ``_is_overflow_shaped(exc)`` is True; ``batch.items`` is non-empty.

    Postconditions:
        - When ``batch.items`` has more than one file and ``depth`` is under
          :data:`_MAX_BATCH_BISECT_DEPTH`, bisects the file list in half and
          recurses into each half at ``depth + 1``, concatenating both halves'
          results in order (each half may recover independently). Each half's
          :attr:`FileBatch.is_partial` is True, since neither contains all of
          ``batch.items``.
        - Otherwise (a single file, or the bisect depth cap reached) returns
          ``[]`` — the runner never truncates file, body, or manifest content
          to force a fit. Logged, never raised.
    """
    if len(batch.items) > 1 and depth < _MAX_BATCH_BISECT_DEPTH:
        logger.warning(
            "%s: batch %s/%s overflowed (%s: %s); bisecting %s file(s) at depth %s",
            pass_label,
            batch.index,
            batch.total,
            type(exc).__name__,
            exc,
            len(batch.items),
            depth,
        )
        mid = len(batch.items) // 2
        results: List[T] = []
        for half_items in (batch.items[:mid], batch.items[mid:]):
            half_batch = FileBatch(
                items=half_items, index=batch.index, total=batch.total, is_partial=True
            )
            results.extend(
                _run_batch_with_recovery(
                    model,
                    reasoning_system_prompt,
                    formatting_instructions,
                    tools,
                    build_prompt,
                    parse,
                    half_batch,
                    pass_label,
                    depth=depth + 1,
                )
            )
        return results

    logger.warning(
        "%s: batch %s/%s overflowed (%s: %s) and cannot be split further "
        "without truncating content; skipping",
        pass_label,
        batch.index,
        batch.total,
        type(exc).__name__,
        exc,
    )
    return []


def run_submission_pass(
    llm: LLMClient,
    *,
    changed_files: List[Tuple[str, str]],
    reasoning_system_prompt: str,
    formatting_instructions: str,
    build_prompt: Callable[[FileBatch], str],
    tools: list,
    parse: Callable[[str], T],
    pass_label: str = "SubmissionPass",
) -> List[T]:
    """Run one additive code-review submission pass; never raises.

    Owns think-then-format ``Agent`` calls and reactive overflow recovery
    (file-list bisect only). The calling pass supplies split system prompts,
    tool list, a ``build_prompt`` closure that renders the full batch with no
    character caps, and a ``parse`` callback.

    Preconditions:
        - ``changed_files`` holds this submission's ``(path, content)`` pairs
          in submission order (may be empty).
        - ``build_prompt`` is deterministic given ``batch``, does not mutate
          its arguments, and must not truncate file, body, or manifest text.

    Postconditions:
        - Returns ``[]`` without constructing an ``Agent`` when
          ``changed_files`` is empty.
        - Otherwise issues one initial think-then-format call over the full
          changed-file set (reactive bisect may add calls on overflow) and
          returns one entry per batch that completed successfully, in batch
          order.
        - A batch that fails non-recoverably contributes nothing but never
          discards results already collected from other batches; this
          function itself never raises.
    """
    if not changed_files:
        return []

    model = resolve_code_review_model(llm)
    batch = FileBatch(items=list(changed_files), index=1, total=1)
    return _run_batch_with_recovery(
        model,
        reasoning_system_prompt,
        formatting_instructions,
        tools,
        build_prompt,
        parse,
        batch,
        pass_label,
        depth=0,
    )
