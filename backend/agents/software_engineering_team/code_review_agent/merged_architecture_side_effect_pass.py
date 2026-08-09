"""Merged architecture-consistency + side-effect-impact pass (one LLM call).

Runs both additive whole-submission checks that the in-process coordinator
previously scheduled as two independent Agent calls, in a single pass with
a half-aware ``build_merged_architecture_side_effect_prompt``. Findings are
split back into the two lists downstream merge/gate logic already expects.

Invariants:

    - **Additive-only, fail-safe.** Never removes or mutates findings the
      caller already has; any setup/LLM/validation failure yields
      ``([], [])``.
    - **Bounded cost per call.** The changed-file set is split into bounded
      batches (mirroring the map-phase chunk budget,
      ``compute_code_review_map_chunk_chars``) whenever it exceeds the
      per-call inline-code budget; each batch is one independent LLM call.
      A submission that fits under the budget still makes exactly one call,
      identical to the pre-batching behavior.
    - **``CODE_REVIEW`` profile only.** Same restriction as each standalone
      pass.
    - **Context-aware budgeting.** Changed-file inlining (and, when needed,
      architecture text / the path manifest) is sized for the combined
      system prompt and dual finding-array response — not the map-call code
      allowance alone. Disabled halves are omitted from the prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional, Tuple, Union

from strands import Agent, tool
from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, LLMClientModel
from llm_service.config import resolve_max_output_tokens
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import (
    CODE_REVIEW_MERGED_PASS_BASE_SCAFFOLDING_CHARS,
    MergedPassBudgets,
    compute_code_review_merged_pass_budgets,
)

from . import architecture_consistency_pass as arch_pass
from . import side_effect_impact_pass as side_pass
from .architecture_context import (
    architecture_document_text,
    architecture_evidence_available,
)
from .false_positive_filter import CodebaseIndex, _build_tools, code_fence_for
from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue
from .profiles import ReviewProfile
from .prompts import build_merged_architecture_side_effect_prompt
from .repo_reader import RepoReader

logger = logging.getLogger(__name__)

_ARCH_ENV = "CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS"
_SIDE_ENV = "CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS"


def find_architecture_and_side_effect_issues(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
    """Run both additive whole-submission checks in a single LLM call.

    Preconditions:
        - ``input_data`` is the coordinator's review input for this submission.
        - ``index``, when given, was built from this same ``input_data``/
          ``repo_reader``.

    Postconditions:
        - Returns ``([], [])`` with no LLM call when both halves are disabled
          (env flags off, architecture disabled for lack of repository /
          document evidence, or side-effect disabled by ``pre_numbered`` with
          architecture also off), profile is not ``CODE_REVIEW``, or there are
          no readable files.
        - Otherwise returns two lists of NEW ``CodeReviewIssue``s
          (architecture/refactor and side-effects/documentation respectively),
          each validated like the corresponding standalone pass; never raises.
        - When only one half is enabled, still makes the merged call but returns
          ``[]`` for the disabled half. ``pre_numbered`` forces the side-effect
          half off (same guard as the standalone side-effect pass, via
          ``side_pass._effective_pre_numbered`` -- a caller-supplied
          ``full_content`` re-enables it). Architecture is forced off when
          there is no architecture payload and no ``repo_reader`` /
          ``existing_codebase`` evidence.
        - When the changed-file set's estimated inline size exceeds one call's
          budget, it is split into multiple bounded batches (see
          :func:`_split_changed_files_into_batches`); findings from every
          batch are concatenated into the same two returned lists. A
          submission under the budget still makes exactly one call.
    """
    arch_on = env_flag_enabled(_ARCH_ENV)
    # Mirror ``find_side_effect_impact_issues``: pre-numbered hunk mode only has
    # partial file excerpts, so caller-impact analysis must not run (architecture
    # half may still proceed) -- unless the caller supplied ``full_content``,
    # which overlays real full bodies onto the index for this same submission
    # (see ``side_pass._effective_pre_numbered``).
    side_on = env_flag_enabled(_SIDE_ENV) and not side_pass._effective_pre_numbered(input_data)
    # Architecture half needs either a formal architecture payload or off-diff /
    # existing-codebase evidence. Without those, list_files()/read_file() only
    # see the changed submission files, so "established repository structure"
    # cannot be verified and any architecture finding would be speculation.
    if arch_on and not architecture_evidence_available(input_data, repo_reader, index):
        arch_on = False
    if not arch_on and not side_on:
        return [], []
    if input_data.profile != ReviewProfile.CODE_REVIEW:
        return [], []
    try:
        return _run_pass(
            llm,
            input_data,
            repo_reader,
            index,
            arch_on=arch_on,
            side_on=side_on,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: never break the review
        logger.warning(
            "MergedArchitectureSideEffectPass: failed (%s: %s); returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return [], []


def _run_pass(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: Optional[CodebaseIndex],
    *,
    arch_on: bool,
    side_on: bool,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
    """Core of :func:`find_architecture_and_side_effect_issues`; may raise.

    Preconditions:
        - At least one of ``arch_on`` / ``side_on`` is True.
        - ``index``, when given, was built from this same ``input_data``/
          ``repo_reader``.

    Postconditions:
        - Same contract as the public entry, minus the env/profile early
          returns the caller already handled.
        - Skips the LLM call (returns ``([], [])``) when the model context
          cannot hold the fixed prompt plus a usable response reserve.
        - When the changed-file set's estimated inline size exceeds one call's
          budget, splits it into multiple bounded batches (see
          :func:`_split_changed_files_into_batches`) and issues one
          independent LLM call per batch, concatenating each batch's
          validated findings into the two returned lists. A batch whose call
          raises (malformed reply, agent/LLM failure) contributes no findings
          but does not discard findings already collected from other
          batches — only a failure before batching starts (index/budget
          setup) propagates to the caller's outer fail-safe.
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        return [], []

    system_prompt = build_merged_architecture_side_effect_prompt(arch_on=arch_on, side_on=side_on)
    arch_body = architecture_document_text(input_data.architecture) if arch_on else ""
    changed_paths = list(index.files.keys())
    manifest_chars = _manifest_chars(changed_paths)
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=len(arch_body),
        system_prompt_chars=len(system_prompt),
        manifest_chars=manifest_chars,
        base_scaffolding_chars=CODE_REVIEW_MERGED_PASS_BASE_SCAFFOLDING_CHARS,
        finding_array_count=(1 if arch_on ^ side_on else 2),
    )
    if budgets is None:
        logger.warning(
            "MergedArchitectureSideEffectPass: model context too small for fixed "
            "prompt + response reserve; skipping merged call"
        )
        return [], []

    model = _with_merged_pass_output_budget(
        resolve_code_review_model(llm),
        response_tokens=budgets.reserved_response_tokens,
    )
    tools = _build_merged_pass_tools(index, side_on=side_on)
    batches = _split_changed_files_into_batches(
        list(index.files.items()), budgets.max_inline_code_chars
    )
    total_batches = len(batches)
    if total_batches > 1:
        logger.info(
            "MergedArchitectureSideEffectPass: changed-file set split into %s batches "
            "(budget=%s chars/call)",
            total_batches,
            budgets.max_inline_code_chars,
        )

    architecture_findings: List[CodeReviewIssue] = []
    side_effect_findings: List[CodeReviewIssue] = []
    for batch_number, batch in enumerate(batches, start=1):
        try:
            batch_arch, batch_side = _run_batch(
                model,
                system_prompt,
                tools,
                index,
                arch_body,
                budgets,
                arch_on=arch_on,
                side_on=side_on,
                pre_numbered=side_pass._effective_pre_numbered(input_data),
                batch_items=batch,
                batch_index=batch_number if total_batches > 1 else None,
                total_batches=total_batches if total_batches > 1 else None,
            )
        except Exception as exc:  # noqa: BLE001 - one bad batch must not drop the rest
            logger.warning(
                "MergedArchitectureSideEffectPass: batch %s/%s failed (%s: %s); "
                "returning no findings for this batch",
                batch_number,
                total_batches,
                type(exc).__name__,
                exc,
            )
            continue
        architecture_findings.extend(batch_arch)
        side_effect_findings.extend(batch_side)

    if architecture_findings or side_effect_findings:
        logger.info(
            "MergedArchitectureSideEffectPass: found %s architecture and %s side-effect finding(s)",
            len(architecture_findings),
            len(side_effect_findings),
        )
    return architecture_findings, side_effect_findings


