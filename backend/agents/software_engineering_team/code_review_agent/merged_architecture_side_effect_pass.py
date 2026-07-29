"""Merged architecture-consistency + side-effect-impact pass (one LLM call).

Runs both additive whole-submission checks that the in-process coordinator
previously scheduled as two independent Agent calls, using
``MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`` and
:class:`MergedArchitectureSideEffectResponse`. Findings are split back into
the two lists downstream merge/gate logic already expects.

Invariants:

    - **Additive-only, fail-safe.** Never removes or mutates findings the
      caller already has; any setup/LLM/validation failure yields
      ``([], [])``.
    - **Bounded cost.** At most one LLM call per submission when at least
      one of the two env flags is enabled.
    - **``CODE_REVIEW`` profile only.** Same restriction as each standalone
      pass.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from strands import Agent

from llm_service import LLMClient
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars
from software_engineering_team.shared.models import SystemArchitecture

from . import architecture_consistency_pass as arch_pass
from . import side_effect_impact_pass as side_pass
from .architecture_context import render_architecture_context
from .false_positive_filter import CodebaseIndex, _code_fence_for
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

    model = resolve_code_review_model(llm)
    max_inline_chars = compute_code_review_map_chunk_chars(llm)
    prompt = _build_prompt(index, input_data.architecture, max_inline_chars)
    agent = Agent(
        model=model,
        system_prompt=MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT,
        tools=side_pass._build_side_effect_tools(index),
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
            parse=arch_pass._parse_findings,
            validate=arch_pass._validate_findings,
            index=index,
            pre_numbered=input_data.pre_numbered,
        )
    if side_on:
        side_effect_findings = _issues_from_half(
            data.get("side_effect_findings"),
            parse=side_pass._parse_findings,
            validate=side_pass._validate_findings,
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


def _issues_from_half(
    raw_list: object,
    *,
    parse,
    validate,
    index: CodebaseIndex,
    pre_numbered: bool,
) -> List[CodeReviewIssue]:
    """Coerce one merged-response array into validated ``CodeReviewIssue``s.

    Preconditions:
        - ``parse`` / ``validate`` are the corresponding standalone pass helpers
          (``_parse_findings`` / ``_validate_findings``).

    Postconditions:
        - Returns ``[]`` when ``raw_list`` is missing or not a list (that half
          produced nothing usable) without raising.
        - Otherwise returns the parse+validate result for that half alone.
    """
    if not isinstance(raw_list, list):
        return []
    findings = parse({"findings": raw_list})
    if findings:
        findings = validate(index, findings, pre_numbered=pre_numbered)
    return findings


def _build_prompt(
    index: CodebaseIndex,
    architecture: Optional[SystemArchitecture],
    max_inline_chars: int,
) -> str:
    """Render the single user prompt for the merged pass.

    Postconditions:
        - Inlines architecture document/context when present; otherwise states
          that no formal architecture document was provided.
        - Inlines changed files up to ``max_inline_chars``; overflow files are
          named as tool-reachable.
        - Ends with a return instruction containing both response keys so
          DummyLLMClient tests can anchor on the merged call.
    """
    parts: List[str] = []

    if architecture is None:
        arch_doc = ""
    else:
        arch_doc = "\n\n".join(
            p
            for p in (
                (architecture.architecture_document or "").strip(),
                render_architecture_context(architecture),
            )
            if p
        )
    if arch_doc:
        doc_fence = _code_fence_for(arch_doc)
        parts.append("**Architecture document:**")
        parts.append(doc_fence)
        parts.append(arch_doc)
        parts.append(doc_fence)
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
        body_fence = _code_fence_for(body)
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
