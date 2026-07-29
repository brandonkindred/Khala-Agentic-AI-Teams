"""Merged architecture-consistency + side-effect-impact pass (one LLM call).

Runs both additive whole-submission checks that the in-process coordinator
previously scheduled as two independent Agent calls, in a single pass with
``MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT``. Findings are split back into
the two lists downstream merge/gate logic already expects.

Invariants:

    - **Additive-only, fail-safe.** Never removes or mutates findings the
      caller already has; any setup/LLM/validation failure yields
      ``([], [])``.
    - **Bounded cost.** At most one LLM call per submission when at least
      one of the two env flags is enabled.
    - **``CODE_REVIEW`` profile only.** Same restriction as each standalone
      pass.
    - **Context-aware budgeting.** Changed-file inlining (and, when needed,
      architecture text) is sized for the combined system prompt, full
      architecture document, and dual finding-array response — not the
      map-call code allowance alone.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional, Tuple, Union

from strands import Agent
from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, LLMClientModel
from llm_service.config import resolve_max_tokens
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import (
    CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    compute_code_review_merged_pass_budgets,
)
from software_engineering_team.shared.models import SystemArchitecture

from . import architecture_consistency_pass as arch_pass
from . import side_effect_impact_pass as side_pass
from .architecture_context import render_architecture_context
from .false_positive_filter import CodebaseIndex, code_fence_for
from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue
from .profiles import ReviewProfile
from .prompts import MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
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
          (env flags off, or side-effect disabled by ``pre_numbered`` with
          architecture also off), profile is not ``CODE_REVIEW``, or there are
          no readable files.
        - Otherwise returns two lists of NEW ``CodeReviewIssue``s
          (architecture/refactor and side-effects/documentation respectively),
          each validated like the corresponding standalone pass; never raises.
        - When only one half is enabled, still makes the merged call but returns
          ``[]`` for the disabled half. ``pre_numbered`` forces the side-effect
          half off (same guard as the standalone side-effect pass).
    """
    arch_on = env_flag_enabled(_ARCH_ENV)
    # Mirror ``find_side_effect_impact_issues``: pre-numbered hunk mode only has
    # partial file excerpts, so caller-impact analysis must not run (architecture
    # half may still proceed).
    side_on = env_flag_enabled(_SIDE_ENV) and not input_data.pre_numbered
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
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        return [], []

    model = _with_merged_pass_output_budget(resolve_code_review_model(llm))
    arch_body = _architecture_document_text(input_data.architecture)
    max_arch_chars, max_inline_chars = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=len(arch_body),
        system_prompt_chars=len(MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT),
    )
    prompt = _build_prompt(
        index,
        arch_body,
        max_inline_chars,
        max_architecture_chars=max_arch_chars,
    )
    agent = Agent(
        model=model,
        system_prompt=MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT,
        tools=side_pass.build_side_effect_tools(index),
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


def _with_merged_pass_output_budget(
    model: "Union[LLMClient, _StrandsModel]",
) -> "Union[LLMClient, _StrandsModel]":
    """Raise a tight output cap so both finding arrays can fit.

    Preconditions:
        - ``model`` is the result of :func:`resolve_code_review_model`.

    Postconditions:
        - When ``model`` is an ``LLMClientModel`` and the effective
          ``LLM_MAX_TOKENS`` / pinned ``max_tokens`` is set but below
          ``CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS``, returns a clone with
          that dual-array floor. Otherwise returns ``model`` unchanged
          (injected test models and unset / already-generous caps keep the
          provider default).
        - Never mutates ``model``.
    """
    if not isinstance(model, LLMClientModel):
        return model
    configured = resolve_max_tokens()
    pinned = model.get_config().get("max_tokens")
    pinned_int = pinned if isinstance(pinned, int) and pinned > 0 else 0
    effective = configured if configured > 0 else pinned_int
    if 0 < effective < CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS:
        return model.clone(max_tokens=CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS)
    return model


def _architecture_document_text(architecture: Optional[SystemArchitecture]) -> str:
    """Flatten the optional architecture payload into the inlined body text.

    Postconditions:
        - Returns ``""`` when ``architecture`` is ``None`` or has no document /
          rendered context content.
        - Otherwise returns the same joined body ``_build_prompt`` inlines
          (without fences or section headers).
    """
    if architecture is None:
        return ""
    return "\n\n".join(
        p
        for p in (
            (architecture.architecture_document or "").strip(),
            render_architecture_context(architecture),
        )
        if p
    )


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
) -> str:
    """Render the single user prompt for the merged pass.

    Preconditions:
        - ``architecture_body`` is the flattened document text (may be empty).
        - ``max_architecture_chars`` / ``max_inline_chars`` are ``>= 0`` budgets
          from :func:`compute_code_review_merged_pass_budgets`.

    Postconditions:
        - Inlines architecture document/context when present (truncated to
          ``max_architecture_chars`` when the budget requires it); otherwise
          states that no formal architecture document was provided.
        - Inlines changed files up to ``max_inline_chars``; overflow files are
          named as tool-reachable.
        - Ends with a return instruction containing both response keys so
          DummyLLMClient tests can anchor on the merged call.
    """
    parts: List[str] = []

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
    manifest = [path for path, _ in changed_files]
    parts.append(f"**Changed files in this submission ({len(manifest)}):**")
    parts.extend(manifest)
    parts.append("")

    parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(changed_files):
        if remaining <= 0:
            omitted = len(changed_files) - i
            break
        body = content[:remaining]
        body_fence = code_fence_for(body)
        parts.append(f"### {path} ###")
        parts.append(body_fence)
        parts.append(body)
        parts.append(body_fence)
        if len(body) < len(content):
            parts.append(
                f"(Only the first {len(body)} characters of `{path}` are shown above; call "
                "read_file to see the rest.)"
            )
        remaining -= len(body)
    if omitted:
        parts.append(
            f"... and {omitted} more changed file(s) not shown above; use read_file(path) or "
            "list_files() to see them."
        )
    parts.append("")

    parts.append(
        "Use list_files()/read_file()/search_codebase()/search_repository()/"
        "find_function_at_line() as each part's instructions require. Address "
        "Part 1 and Part 2 independently."
    )
    parts.append(
        'Return a single JSON object with "architecture_findings"/"side_effect_findings" '
        "keys as instructed. Return "
        '{"architecture_findings": [], "side_effect_findings": []} if neither part finds anything.'
    )
    return "\n".join(parts)