def _run_batch(
    model: "Union[LLMClient, _StrandsModel]",
    system_prompt: str,
    tools: list,
    index: CodebaseIndex,
    arch_body: str,
    budgets: MergedPassBudgets,
    *,
    arch_on: bool,
    side_on: bool,
    pre_numbered: bool,
    batch_items: List[Tuple[str, str]],
    batch_index: Optional[int],
    total_batches: Optional[int],
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
    """Run one merged-pass LLM call for a single batch of changed files; may raise.

    Preconditions:
        - ``batch_items`` is a non-empty subset of ``index.files.items()``
          (see :func:`_split_changed_files_into_batches`).
        - ``batch_index``/``total_batches`` are both ``None`` (single-batch
          submission) or both set to this batch's 1-based position and the
          total batch count.

    Postconditions:
        - Issues exactly one LLM call via a fresh ``Agent`` (never reuses a
          prior batch's conversation state, so every batch gets the full
          response/tool-transcript budget).
        - Returns this batch's validated findings for each enabled half; each
          half independently falls back to ``[]`` on its own parse/validation
          failure (see :func:`_issues_from_half`) without raising.
        - Raises on a malformed top-level reply (non-JSON or non-object) or an
          ``Agent``/LLM invocation failure; the caller treats a raise from
          this batch as a fail-safe no-op for the batch, not the submission.
    """
    prompt = _build_prompt(
        index,
        arch_body,
        budgets.max_inline_code_chars,
        max_architecture_chars=budgets.max_architecture_chars,
        max_manifest_chars=budgets.max_manifest_chars,
        arch_on=arch_on,
        side_on=side_on,
        content_items=batch_items,
        batch_index=batch_index,
        total_batches=total_batches,
    )
    agent = Agent(model=model, system_prompt=system_prompt, tools=tools)
    raw = str(agent(prompt)).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError(f"merged pass expected a JSON object, got {type(data).__name__}")
    # Validate each half independently. A malformed / missing key on one side
    # must not discard valid findings from the other (the Agent+tools path
    # cannot use ``complete_validated``'s corrective retry the way chunk review
    # does, so per-half salvage matches the standalone passes' ``_parse_findings``
    # posture instead of all-or-nothing ``MergedArchitectureSideEffectResponse``
    # validation).
    architecture_findings: List[CodeReviewIssue] = []
    side_effect_findings: List[CodeReviewIssue] = []
    if arch_on:
        architecture_findings = _issues_from_half(
            data.get("architecture_findings"),
            parse=arch_pass.parse_findings,
            validate=arch_pass.validate_findings,
            index=index,
            pre_numbered=pre_numbered,
        )
    if side_on:
        side_effect_findings = _issues_from_half(
            data.get("side_effect_findings"),
            parse=side_pass.parse_findings,
            validate=side_pass.validate_findings,
            index=index,
            pre_numbered=pre_numbered,
        )
    return architecture_findings, side_effect_findings


def _manifest_chars(paths: List[str]) -> int:
    """Char count of the changed-file path list as emitted in the user prompt.

    Postconditions: returns ``>= 0``; one newline per path plus the section header.
    """
    header = f"**Changed files in this submission ({len(paths)}):**\n"
    return len(header) + sum(len(p) + 1 for p in paths)


def _estimated_file_block_chars(path: str, content: str) -> int:
    """Conservative estimate of one changed-file block's rendered prompt size.

    Postconditions:
        - Returns ``>= len(content)``. Used only to group files into batches
          (:func:`_split_changed_files_into_batches`), not to render — a
          batch's actual render still goes through ``_fit_changed_file_block``,
          which truncates/omits at render time as a backstop against any
          under-estimation here.
    """
    heading = f"### {path} ###"
    return len(heading) + len(content) + 32  # fences + newlines headroom


def _split_changed_files_into_batches(
    items: List[Tuple[str, str]],
    max_chars: int,
) -> List[List[Tuple[str, str]]]:
    """Group changed-file (path, content) pairs into batches bounded by ``max_chars``.

    Mirrors the greedy per-unit packing ``compute_code_review_map_chunk_chars``
    callers use for map-phase chunking, sized at file granularity: files are
    kept whole (never split mid-file) and packed in submission order until the
    next file would push the running estimate over budget, then a new batch
    starts.

    Preconditions:
        - ``max_chars`` is ``>= 0`` (the merged pass's per-call inline-code
          budget, :attr:`MergedPassBudgets.max_inline_code_chars`).

    Postconditions:
        - Every input pair appears in exactly one returned batch, in original
          order; no pair is dropped or duplicated.
        - Returns a single batch holding every pair when their combined
          estimated size already fits ``max_chars`` — the common case, and
          identical to the pre-batching single-call behavior (including when
          ``items`` holds only one file, regardless of ``max_chars``).
        - Returns a single batch holding every pair when ``max_chars <= 0``:
          with no positive inline budget, no batch could inline any content
          either way (every batch would still omit its files and fall back to
          ``read_file``/``list_changed_files``), so splitting would only add
          extra LLM calls for no benefit — this preserves the pre-batching
          single-call omit-and-recover-via-tools behavior exactly.
        - A single file whose own estimated size exceeds ``max_chars`` becomes
          its own one-file batch rather than being merged with neighbors (its
          content is still truncated/omitted to fit at render time by
          ``_fit_changed_file_block``).
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


# Default / hard caps for ``list_changed_files`` pagination so a truncated
# manifest cannot re-enter the context as one unbounded tool result.
_LIST_CHANGED_FILES_DEFAULT_LIMIT = 100
_LIST_CHANGED_FILES_MAX_LIMIT = 500
_LIST_CHANGED_FILES_PAGE_MAX_CHARS = 8_000


def format_changed_files_page(
    paths: List[str],
    *,
    offset: int = 0,
    limit: int = _LIST_CHANGED_FILES_DEFAULT_LIMIT,
    max_chars: int = _LIST_CHANGED_FILES_PAGE_MAX_CHARS,
) -> str:
    """Format a bounded page of submission paths for ``list_changed_files``.

    Preconditions:
        - ``paths`` is an ordered list of submission path strings (may be empty).
        - ``max_chars`` is ``>= 1``.

    Postconditions:
        - Returns a non-empty string. Empty ``paths`` yields
          ``"(no changed files)"``.
        - At most ``limit`` paths (clamped to
          ``[1, _LIST_CHANGED_FILES_MAX_LIMIT]``) starting at ``offset``
          (clamped to ``>= 0``), stopping early when joining the next path
          would exceed ``max_chars`` (a single oversized path is still
          returned whole so ``read_file`` remains usable).
        - When more paths remain after this page, appends a next-``offset``
          hint so every path stays reachable via further calls.
        - Never raises.
    """
    assert max_chars >= 1, "max_chars must be >= 1"
    total = len(paths)
    if total == 0:
        return "(no changed files)"
    start = max(0, int(offset))
    page_limit = max(1, min(int(limit), _LIST_CHANGED_FILES_MAX_LIMIT))
    if start >= total:
        return (
            f"(no paths in this page; total={total}. "
            f"call list_changed_files(offset=0, limit={page_limit}) from the start)"
        )
    end_cap = min(total, start + page_limit)
    page: List[str] = []
    used = 0
    end = start
    for i in range(start, end_cap):
        path = paths[i]
        add = len(path) + (1 if page else 0)
        if page and used + add > max_chars:
            break
        if not page and len(path) > max_chars:
            page.append(path)
            end = i + 1
            break
        page.append(path)
        used += add
        end = i + 1
    body = "\n".join(page) if page else "(no paths in this page)"
    if end < total:
        return (
            f"{body}\n"
            f"(showing paths {start + 1}-{end} of {total}; "
            f"call list_changed_files(offset={end}, limit={page_limit}) for more)"
        )
    if start > 0:
        return f"{body}\n(showing paths {start + 1}-{end} of {total})"
    return body


def _build_merged_pass_tools(index: CodebaseIndex, *, side_on: bool) -> list:
    """Tools for the merged pass, including a changed-files-only listing.

    Postconditions:
        - Returns the side-effect tool set when ``side_on``, else the shared
          architecture/false-positive tool set, plus ``list_changed_files`` so
          truncated manifests remain recoverable without confusing submission
          paths with repository paths from ``list_files()``.
    """
    base = side_pass.build_side_effect_tools(index) if side_on else _build_tools(index)

    @tool
    def list_changed_files(
        offset: int = 0,
        limit: int = _LIST_CHANGED_FILES_DEFAULT_LIMIT,
    ) -> str:
        """List changed file paths in this submission only (paginated).

        Unlike ``list_files()``, this never includes repository paths outside
        the submission. Use it when the changed-file manifest in the prompt was
        truncated, then ``read_file(path)`` for any omitted path. Pass
        ``offset``/``limit`` to page through large submissions — do not expect
        one call to return every path when the list is long.

        Returns:
            One submission path per line for this page, plus a next-offset hint
            when more remain, or a message when none are available.
        """
        try:
            return format_changed_files_page(
                list(index.files.keys()),
                offset=offset,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 - tool errors become tool messages
            return f"Error: could not list changed files: {type(exc).__name__}: {exc}"

    return [*base, list_changed_files]


def _with_merged_pass_output_budget(
    model: "Union[LLMClient, _StrandsModel]",
    *,
    response_tokens: int,
) -> "Union[LLMClient, _StrandsModel]":
    """Align the model's output cap with the merged call's response reserve.

    Preconditions:
        - ``model`` is the result of :func:`resolve_code_review_model`.
        - ``response_tokens`` is the reserve from
          :func:`compute_code_review_merged_pass_budgets` (``>= 1024``).

    Postconditions:
        - When ``model`` is an ``LLMClientModel``, clones with
          ``max_tokens=response_tokens`` whenever the effective cap (model pin
          first, else ``LLM_MAX_OUTPUT_TOKENS``, else unset ``0``) differs from that
          reserve — raising tight caps and clamping oversized / unset provider
          defaults so the completion cannot exceed the input budget.
        - Injected non-``LLMClientModel`` test models are returned unchanged.
        - Never mutates ``model``.
    """
    assert response_tokens >= 1024, "response_tokens must be >= 1024"
    if not isinstance(model, LLMClientModel):
        return model
    configured = resolve_max_output_tokens()
    pinned = model.get_config().get("max_tokens")
    pinned_int = pinned if isinstance(pinned, int) and pinned > 0 else 0
    # Match provider precedence: an explicit model pin wins over the env cap.
    effective = pinned_int if pinned_int > 0 else configured
    if effective != response_tokens:
        return model.clone(max_tokens=response_tokens)
    return model


def _issues_from_half(
    raw_list: object,
    *,
    parse: Callable[[object], List[CodeReviewIssue]],
    validate: Callable[..., List[CodeReviewIssue]],
    index: CodebaseIndex,
    pre_numbered: bool,
) -> List[CodeReviewIssue]:
    """Coerce one merged-response array into validated ``CodeReviewIssue``s.

    Preconditions:
        - ``parse`` / ``validate`` are the corresponding standalone pass helpers
          (``parse_findings`` / ``validate_findings``).
        - ``parse`` accepts a dict shaped like ``{"findings": <raw_list>}`` (the
          same envelope each standalone pass's JSON schema uses); callers wrap
          the merged half-array that way.

    Postconditions:
        - Returns ``[]`` when ``raw_list`` is missing or not a list (that half
          produced nothing usable) without raising.
        - Returns ``[]`` for *this half only* when ``parse`` / ``validate`` raise
          (a malformed half must not discard findings from the other half).
        - Otherwise returns the parse+validate result for that half alone.
    """
    if not isinstance(raw_list, list):
        return []
    try:
        findings = parse({"findings": raw_list})
        if findings:
            findings = validate(index, findings, pre_numbered=pre_numbered)
        return findings
    except Exception as exc:  # noqa: BLE001 - per-half fail-safe
        logger.warning(
            "MergedArchitectureSideEffectPass: half failed (%s: %s); "
            "returning no findings for this half",
            type(exc).__name__,
            exc,
        )
        return []


def _build_prompt(
    index: CodebaseIndex,
    architecture_body: str,
    max_inline_chars: int,
    *,
    max_architecture_chars: int,
    max_manifest_chars: int,
    arch_on: bool,
    side_on: bool,
    content_items: Optional[List[Tuple[str, str]]] = None,
    batch_index: Optional[int] = None,
    total_batches: Optional[int] = None,
) -> str:
    """Render the single user prompt for one merged-pass LLM call.

    Preconditions:
        - ``architecture_body`` is the flattened document text (empty when the
          architecture half is off or no document was provided).
        - Budget ints are ``>= 0`` from :func:`compute_code_review_merged_pass_budgets`.
        - At least one of ``arch_on`` / ``side_on`` is True.
        - ``content_items``, when given, is this call's batch of the changed
          files (a subset of ``index.files.items()``) whose full content is
          inlined below the manifest; ``None`` inlines every changed file
          (the pre-batching / single-batch behavior).
        - ``batch_index``/``total_batches`` are both ``None`` (no batch label
          rendered) or both set to this batch's 1-based position and the
          total batch count.

    Postconditions:
        - Omits the architecture section when ``arch_on`` is False.
        - The changed-file path manifest always lists every changed file in
          the submission (from ``index.files``, not ``content_items``),
          truncated to ``max_manifest_chars`` with a tool-reachable overflow
          note when needed — batching only bounds inlined content, not
          whole-submission awareness of what changed.
        - Inlines ``content_items`` (or every changed file when ``None``) up
          to ``max_inline_chars``, deducting per-file heading/fence wrappers
          from that allowance. When ``total_batches`` is set (> 1), the
          content section header names this batch's position and points to
          the manifest/tools for files not shown in this call.
        - Ends with a return instruction containing both response keys so
          DummyLLMClient tests can anchor on the merged call; disabled halves
          are told to stay empty.
    """
    parts: List[str] = []

    if arch_on:
        if architecture_body:
            body = architecture_body[:max_architecture_chars]
            doc_fence = code_fence_for(body)
            parts.append("**Architecture document:**")
            parts.append(doc_fence)
            parts.append(body)
            parts.append(doc_fence)
            if len(body) < len(architecture_body):
                parts.append(
                    f"(Only the first {len(body)} characters of the architecture document "
                    "are shown above — the remainder was omitted to fit the model context.)"
                )
        else:
            parts.append("**Architecture document:**")
            parts.append(
                "No formal architecture document was provided for this review. "
                "For Part 1, derive architecture expectations from the repository's "
                "established structure and patterns via list_files()/read_file(); "
                "do not invent a phantom document."
            )
        parts.append("")

    changed_files = list(index.files.items())
    paths = [path for path, _ in changed_files]
    parts.extend(_render_manifest(paths, max_manifest_chars))
    parts.append("")

    batch_files = content_items if content_items is not None else changed_files
    if total_batches and total_batches > 1:
        parts.append(
            f"**Full content of the changed files (batch {batch_index} of {total_batches} — "
            f"showing {len(batch_files)} of {len(changed_files)} changed files in this "
            "submission; the rest are listed in the manifest above and reachable via "
            "list_changed_files()/read_file()):**"
        )
    else:
        parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(batch_files):
        if remaining <= 0:
            omitted = len(batch_files) - i
            break
        block_lines, _truncated = _fit_changed_file_block(path, content, remaining)
        if block_lines is None:
            omitted = len(batch_files) - i
            break
        block = "\n".join(block_lines)
        parts.extend(block_lines)
        remaining -= len(block) + 1  # +1 for the join newline before the next block
    if omitted:
        parts.append(
            f"... and {omitted} more changed file(s) not shown above; call "
            "list_changed_files(offset=0)/read_file(path) to see them "
            "(page with offset/limit when the list is long)."
        )
    parts.append("")

    if arch_on and side_on:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "search_repository()/find_function_at_line() as each part's instructions "
            "require. Address Part 1 and Part 2 independently. Prefer "
            "list_changed_files(offset, limit) when recovering omitted submission paths."
        )
    elif arch_on:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "find_function_at_line() as Part 1 requires. Prefer "
            "list_changed_files(offset, limit) when recovering omitted submission "
            "paths. Do not run side-effect analysis."
        )
    else:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "search_repository()/find_function_at_line() as Part 2 requires. Prefer "
            "list_changed_files(offset, limit) when recovering omitted submission "
            "paths. Do not run architecture analysis."
        )
    parts.append(
        'Return a single JSON object with "architecture_findings"/"side_effect_findings" '
        "keys as instructed. Return "
        '{"architecture_findings": [], "side_effect_findings": []} if neither part finds anything.'
    )
    return "\n".join(parts)


def _overflow_manifest_note(omitted: int) -> str:
    """Tool-reachable note for changed paths omitted from the inline manifest."""
    return (
        f"... and {omitted} more changed path(s) not listed; "
        "call list_changed_files(offset=0) then read_file(path) to reach them "
        "(page with offset/limit when the list is long)."
    )


def _render_manifest(paths: List[str], max_manifest_chars: int) -> List[str]:
    """Render the changed-file path list, truncated to ``max_manifest_chars``.

    Postconditions:
        - Always includes the section header.
        - When the full remaining list fits in the budget, renders every remaining
          path (no overflow note) — never reserves note room that would hide
          paths that already fit.
        - When the full list exceeds the budget, includes as many paths as fit
          and a tool-reachable overflow note for the rest.
    """
    header = f"**Changed files in this submission ({len(paths)}):**"
    lines: List[str] = [header]
    used = len(header) + 1
    shown = 0
    for i, path in enumerate(paths):
        # Prefer emitting the full remainder when it fits without an overflow note.
        rest_cost = sum(len(p) + 1 for p in paths[i:])
        if used + rest_cost <= max_manifest_chars:
            lines.extend(paths[i:])
            return lines

        line_cost = len(path) + 1
        omitted_after = len(paths) - (shown + 1)
        room_for_note = len(_overflow_manifest_note(omitted_after)) + 1 if omitted_after > 0 else 0
        if used + line_cost + room_for_note > max_manifest_chars and shown > 0:
            lines.append(_overflow_manifest_note(len(paths) - shown))
            break
        if used + line_cost > max_manifest_chars and shown == 0:
            # Budget too tight for even one path: header + overflow only.
            lines.append(_overflow_manifest_note(len(paths)))
            break
        lines.append(path)
        used += line_cost
        shown += 1
    return lines


def _fit_changed_file_block(
    path: str,
    content: str,
    remaining: int,
) -> Tuple[Optional[List[str]], bool]:
    """Build one changed-file prompt block that fits in ``remaining`` characters.

    Preconditions:
        - ``remaining`` is ``>= 0``.
        - ``path`` / ``content`` are the submission path and body to inline.

    Postconditions:
        - Returns ``(None, True)`` when even a heading/fence shell cannot fit
          (caller should omit this and every later file).
        - Otherwise returns ``(block_lines, truncated)`` where ``block_lines``
          join to a string of length ``<= remaining``. When the body is a
          prefix of ``content``, ``truncated`` is ``True`` and a read_file note
          is included. Never raises.
    """
    heading = f"### {path} ###"
    fence_reserve = 8
    base_overhead = len(heading) + 1 + 2 * (fence_reserve + 1)
    note_template = (
        "(Only the first {n} characters of `{path}` are shown above; call "
        "read_file to see the rest.)"
    )
    # Worst-case note length uses ``remaining`` as the digit width upper bound.
    note_reserve = len(note_template.format(n=remaining, path=path)) + 1
    if base_overhead >= remaining:
        return None, True

    if base_overhead + len(content) <= remaining:
        body = content
        include_note = False
    else:
        overhead = base_overhead + note_reserve
        if remaining <= overhead:
            return None, True
        body = content[: remaining - overhead]
        include_note = True

    def _lines_for(body_text: str, with_note: bool) -> List[str]:
        fence = code_fence_for(body_text)
        out = [heading, fence, body_text, fence]
        if with_note:
            out.append(note_template.format(n=len(body_text), path=path))
        return out

    block_lines = _lines_for(body, include_note)
    block = "\n".join(block_lines)
    # Actual fences can exceed the fixed reserve (long backtick runs). Shrink
    # the body — including full-file blocks — rather than dropping this file
    # and every later one.
    while len(block) > remaining and body:
        excess = len(block) - remaining
        if len(body) <= excess:
            return None, True
        body = body[: len(body) - excess]
        include_note = True
        block_lines = _lines_for(body, True)
        block = "\n".join(block_lines)
    if len(block) > remaining:
        return None, True
    return block_lines, include_note or len(body) < len(content)
