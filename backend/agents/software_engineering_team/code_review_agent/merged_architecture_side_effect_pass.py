"""Merged architecture-consistency + side-effect-impact pass (one LLM call).

Runs both additive whole-submission checks that the in-process coordinator
previously scheduled as two independent Agent calls, in a single pass with
a half-aware ``build_merged_architecture_side_effect_prompt``. Findings are
split back into the two lists downstream merge/gate logic already expects.

Invariants:

    - **Additive-only, fail-safe.** Never removes or mutates findings the
      caller already has; any setup/LLM/validation failure yields
      ``([], [])``.
    - **Bounded cost.** At most one LLM call per submission when at least
      one of the two env flags is enabled.
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
from llm_service.config import resolve_max_tokens
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import (
    CODE_REVIEW_MERGED_PASS_BASE_SCAFFOLDING_CHARS,
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
          half off (same guard as the standalone side-effect pass). Architecture
          is forced off when there is no architecture payload and no
          ``repo_reader`` / ``existing_codebase`` evidence.
    """
    arch_on = env_flag_enabled(_ARCH_ENV)
    # Mirror ``find_side_effect_impact_issues``: pre-numbered hunk mode only has
    # partial file excerpts, so caller-impact analysis must not run (architecture
    # half may still proceed).
    side_on = env_flag_enabled(_SIDE_ENV) and not input_data.pre_numbered
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
    prompt = _build_prompt(
        index,
        arch_body,
        budgets.max_inline_code_chars,
        max_architecture_chars=budgets.max_architecture_chars,
        max_manifest_chars=budgets.max_manifest_chars,
        arch_on=arch_on,
        side_on=side_on,
    )
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=_build_merged_pass_tools(index, side_on=side_on),
    )
    raw = str(agent(prompt)).strip()
    data = json.loads(raw)
    # Validate each half independently. A malformed / missing key on one side
    # must not discard valid findings from the other (the Agent+tools path
    # cannot use ``complete_validated``'s corrective retry the way chunk review
    # does, so per-half salvage matches the standalone passes' ``_parse_findings``
    # posture instead of all-or-nothing ``MergedArchitectureSideEffectResponse``
    # validation).
    architecture_findings: List[CodeReviewIssue] = []
    side_effect_findings: List[CodeReviewIssue] = []
    if not isinstance(data, dict):
        raise TypeError(f"merged pass expected a JSON object, got {type(data).__name__}")
    if arch_on:
        architecture_findings = _issues_from_half(
            data.get("architecture_findings"),
            parse=arch_pass.parse_findings,
            validate=arch_pass.validate_findings,
            index=index,
            pre_numbered=input_data.pre_numbered,
        )
    if side_on:
        side_effect_findings = _issues_from_half(
            data.get("side_effect_findings"),
            parse=side_pass.parse_findings,
            validate=side_pass.validate_findings,
            index=index,
            pre_numbered=input_data.pre_numbered,
        )
    if architecture_findings or side_effect_findings:
        logger.info(
            "MergedArchitectureSideEffectPass: found %s architecture and %s side-effect finding(s)",
            len(architecture_findings),
            len(side_effect_findings),
        )
    return architecture_findings, side_effect_findings


