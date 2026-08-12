"""Shared runner for once-per-submission additive code-review passes.

The additive passes (`architecture_consistency_pass.py`,
`side_effect_impact_pass.py`, `merged_architecture_side_effect_pass.py`) each
run one or more `strands.Agent` calls over the whole changed-file set, on top
of the map-phase chunk review. All three now construct those calls only
through this module: context budgeting, proactive file-group chunking
(splitting an oversized changed-file set into multiple bounded calls), `Agent`
construction, and reactive overflow recovery (retrying a call that overflows
the model's context mid-turn, rather than simply skipping it) — so a pass only
supplies its prompt/tools/parser.

This module is self-contained and importable in isolation.

Invariants:

    - **Additive-only, fail-safe.** :func:`run_submission_pass` never raises
      from a per-batch failure. A batch that cannot be recovered contributes
      nothing to the result and is logged, not raised — the caller (an
      additive pass) keeps whatever other batches produced, exactly like the
      pre-runner fail-safe posture each pass already has.
    - **Bounded cost.** Proactive chunking packs the changed-file set into as
      few batches as fit the computed budget (one batch, the common case, for
      a submission that already fits); reactive recovery only grows the call
      count for a batch that has already overflowed, and is depth-bounded so
      it can never recurse unboundedly.
    - **Callers own content, not mechanics.** The runner never builds a
      `CodebaseIndex`, never invents prompt text, and never validates parsed
      findings — those stay entirely with the calling pass via the
      `build_prompt`/`tools`/`parse` callbacks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Tuple, TypeVar, Union

from strands.types.exceptions import ContextWindowOverflowException, MaxTokensReachedException

from llm_service import LLMClient, LLMClientModel, LLMTruncatedError
from llm_service.config import resolve_max_output_tokens
from software_engineering_team.shared.context_sizing import (
    CODE_REVIEW_MERGED_PASS_BASE_SCAFFOLDING_CHARS,
    compute_code_review_merged_pass_budgets,
)

from .model_resolution import resolve_code_review_model
from .via_reasoning import run_agent_via_reasoning

try:
    from strands.models.model import Model as _StrandsModel
except ImportError:  # pragma: no cover - strands is a required dependency
    _StrandsModel = object  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Depth cap for reactive file-group bisection: at most this many successive
# halvings of a batch before recovery falls back to a single shrink-and-retry
# instead of continuing to split. Bounds worst-case call count per submission
# to O(2 ** depth) leaf batches; not env-overridable, unlike the map-phase
# bisector, since this operates at whole-file granularity where a handful of
# levels already isolates a single culprit file.
_MAX_BATCH_BISECT_DEPTH = 4


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


# Overflow-shaped failures that trigger reactive recovery (bisect/shrink)
# rather than an immediate skip. ``LLMTruncatedError`` (finish_reason
# "length") is included alongside the native Strands exceptions because the
# injected-``LLMClientModel`` test/production path can raise it for the same
# "ran out of room" condition a bare Strands model raises the other two for.
_OVERFLOW_ERRORS: Tuple[type, ...] = (LLMTruncatedError, *_strands_overflow_errors())


def _is_overflow_shaped(exc: BaseException) -> bool:
    """True when ``exc`` signals the call ran out of context/output room.

    Postconditions: returns ``isinstance(exc, _OVERFLOW_ERRORS)``. Any other
        exception (malformed JSON, generic ``LLMError``, programming bugs) is
        not overflow-shaped and must not trigger bisect/shrink recovery.
    """
    return isinstance(exc, _OVERFLOW_ERRORS)


@dataclass(frozen=True)
class SubmissionPassBudgets:
    """Character/token budgets for one submission-pass batch call.

    Attributes:
        max_inline_code_chars: Per-call budget for inlined changed-file
            content (drives proactive chunking).
        max_manifest_chars: Per-call budget for the changed-file path
            manifest, truncated with a tool-reachable overflow note when the
            full list does not fit.
        max_extra_body_chars: Per-call budget for the pass-specific extra
            body (sized from ``extra_reserved_chars``, e.g. an architecture
            document) that ``build_prompt`` inlines alongside the manifest
            and code; ``0`` when the pass has no such body. A ``build_prompt``
            that inlines this body in full without truncating to this budget
            can build a prompt larger than the computed context allowance.
        reserved_response_tokens: Output-token reserve the resolved model is
            clamped to for this call.
    """

    max_inline_code_chars: int
    max_manifest_chars: int
    max_extra_body_chars: int
    reserved_response_tokens: int


@dataclass(frozen=True)
class FileBatch:
    """One batch of changed files for a single submission-pass call.

    Attributes:
        items: ``(path, content)`` pairs in this batch, a subset of the full
            changed-file set in submission order.
        index: 1-based position of this batch among ``total``.
        total: Total batch count for this submission-pass invocation.
        is_partial: True when this batch is a reactive-recovery bisect/shrink
            child rather than one of the ``total`` proactive batches. A
            partial batch's ``items`` never represents everything ``index``
            covers — ``build_prompt`` must not render it as a complete batch
            (e.g. must not imply "batch 1 of 1" means the full changed-file
            set when a recovery split leaves only some files in ``items``).
    """

    items: List[Tuple[str, str]]
    index: int
    total: int
    is_partial: bool = False


def _manifest_chars(paths: List[str]) -> int:
    """Char count of the changed-file path list as it would appear in a prompt.

    Postconditions: returns ``>= 0``; one newline per path plus the section header.
    """
    header = f"**Changed files in this submission ({len(paths)}):**\n"
    return len(header) + sum(len(p) + 1 for p in paths)


def _estimated_file_block_chars(path: str, content: str) -> int:
    """Conservative estimate of one changed-file block's rendered prompt size.

    Postconditions: returns ``>= len(content)``. Used only to group files into
        batches (:func:`_pack_batches`), not to render — actual rendering is
        the caller's ``build_prompt`` responsibility.
    """
    heading = f"### {path} ###"
    return len(heading) + len(content) + 32  # fences + newlines headroom


def _pack_batches(
    items: List[Tuple[str, str]],
    max_chars: int,
) -> List[List[Tuple[str, str]]]:
    """Group changed-file ``(path, content)`` pairs into batches bounded by ``max_chars``.

    Greedy per-file packing: files are kept whole (never split mid-file) and
    packed in submission order until the next file would push the running
    estimate over budget, then a new batch starts.

    Preconditions: ``max_chars`` is any int (non-positive is treated as "no
        useful inline budget").

    Postconditions:
        - Every input pair appears in exactly one returned batch, in original
          order; no pair is dropped or duplicated.
        - Returns a single batch holding every pair when they already fit
          ``max_chars`` (the common case) or when ``max_chars <= 0`` (no batch
          could inline anything either way, so splitting would only add calls
          for no benefit).
        - A single file whose own estimated size exceeds ``max_chars`` becomes
          its own one-file batch rather than being merged with neighbors.
        - Returns ``[]`` only when ``items`` is empty.
    """
    if not items:
        return []
    if max_chars <= 0:
        return [items]
    batches: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    current_size = 0
    for path, content in items:
        size = _estimated_file_block_chars(path, content)
        if current and current_size + size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append((path, content))
        current_size += size
    if current:
        batches.append(current)
    return batches


def _shrink_items(
    items: List[Tuple[str, str]],
) -> Optional[List[Tuple[str, str]]]:
    """Halve the inlined content of every file in a batch, for one shrink retry.

    Preconditions: ``items`` is non-empty.

    Postconditions:
        - Returns a new list with each file's content cut to half its length,
          when at least one file's content actually shrinks.
        - Returns ``None`` when no file's content can shrink further (every
          file is already empty) — the caller should give up rather than
          retry with an identical batch.
    """
    assert items, "items must be non-empty"
    shrunk: List[Tuple[str, str]] = []
    changed = False
    for path, content in items:
        half = len(content) // 2
        if half < len(content):
            changed = True
        shrunk.append((path, content[:half]))
    return shrunk if changed else None


def _shrink_budgets(budgets: SubmissionPassBudgets) -> SubmissionPassBudgets:
    """Halve the per-call inline-code budget for one shrink retry.

    A ``build_prompt`` that truncates each file to
    ``budgets.max_inline_code_chars`` (rather than to that file's own,
    already-shrunk length) would otherwise render an identical prefix on the
    shrink retry whenever a file's content is at least twice the budget —
    :func:`_shrink_items` alone cannot guarantee a smaller rendered payload
    in that case. Shrinking the budget alongside the content means at least
    one of the two signals a compliant ``build_prompt`` can act on always
    shrinks.

    Postconditions: returns a new ``SubmissionPassBudgets`` with
        ``max_inline_code_chars`` halved (``>= 0``); every other field is
        unchanged. Never mutates ``budgets``.
    """
    return replace(budgets, max_inline_code_chars=budgets.max_inline_code_chars // 2)


def _with_output_budget(
    model: "Union[LLMClient, _StrandsModel]",
    *,
    response_tokens: int,
) -> "Union[LLMClient, _StrandsModel]":
    """Align the model's output cap with this call's response reserve.

    Preconditions:
        - ``model`` is the result of :func:`model_resolution.resolve_code_review_model`.
        - ``response_tokens`` is the reserve from
          :func:`~software_engineering_team.shared.context_sizing.compute_code_review_merged_pass_budgets`
          (``>= 1024``).

    Postconditions:
        - When ``model`` is an ``LLMClientModel``, clones with
          ``max_tokens=response_tokens`` whenever the effective cap (model pin
          first, else ``LLM_MAX_OUTPUT_TOKENS``, else unset ``0``) differs from
          that reserve.
        - Injected non-``LLMClientModel`` test models are returned unchanged.
        - Never mutates ``model``.
    """
    assert response_tokens >= 1024, "response_tokens must be >= 1024"
    if not isinstance(model, LLMClientModel):
        return model
    configured = resolve_max_output_tokens()
    pinned = model.get_config().get("max_tokens")
    pinned_int = pinned if isinstance(pinned, int) and pinned > 0 else 0
    effective = pinned_int if pinned_int > 0 else configured
    if effective != response_tokens:
        return model.clone(max_tokens=response_tokens)
    return model


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
    build_prompt: Callable[[FileBatch, SubmissionPassBudgets], str],
    parse: Callable[[str], T],
    budgets: SubmissionPassBudgets,
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
          :func:`_recover_from_overflow` (bisect while possible, else one
          shrink-and-retry), returning whatever that recovers (possibly ``[]``).
    """
    try:
        prompt = build_prompt(batch, budgets)
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
            budgets,
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
    build_prompt: Callable[[FileBatch, SubmissionPassBudgets], str],
    parse: Callable[[str], T],
    budgets: SubmissionPassBudgets,
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
        - Otherwise (a single file, or the depth cap reached) attempts exactly
          one shrink-and-retry with a fresh ``Agent`` call: both the item
          content (:func:`_shrink_items`) and the inline-code budget
          (:func:`_shrink_budgets`) shrink together, so the retry's rendered
          payload is smaller even when ``build_prompt`` truncates by budget
          rather than by content length. The retry batch is marked
          ``is_partial=True`` (the shrunk content is not the full file body);
          returns ``[result]`` on success.
        - Returns ``[]`` when nothing can be shrunk further, or the shrink
          retry itself still raises — logged, never raised.
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
                    budgets,
                    half_batch,
                    pass_label,
                    depth=depth + 1,
                )
            )
        return results

    shrunk = _shrink_items(batch.items)
    if shrunk is None:
        logger.warning(
            "%s: batch %s/%s overflowed (%s: %s) and cannot be split/shrunk further; skipping",
            pass_label,
            batch.index,
            batch.total,
            type(exc).__name__,
            exc,
        )
        return []

    logger.warning(
        "%s: batch %s/%s overflowed (%s: %s); shrinking inline content and retrying once",
        pass_label,
        batch.index,
        batch.total,
        type(exc).__name__,
        exc,
    )
    shrink_batch = FileBatch(items=shrunk, index=batch.index, total=batch.total, is_partial=True)
    shrink_budgets = _shrink_budgets(budgets)
    try:
        prompt = build_prompt(shrink_batch, shrink_budgets)
        result = _call_agent(
            model,
            reasoning_system_prompt,
            formatting_instructions,
            tools,
            prompt,
            parse,
        )
    except Exception as retry_exc:  # noqa: BLE001 - one shrink attempt only
        logger.warning(
            "%s: batch %s/%s still failed after shrink (%s: %s); skipping",
            pass_label,
            batch.index,
            batch.total,
            type(retry_exc).__name__,
            retry_exc,
        )
        return []
    return [result]


