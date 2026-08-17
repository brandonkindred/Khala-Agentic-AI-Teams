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
from software_engineering_team.github_source.pr_review_mapping import (
    _normalize_path,
    format_removed_excerpt,
)
from software_engineering_team.shared.llm import extract_json_from_response

from ._prompt_utils import _cap_context_field, _render_finding_block
from .false_positive_filter import (
    CodebaseIndex,
    _agent_read_the_cited_file,
    _build_tools,
)
from .model_resolution import resolve_code_review_verify_model
from .models import CodeReviewInput, CodeReviewIssue, coerce_line
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


def finding_overlaps_changed_lines(
    issue: Any, changed_by_path: Mapping[str, Sequence[int]]
) -> bool:
    """True when any line in the finding's span sits on an added/modified line.

    Multi-line findings use ``start_line..line`` (inclusive). A finding that
    starts on a changed line and ends on unchanged context is still in-scope.

    Postconditions: False when the path cannot be resolved or no positive line
        span exists. Never raises.
    """
    changed_sets: Dict[str, set[int]] = {
        path: {int(n) for n in lines} for path, lines in changed_by_path.items()
    }
    path = _normalize_path(getattr(issue, "file_path", "") or "", changed_sets)
    if path is None:
        return False
    end = coerce_line(getattr(issue, "line", None))
    start = coerce_line(getattr(issue, "start_line", None))
    if start is None and end is None:
        return False
    if start is None:
        start = end
    if end is None:
        end = start
    assert start is not None and end is not None
    if start > end:
        start, end = end, start
    changed = changed_sets[path]
    return any(n in changed for n in range(start, end + 1))


def _cited_file_has_deletions(issue: Any, removed_by_path: Mapping[str, Sequence[int]]) -> bool:
    """True when the finding's file has at least one deleted old-file line.

    Postconditions: False when the path cannot be resolved. Never raises.
    """
    removed_sets: Dict[str, set[int]] = {
        path: {int(n) for n in lines} for path, lines in removed_by_path.items()
    }
    path = _normalize_path(getattr(issue, "file_path", "") or "", removed_sets)
    return path is not None and bool(removed_sets[path])


def apply_scope_verdicts(
    issues: Sequence[CodeReviewIssue],
    *,
    changed_by_path: Mapping[str, Sequence[int]],
    verdicts: Mapping[int, ScopeVerdict],
    grounded: bool,
    preserve_original: Optional[AbstractSet[int]] = None,
    removed_by_path: Optional[Mapping[str, Sequence[int]]] = None,
) -> List[CodeReviewIssue]:
    """Tag findings for PR posting eligibility from verifier verdicts.

    Preconditions:
        - ``issues`` are genuine reviewer findings (not coverage/safety).
        - ``changed_by_path`` maps paths to 1-based added/modified line numbers.
        - ``removed_by_path`` maps paths to 1-based old-file deleted line numbers.

    Postconditions:
        - Returns a new list of the same length. Each element is a fresh copy
          (``model_copy(deep=True)``) of the corresponding input finding; a
          duck-typed finding without ``model_copy`` is returned unchanged (same
          object). Callers must not rely on reference equality with ``issues``.
          Findings whose span overlaps an added/modified line are never tagged
          ``pre_existing``.
        - Indices in ``preserve_original`` keep their original ``pre_existing``.
        - Off-diff findings on a file that deletes lines keep their original
          ``pre_existing`` when the verdict is missing, unsure, or
          low-confidence — deletion-only diffs have no added-line map, so
          fail-closed unsure would drop every real finding.
        - A confident grounded ``out_of_scope`` verdict, an ``unsure``/missing/
          low-confidence verdict, or a low-confidence ``in_scope`` tags
          ``pre_existing=True`` (fail closed for posting).
        - An ungrounded ``out_of_scope`` verdict (``grounded=False``) leaves the
          finding's original ``pre_existing`` flag unchanged.
        - Confident ``in_scope`` / ``omission`` keep ``pre_existing=False``.
        - Never raises.
    """
    keep = preserve_original or frozenset()
    tagged: List[CodeReviewIssue] = []
    for idx, issue in enumerate(issues):
        copy = _copy_issue(issue)
        if idx in keep or finding_overlaps_changed_lines(copy, changed_by_path):
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
        if _cited_file_has_deletions(copy, removed_by_path or {}) and (
            verdict is None or verdict.scope == "unsure" or verdict.confidence not in _CONFIDENT
        ):
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
    removed_by_path: Optional[Mapping[str, Sequence[int]]] = None,
    patches_by_path: Optional[Mapping[str, str]] = None,
) -> List[CodeReviewIssue]:
    """Run the scope verifier and return issues tagged for posting eligibility.

    Preconditions:
        - ``issues`` are genuine reviewer findings.
        - ``files`` is the submission the reviewer saw (may be empty).

    Postconditions:
        - Unscripted ``DummyLLMClient``, a disabled env toggle, empty
          ``changed_by_path`` *and* empty ``removed_by_path``, or empty
          ``issues`` return copies unchanged.
        - Otherwise off-diff findings are classified; posting is fail-closed
          except that ungrounded out-of-scope verdicts are discarded and
          deletion-only files preserve original tags on unsure/missing.
        - Never raises: setup/LLM failure returns copies unchanged.
    """
    snapshot = [_copy_issue(i) for i in issues]
    removed = removed_by_path or {}
    if not issues or (not changed_by_path and not removed):
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
            removed,
            patches_by_path or {},
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


