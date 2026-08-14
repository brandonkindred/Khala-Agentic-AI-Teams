"""Scope verification for code-review findings before PR comments are posted.

The chunk reviewer and auxiliary passes often flag defects in unchanged
context. Posting those as PR comments expands ticket scope. This pass tags
findings the verifier cannot confidently call in-scope as ``pre_existing=True``
so ``_partition_review_issues`` routes them to issue proposals instead of
comments.

Posting is fail-closed: unsure / missing / low-confidence verdicts are not
posted. An *ungrounded* out-of-scope verdict is ignored so it cannot strip a
real in-scope finding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence

from strands.agent.conversation_manager import SlidingWindowConversationManager

from llm_service.interface import LLMClient
from shared.env import env_flag_enabled
from software_engineering_team.github_source.pr_review_mapping import is_within_diff
from software_engineering_team.shared.llm import extract_json_from_response

from .false_positive_filter import (
    CodebaseIndex,
    _agent_read_the_cited_file,
    _build_tools,
    _render_finding_block,
)
from .model_resolution import resolve_code_review_verify_model
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import SCOPE_VERIFY_FORMATTING_INSTRUCTIONS, SCOPE_VERIFY_REASONING_SYSTEM_PROMPT
from .via_reasoning import run_agent_via_reasoning

logger = logging.getLogger(__name__)

_FILTER_ENV = "CODE_REVIEW_SCOPE_FILTER"

_CONFIDENT = frozenset({"high", "medium"})
_IN_SCOPE_LABELS = frozenset({"in_scope", "omission"})


@dataclass(frozen=True)
class ScopeVerdict:
    """One scope verdict for a single finding.

    Invariants:
        - ``scope`` is a taxonomy token (``in_scope``, ``omission``,
          ``out_of_scope``, ``unsure``) or empty when unparsed.
        - ``confidence`` is a lowercased token (may be blank).
    """

    scope: str
    confidence: str = ""
    reasoning: str = ""


def apply_scope_verdicts(
    issues: Sequence[CodeReviewIssue],
    *,
    changed_by_path: Mapping[str, Sequence[int]],
    verdicts: Mapping[int, ScopeVerdict],
    grounded: bool,
    preserve_original: Optional[AbstractSet[int]] = None,
) -> List[CodeReviewIssue]:
    """Tag findings for PR posting eligibility from verifier verdicts.

    Preconditions:
        - ``issues`` are genuine reviewer findings (not coverage/safety).
        - ``changed_by_path`` maps paths to 1-based added/modified line numbers.

    Postconditions:
        - Returns a new list of the same length. Findings whose file/line sit
          on an added/modified line are never tagged ``pre_existing``.
        - Indices in ``preserve_original`` keep their original ``pre_existing``.
        - A confident grounded ``out_of_scope`` verdict, an ``unsure``/missing/
          low-confidence verdict, or a low-confidence ``in_scope`` tags
          ``pre_existing=True`` (fail closed for posting).
        - An ungrounded ``out_of_scope`` verdict (``grounded=False``) leaves the
          finding's original ``pre_existing`` flag unchanged.
        - Confident ``in_scope`` / ``omission`` keep ``pre_existing=False``.
        - Never raises.
    """
    changed_sets: Dict[str, set[int]] = {
        path: {int(n) for n in lines} for path, lines in changed_by_path.items()
    }
    keep = preserve_original or frozenset()
    tagged: List[CodeReviewIssue] = []
    for idx, issue in enumerate(issues):
        copy = _copy_issue(issue)
        if idx in keep or is_within_diff(copy, changed_sets):
            tagged.append(copy)
            continue
        verdict = verdicts.get(idx)
        if verdict is not None and verdict.scope == "out_of_scope" and not grounded:
            tagged.append(copy)
            continue
        if (
            verdict is not None
            and verdict.scope in _IN_SCOPE_LABELS
            and verdict.confidence in _CONFIDENT
        ):
            copy.pre_existing = False
            tagged.append(copy)
            continue
        copy.pre_existing = True
        tagged.append(copy)
    return tagged


def apply_scope_verification(
    llm: LLMClient,
    *,
    issues: Sequence[CodeReviewIssue],
    changed_by_path: Mapping[str, Sequence[int]],
    files: Mapping[str, str],
    repo_reader: Any = None,
    input_data: Optional[CodeReviewInput] = None,
) -> List[CodeReviewIssue]:
    """Run the scope verifier and return issues tagged for posting eligibility.

    Preconditions:
        - ``issues`` are genuine reviewer findings.
        - ``files`` is the submission the reviewer saw (may be empty).

    Postconditions:
        - Unscripted ``DummyLLMClient``, a disabled env toggle, empty
          ``changed_by_path``, or empty ``issues`` return copies unchanged.
        - Otherwise off-diff findings are classified; posting is fail-closed
          except that ungrounded out-of-scope verdicts are discarded.
        - Never raises: setup/LLM failure returns copies unchanged.
    """
    snapshot = [_copy_issue(i) for i in issues]
    if not issues or not changed_by_path:
        return snapshot
    if not env_flag_enabled(_FILTER_ENV):
        return snapshot
    if _is_unscripted_dummy(llm):
        return snapshot
    try:
        return _verify_scope(
            llm,
            snapshot,
            changed_by_path,
            files,
            repo_reader,
            input_data,
        )
    except Exception as exc:  # noqa: BLE001 — must never break the review
        logger.warning(
            "ScopeFilter: verification failed during setup (%s: %s); leaving tags unchanged",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return snapshot


def _copy_issue(issue: Any) -> Any:
    """Deep-copy a Pydantic finding; return duck-typed objects unchanged.

    Postconditions: ``CodeReviewIssue`` (and any object with ``model_copy``)
        is cloned; other duck-typed findings are returned as the same object
        so PR-review fakes still work. Never raises.
    """
    copier = getattr(issue, "model_copy", None)
    if callable(copier):
        return copier(deep=True)
    return issue


def _is_unscripted_dummy(llm: Any) -> bool:
    """True for the production dummy harness, not scripted test subclasses.

    Preconditions: ``llm`` may be any object.
    Postconditions: ``True`` iff ``llm`` or ``llm.client`` is exactly
        ``DummyLLMClient`` (not a subclass used as a test stub). Pure.
    """
    from llm_service.clients.dummy import DummyLLMClient

    if type(llm) is DummyLLMClient:
        return True
    inner = getattr(llm, "client", None)
    return type(inner) is DummyLLMClient


def _parse_scope_verdicts(data: object, count: int) -> Dict[int, ScopeVerdict]:
    """Map a verifier reply to ``{index: ScopeVerdict}`` for in-range indices.

    Postconditions: malformed replies yield ``{}``; out-of-range indices are
        dropped; first in-range verdict wins. Never raises.
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}
    verdicts: Dict[int, ScopeVerdict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
            continue
        if not (0 <= raw_index < count) or raw_index in verdicts:
            continue
        scope = str(item.get("scope", "") or "").strip().lower()
        confidence = str(item.get("confidence", "") or "").strip().lower()
        reasoning = str(item.get("reasoning", "") or "").strip()
        verdicts[raw_index] = ScopeVerdict(scope=scope, confidence=confidence, reasoning=reasoning)
    return verdicts


def _format_changed_lines(changed_by_path: Mapping[str, Sequence[int]]) -> str:
    """Render the added/modified line map for the verifier prompt.

    Postconditions: one ``path: line,...`` line per path, paths sorted.
        Never raises.
    """
    lines = []
    for path in sorted(changed_by_path):
        nums = ", ".join(str(n) for n in sorted({int(x) for x in changed_by_path[path]}))
        lines.append(f"- `{path}`: {nums}")
    return "\n".join(lines) if lines else "(none)"


def _build_scope_prompt(
    file_path: str,
    issues: Sequence[CodeReviewIssue],
    changed_by_path: Mapping[str, Sequence[int]],
) -> str:
    """User prompt for one file's (or one off-diff group's) scope verdicts.

    Postconditions: names ``file_path``, inlines the changed-line map, and
        indexes findings; does not inline file bodies. Never raises.
    """
    parts = [
        "**Lines this pull request added or modified:**",
        _format_changed_lines(changed_by_path),
        "",
        f"**File the findings below are about: `{file_path}`.**",
        "Call read_file on that path when it is in the submission. If it is not "
        "listed, call list_files() to see whether it exists; an omission finding "
        "stays in-scope even when the path is absent from the diff.",
        "",
    ]
    for i, issue in enumerate(issues):
        parts.extend(_render_finding_block(i, issue))
        parts.append("")
    return "\n".join(parts)


def _scope_run_was_grounded(agent: Any, index: CodebaseIndex, file_path: str) -> bool:
    """Whether the verifier actually inspected the cited file or the file list.

    Postconditions: True when a successful ``read_file`` of ``file_path``
        occurred, or when ``file_path`` is not in the submission and
        ``list_files`` ran (omission / off-diff case). False otherwise.
        Never raises.
    """
    if index.resolve_path(file_path) is not None:
        return _agent_read_the_cited_file(agent, index, file_path)
    if agent is None:
        return False
    try:
        messages = getattr(agent, "messages", None) or []
    except Exception:  # noqa: BLE001
        return False
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool = block.get("toolUse") or {}
            if isinstance(tool, dict) and tool.get("name") == "list_files":
                return True
    return False


def _verify_scope(
    llm: LLMClient,
    issues: List[CodeReviewIssue],
    changed_by_path: Mapping[str, Sequence[int]],
    files: Mapping[str, str],
    repo_reader: Any,
    input_data: Optional[CodeReviewInput],
) -> List[CodeReviewIssue]:
    """LLM-backed tagging; may raise on model resolution (caller catches).

    Findings already on added lines skip the LLM. Remaining findings are
    grouped by cited path (blank path → one group).
    """
    changed_sets: Dict[str, set[int]] = {
        path: {int(n) for n in lines} for path, lines in changed_by_path.items()
    }
    need_llm: List[int] = [
        idx for idx, issue in enumerate(issues) if not is_within_diff(issue, changed_sets)
    ]
    if not need_llm or not files:
        return apply_scope_verdicts(
            issues, changed_by_path=changed_by_path, verdicts={}, grounded=True
        )

    source = input_data or CodeReviewInput(files=dict(files))
    index = CodebaseIndex.from_input(source, repo_reader=repo_reader)
    model = resolve_code_review_verify_model(llm)

    groups: Dict[str, List[int]] = {}
    for orig_idx in need_llm:
        path = (issues[orig_idx].file_path or "").strip() or "(unknown)"
        groups.setdefault(path, []).append(orig_idx)

    combined: Dict[int, ScopeVerdict] = {}
    preserve: set[int] = set()
    for file_path, orig_indices in groups.items():
        group_issues = [issues[i] for i in orig_indices]
        prompt = _build_scope_prompt(file_path, group_issues, changed_by_path)
        captured: List[Any] = []

        def _on_agent(agent: Any) -> None:
            captured.append(agent)

        data = run_agent_via_reasoning(
            model=model,
            reasoning_prompt=prompt,
            reasoning_system_prompt=SCOPE_VERIFY_REASONING_SYSTEM_PROMPT,
            formatting_instructions=SCOPE_VERIFY_FORMATTING_INSTRUCTIONS,
            parse=extract_json_from_response,
            tools=_build_tools(index),
            reasoning_think=True,
            agent_key="code_review_scope_verify",
            conversation_manager=SlidingWindowConversationManager(should_truncate_results=False),
            on_reasoning_agent=_on_agent,
        )
        reasoning_agent = captured[0] if captured else None
        group_grounded = _scope_run_was_grounded(reasoning_agent, index, file_path)
        parsed = _parse_scope_verdicts(data, len(group_issues))
        for local_idx, verdict in parsed.items():
            orig = orig_indices[local_idx]
            if verdict.scope == "out_of_scope" and not group_grounded:
                preserve.add(orig)
                continue
            combined[orig] = verdict

    return apply_scope_verdicts(
        issues,
        changed_by_path=changed_by_path,
        verdicts=combined,
        grounded=True,
        preserve_original=preserve,
    )