def run_submission_pass(
    llm: LLMClient,
    *,
    changed_files: List[Tuple[str, str]],
    reasoning_system_prompt: str,
    formatting_instructions: str,
    build_prompt: Callable[[FileBatch, SubmissionPassBudgets], str],
    tools: list,
    parse: Callable[[str], T],
    extra_reserved_chars: int = 0,
    finding_array_count: int = 1,
    pass_label: str = "SubmissionPass",
) -> List[T]:
    """Run one additive code-review submission pass; never raises.

    Owns context budgeting, proactive file-group chunking, think-then-format
    ``Agent`` calls, and reactive overflow recovery. The calling pass supplies
    only content: split system prompts, tool list, a ``build_prompt`` closure
    that renders one batch's user prompt from its manifest/content budgets,
    and a ``parse`` callback that turns one call's raw reply into a typed
    result (or raises on a malformed reply — the runner treats a ``parse``
    failure the same as an ``Agent`` call failure).

    Preconditions:
        - ``changed_files`` holds this submission's ``(path, content)`` pairs
          in submission order (may be empty).
        - ``build_prompt`` is deterministic given ``(batch, budgets)`` and
          does not mutate its arguments.
        - ``extra_reserved_chars`` is the size of any pass-specific body
          ``build_prompt`` also inlines beyond the changed-file manifest/code
          (e.g. an architecture document); ``0`` when there is none.
          ``build_prompt`` must truncate that body to
          ``budgets.max_extra_body_chars`` (the allowance this reserves it,
          which may be smaller than ``extra_reserved_chars`` when the model
          context is tight) rather than inlining it in full — otherwise the
          rendered prompt can exceed the computed context allowance even
          though the runner accounted for the body's size.
        - ``finding_array_count`` is ``1`` or ``2`` (forwarded to
          :func:`~software_engineering_team.shared.context_sizing.compute_code_review_merged_pass_budgets`,
          which raises ``ValueError`` for any other value).

    Postconditions:
        - Returns ``[]`` without constructing an ``Agent`` when
          ``changed_files`` is empty, or when the model context cannot hold
          the fixed prompt plus a usable response reserve.
        - Otherwise splits ``changed_files`` into one or more budget-bounded
          batches (:func:`_pack_batches`; a single batch, and a single
          ``Agent`` call, when everything fits — no behavior change for a
          submission under budget) and returns one entry per batch that
          completed successfully (recovered or not), in batch order.
        - A batch that fails non-recoverably contributes nothing but never
          discards results already collected from other batches; this
          function itself never raises.
    """
    if not changed_files:
        return []

    manifest_chars = _manifest_chars([path for path, _ in changed_files])
    fixed_prompt_chars = len(reasoning_system_prompt) + len(formatting_instructions)
    raw_budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=extra_reserved_chars,
        system_prompt_chars=fixed_prompt_chars,
        manifest_chars=manifest_chars,
        base_scaffolding_chars=CODE_REVIEW_MERGED_PASS_BASE_SCAFFOLDING_CHARS,
        finding_array_count=finding_array_count,
    )
    if raw_budgets is None:
        logger.warning(
            "%s: model context too small for fixed prompt + response reserve; skipping call",
            pass_label,
        )
        return []
    budgets = SubmissionPassBudgets(
        max_inline_code_chars=raw_budgets.max_inline_code_chars,
        max_manifest_chars=raw_budgets.max_manifest_chars,
        max_extra_body_chars=raw_budgets.max_architecture_chars,
        reserved_response_tokens=raw_budgets.reserved_response_tokens,
    )

    model = _with_output_budget(
        resolve_code_review_model(llm), response_tokens=budgets.reserved_response_tokens
    )
    batches = _pack_batches(list(changed_files), budgets.max_inline_code_chars)
    total = len(batches)
    if total > 1:
        logger.info(
            "%s: changed-file set split into %s batches (budget=%s chars/call, "
            "CODE_REVIEW_TAIL_PASS_CHUNK_CHARS)",
            pass_label,
            total,
            budgets.max_inline_code_chars,
        )

    results: List[T] = []
    for batch_index, items in enumerate(batches, start=1):
        batch = FileBatch(items=items, index=batch_index, total=total)
        results.extend(
            _run_batch_with_recovery(
                model,
                reasoning_system_prompt,
                formatting_instructions,
                tools,
                build_prompt,
                parse,
                budgets,
                batch,
                pass_label,
                depth=0,
            )
        )
    return results