def _manifest_chars(paths: List[str]) -> int:
    """Char count of the changed-file path list as emitted in the user prompt.

    Postconditions: returns ``>= 0``; one newline per path plus the section header.
    """
    header = f"**Changed files in this submission ({len(paths)}):**\n"
    return len(header) + sum(len(p) + 1 for p in paths)


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
    def list_changed_files() -> str:
        """List every changed file path in this submission only.

        Unlike ``list_files()``, this never includes repository paths outside
        the submission. Use it when the changed-file manifest in the prompt was
        truncated, then ``read_file(path)`` for any omitted path.

        Returns:
            One submission path per line, or a message when none are available.
        """
        try:
            paths = list(index.files.keys())
            return "\n".join(paths) if paths else "(no changed files)"
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
          first, else ``LLM_MAX_TOKENS``, else unset ``0``) differs from that
          reserve — raising tight caps and clamping oversized / unset provider
          defaults so the completion cannot exceed the input budget.
        - Injected non-``LLMClientModel`` test models are returned unchanged.
        - Never mutates ``model``.
    """
    if not isinstance(model, LLMClientModel):
        return model
    configured = resolve_max_tokens()
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
) -> str:
    """Render the single user prompt for the merged pass.

    Preconditions:
        - ``architecture_body`` is the flattened document text (empty when the
          architecture half is off or no document was provided).
        - Budget ints are ``>= 0`` from :func:`compute_code_review_merged_pass_budgets`.
        - At least one of ``arch_on`` / ``side_on`` is True.

    Postconditions:
        - Omits the architecture section when ``arch_on`` is False.
        - Truncates the changed-file path manifest to ``max_manifest_chars`` with
          a tool-reachable overflow note when needed.
        - Inlines changed-file bodies up to ``max_inline_chars``, deducting
          per-file heading/fence wrappers from that allowance.
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

    parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(changed_files):
        if remaining <= 0:
            omitted = len(changed_files) - i
            break
        heading = f"### {path} ###"
        # Fence length is content-dependent; reserve a small worst-case fence so
        # heading + fences never push the block past ``remaining``.
        fence_reserve = 8
        base_overhead = len(heading) + 1 + 2 * (fence_reserve + 1)
        # Reserve the truncation note whenever the file may not fit in full —
        # otherwise body consumes the remainder and the appended note overflows,
        # causing this branch to drop the file entirely.
        note_reserve = (
            len(
                f"(Only the first {remaining} characters of `{path}` are shown above; call "
                "read_file to see the rest.)"
            )
            + 1
        )
        if base_overhead >= remaining:
            omitted = len(changed_files) - i
            break
        if base_overhead + len(content) <= remaining:
            overhead = base_overhead
            body = content
            include_note = False
        else:
            overhead = base_overhead + note_reserve
            if remaining <= overhead:
                omitted = len(changed_files) - i
                break
            body = content[: remaining - overhead]
            include_note = True
        body_fence = code_fence_for(body)
        block_lines = [heading, body_fence, body, body_fence]
        if include_note:
            block_lines.append(
                f"(Only the first {len(body)} characters of `{path}` are shown above; call "
                "read_file to see the rest.)"
            )
        block = "\n".join(block_lines)
        if len(block) > remaining:
            # Actual fence longer than reserve: shrink the body rather than drop
            # the file (and every subsequent file) when a prefix would still fit.
            excess = len(block) - remaining
            if include_note and len(body) > excess:
                body = body[: len(body) - excess]
                body_fence = code_fence_for(body)
                block_lines = [
                    heading,
                    body_fence,
                    body,
                    body_fence,
                    (
                        f"(Only the first {len(body)} characters of `{path}` are shown above; call "
                        "read_file to see the rest.)"
                    ),
                ]
                block = "\n".join(block_lines)
            if len(block) > remaining:
                omitted = len(changed_files) - i
                break
        parts.extend(block_lines)
        remaining -= len(block) + 1  # +1 for the join newline before the next block
    if omitted:
        parts.append(
            f"... and {omitted} more changed file(s) not shown above; call "
            "list_changed_files()/read_file(path) to see them."
        )
    parts.append("")

    if arch_on and side_on:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "search_repository()/find_function_at_line() as each part's instructions "
            "require. Address Part 1 and Part 2 independently. Prefer "
            "list_changed_files() when recovering omitted submission paths."
        )
    elif arch_on:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "find_function_at_line() as Part 1 requires. Prefer list_changed_files() "
            "when recovering omitted submission paths. Do not run side-effect analysis."
        )
    else:
        parts.append(
            "Use list_changed_files()/list_files()/read_file()/search_codebase()/"
            "search_repository()/find_function_at_line() as Part 2 requires. Prefer "
            "list_changed_files() when recovering omitted submission paths. Do not run "
            "architecture analysis."
        )
    parts.append(
        'Return a single JSON object with "architecture_findings"/"side_effect_findings" '
        "keys as instructed. Return "
        '{"architecture_findings": [], "side_effect_findings": []} if neither part finds anything.'
    )
    return "\n".join(parts)


def _render_manifest(paths: List[str], max_manifest_chars: int) -> List[str]:
    """Render the changed-file path list, truncated to ``max_manifest_chars``.

    Postconditions:
        - Always includes the section header.
        - When the full list exceeds the budget, includes as many paths as fit
          and a tool-reachable overflow note for the rest.
    """
    header = f"**Changed files in this submission ({len(paths)}):**"
    lines: List[str] = [header]
    used = len(header) + 1
    shown = 0
    for path in paths:
        line_cost = len(path) + 1
        overflow_note = (
            f"... and {len(paths) - shown} more changed path(s) not listed; "
            "call list_changed_files() then read_file(path) to reach them."
        )
        # Leave room for an overflow note if this isn't the last path.
        need_note_room = shown + 1 < len(paths)
        room_for_note = len(overflow_note) + 1 if need_note_room else 0
        if used + line_cost + room_for_note > max_manifest_chars and shown > 0:
            lines.append(
                f"... and {len(paths) - shown} more changed path(s) not listed; "
                "call list_changed_files() then read_file(path) to reach them."
            )
            break
        if used + line_cost > max_manifest_chars and shown == 0:
            # Budget too tight for even one path: header + overflow only.
            lines.append(
                f"... and {len(paths)} more changed path(s) not listed; "
                "call list_changed_files() then read_file(path) to reach them."
            )
            break
        lines.append(path)
        used += line_cost
        shown += 1
    return lines