def _format_removed_lines(removed_by_path: Mapping[str, Sequence[int]]) -> str:
    """Render the deleted old-file line map for the verifier prompt.

    Postconditions: one ``path: line,...`` line per path, paths sorted.
        Never raises.
    """
    lines = []
    for path in sorted(removed_by_path):
        nums = ", ".join(str(n) for n in sorted({int(x) for x in removed_by_path[path]}))
        lines.append(f"- `{path}`: {nums}")
    return "\n".join(lines) if lines else "(none)"


def _render_scope_finding_block(i: int, issue: CodeReviewIssue) -> List[str]:
    """Render one finding, including a multi-line ``start_line..line`` span.

    Postconditions: same fields as ``_render_finding_block``, plus the span
        when ``start_line`` is set. Never raises.
    """
    block = _render_finding_block(i, issue)
    start = coerce_line(getattr(issue, "start_line", None))
    end = coerce_line(getattr(issue, "line", None))
    if start is not None and end is not None and start != end:
        loc = f"{issue.file_path or '(file unknown)'}:{start}-{end}"
        if len(block) > 1:
            block[1] = f"severity: {issue.severity} | category: {issue.category} | location: {loc}"
    return block


def _build_scope_prompt(
    file_path: str,
    issues: Sequence[CodeReviewIssue],
    changed_by_path: Mapping[str, Sequence[int]],
    *,
    removed_by_path: Mapping[str, Sequence[int]],
    patches_by_path: Mapping[str, str],
    input_data: Optional[CodeReviewInput],
) -> str:
    """User prompt for one file's (or one off-diff group's) scope verdicts.

    Postconditions: names ``file_path``, inlines added and removed line maps,
        task/requirements when present, a deleted-line excerpt for this file,
        and indexes findings with their line span. Never raises.
    """
    parts = [
        "**Lines this pull request added or modified (new-file line numbers):**",
        _format_changed_lines(changed_by_path),
        "",
        "**Lines this pull request deleted (old-file line numbers):**",
        _format_removed_lines(removed_by_path),
        "",
    ]
    if input_data is not None:
        desc = _cap_context_field(input_data.task_description or "")
        req = _cap_context_field(input_data.task_requirements or "")
        if desc:
            parts.extend(["**Pull request / task description:**", desc, ""])
        if req:
            parts.extend(["**Task requirements / PR body:**", req, ""])
        ac = [str(x).strip() for x in (input_data.acceptance_criteria or []) if str(x).strip()]
        if ac:
            parts.append("**Acceptance criteria:**")
            parts.extend(f"- {_cap_context_field(item)}" for item in ac)
            parts.append("")
    excerpt = format_removed_excerpt(patches_by_path.get(file_path, ""))
    if excerpt:
        parts.extend(
            [
                f"**Deleted source from `{file_path}`:**",
                excerpt,
                "",
            ]
        )
    parts.extend(
        [
            f"**File the findings below are about: `{file_path}`.**",
            "Call read_file on that path when it is in the submission. If it is not "
            "listed, call list_files() to see whether it exists; an omission finding "
            "stays in-scope even when the path is absent from the diff. A finding about "
            "deleted code is in-scope when the PR removed that code.",
            "",
        ]
    )
    for i, issue in enumerate(issues):
        parts.extend(_render_scope_finding_block(i, issue))
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
    except Exception as exc:  # noqa: BLE001 — grounding check is best-effort, never raises
        logger.debug("ScopeFilter: could not read agent messages for grounding check: %s", exc)
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
    removed_by_path: Mapping[str, Sequence[int]],
    patches_by_path: Mapping[str, str],
) -> List[CodeReviewIssue]:
    """LLM-backed scope tagging for one review's findings.

    Findings whose span overlaps an added/modified line skip the LLM and stay
    postable. Every remaining (off-diff) finding is grouped by its cited path
    (blank path → one ``"(unknown)"`` group), and each group is sent to the
    scope verifier as a single reasoning run over that file.

    Args:
        llm: Client whose configured model backs the verifier run.
        issues: The full, ordered finding list (both on- and off-diff).
        changed_by_path: Path → 1-based added/modified new-file line numbers.
        files: The submission the reviewer saw; empty means no LLM run.
        repo_reader: Optional reader letting the verifier open off-submission
            files (used to confirm omission findings).
        input_data: The reviewer's ``CodeReviewInput`` (task text, acceptance
            criteria); when None a files-only input is synthesized.
        removed_by_path: Path → 1-based old-file deleted line numbers.
        patches_by_path: Path → unified-diff text, for the deleted-line excerpt.

    Preconditions:
        - ``issues`` are genuine reviewer findings (no coverage/safety items).
        - At least one of ``changed_by_path`` / ``removed_by_path`` is non-empty
          (enforced by the caller).

    Postconditions:
        - Returns a new same-length list from ``apply_scope_verdicts``; on-diff
          findings are untouched and off-diff findings are tagged per verdict.
        - An off-diff finding with no submission (empty ``files``) or no off-diff
          findings at all short-circuits to fail-closed tagging with no LLM call.
        - Ungrounded ``out_of_scope`` verdicts are preserved (original tag kept),
          never used to strip a finding.

    Side effects:
        - One ``run_agent_via_reasoning`` call per file group (LLM + tool use).

    Raises:
        - May raise from model resolution or the reasoning run; the public
          ``apply_scope_verification`` wrapper catches and degrades to no-op.
    """
    need_llm: List[int] = [
        idx
        for idx, issue in enumerate(issues)
        if not finding_overlaps_changed_lines(issue, changed_by_path)
    ]
    if not need_llm:
        return apply_scope_verdicts(
            issues,
            changed_by_path=changed_by_path,
            verdicts={},
            grounded=True,
            removed_by_path=removed_by_path,
        )
    if not files:
        return apply_scope_verdicts(
            issues,
            changed_by_path=changed_by_path,
            verdicts={},
            grounded=True,
            removed_by_path=removed_by_path,
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
        prompt = _build_scope_prompt(
            file_path,
            group_issues,
            changed_by_path,
            removed_by_path=removed_by_path,
            patches_by_path=patches_by_path,
            input_data=source,
        )
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
        removed_by_path=removed_by_path,
    )
