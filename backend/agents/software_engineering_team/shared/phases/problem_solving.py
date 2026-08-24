"""
Shared Problem-solving-phase leaf helpers for the code-v2 teams.

Holds the code that was common (byte-identical or divergent only in logging /
language-conventions injection) between the backend and frontend problem-solving
phases: the code/issue formatters, the batch-fix pass, the one-issue-at-a-time
fix loop, the tool-agent problem-solve application, and the two top-level
``run_problem_solving`` / ``run_problem_solving_for_microtask`` orchestrations.

Team-local models are injected via the team's ``models`` module and the
stack-specific conventions / prompt-slot behavior via its
:class:`~software_engineering_team.shared.stack_profile.StackProfile`. The
phase-specific fix functions (``run_code_review_fixes``/``run_qa_fixes``/
``run_security_fixes``/``run_documentation_fixes``, which interlock with each
stack's review-gate phases) are also generic here, parametrized by
:func:`make_phase_fix_functions`; each stack's ``problem_solving.py`` binds
them with its own profile/models/prompt/tool-agent-map and re-exports the
resulting four callables.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner
from shared.dev_models.models import Task
from software_engineering_team.shared.code_completeness import reject_invalid_python
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile

logger = logging.getLogger(__name__)

MAX_ITERATIONS_PER_ISSUE = 5

MAX_BATCH_FIX_CODE_CHARS = 60_000  # Cap context to avoid blowing up the LLM context window


def _fill_named_placeholders(template: str, **values: object) -> str:
    """Replace exact ``{name}`` tokens in one pass; leave all other braces untouched.

    Substitutions are taken from the original ``template`` only: text inserted
    for one key is never rescanned for later keys, so a value that happens to
    contain ``{other_key}`` is preserved literally.

    Preconditions:
        ``template`` is a str; each value is convertible via ``str(...)``.
    Postconditions:
        Every ``{key}`` for a provided key is replaced with ``str(value)``;
        braces that are not exact provided keys remain; returns a new string.
    """
    assert isinstance(template, str), "template must be a str"
    if not values:
        return template
    mapping = {key: str(value) for key, value in values.items()}
    # Longer keys first so a token like ``{foo_bar}`` is not partially claimed
    # by a shorter ``{foo}`` if both were ever provided.
    pattern = re.compile(
        "|".join(re.escape("{" + key + "}") for key in sorted(mapping, key=len, reverse=True))
    )

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return mapping[token[1:-1]]

    return pattern.sub(_replace, template)


def _attr_or(obj: Any, name: str, default: Any) -> Any:
    """Return attribute ``name`` unless it is ``None`` (empty string is kept).

    Preconditions:
        ``name`` is a non-empty str.
    Postconditions:
        Returns ``default`` only when the attribute is missing or ``None``.
    """
    assert isinstance(name, str) and name, "name must be a non-empty str"
    value = getattr(obj, name, None)
    return default if value is None else value


_ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "medium"})
# Dedicated flag on fixes_applied entries — do not overload free-form ``fix`` text.
_ADVISORY_KEY = "advisory"
# Logging bound only — the returned ``summary`` field stays full-length.
_LOG_SUMMARY_MAX_CHARS = 120


def _issue_severity(issue: Any) -> str:
    """Severity used for actionability decisions.

    Preconditions:
        ``issue`` may expose a ``severity`` attribute.
    Postconditions:
        Returns the severity string; ``None`` or empty string becomes ``"medium"``
        (matching the historical ``severity or "medium"`` filter semantics).
    """
    value = getattr(issue, "severity", None)
    if value is None or value == "":
        return "medium"
    return value


def _is_actionable_issue(issue: Any) -> bool:
    """True when the issue's severity is critical/high/medium (empty → medium)."""
    return _issue_severity(issue) in _ACTIONABLE_SEVERITIES


def _applied_fix_count(fixes_applied: List[Dict[str, Any]]) -> int:
    """Count entries that represent applied fixes, not advisory recommendations.

    Preconditions:
        ``fixes_applied`` is a list of dicts.
    Postconditions:
        Returns the number of entries that are not marked ``advisory=True``.
    """
    return sum(1 for entry in fixes_applied if not entry.get(_ADVISORY_KEY))


def _format_summary_for_log(summary: object, max_chars: int = _LOG_SUMMARY_MAX_CHARS) -> str:
    """Collapse whitespace and bound length for a single log line.

    Preconditions:
        ``max_chars`` > 0.
    Postconditions:
        Returns a single-line string of length ≤ ``max_chars``. The caller's
        full ``summary`` value is left unchanged — this is log-only.
    """
    assert max_chars > 0, "max_chars must be greater than 0"
    compact = " ".join(str(summary).split())
    if len(compact) <= max_chars:
        return compact
    if max_chars == 1:
        return "…"
    return compact[: max_chars - 1] + "…"


def _format_all_code(
    current_files: Dict[str, str], max_chars: int = MAX_BATCH_FIX_CODE_CHARS
) -> str:
    """Format current files for batch fix prompt, truncating to stay within budget.

    Preconditions:
        ``current_files`` maps paths to content; ``max_chars`` > 0.
    Postconditions:
        Returns a code block whose length is ≤ ``max_chars``, counting the
        separators inserted by joining parts (stops at the first file that
        cannot fit, optionally appending a truncation marker when the marker
        itself fits); ``"(no code)"`` when empty. Pure.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than 0")
    parts: List[str] = []
    total = 0
    for path, content in current_files.items():
        chunk = f"--- {path} ---\n{content}\n"
        # ``"\n".join(parts)`` inserts one separator char between existing parts.
        sep = 1 if parts else 0
        if total + sep + len(chunk) > max_chars:
            marker = f"--- {path} --- (truncated, {len(content)} chars omitted)\n"
            if total + sep + len(marker) <= max_chars:
                parts.append(marker)
            break
        parts.append(chunk)
        total += sep + len(chunk)
    out = "\n".join(parts) if parts else "(no code)"
    assert out == "(no code)" or len(out) <= max_chars
    return out


def _format_issues_for_batch(issues: List[Any]) -> str:
    """Format all issues into a numbered list for the batch fix prompt.

    Preconditions:
        ``issues`` is a list of review issues with the usual attributes.
    Postconditions:
        Returns a numbered, human-readable block. Pure. Empty-string attributes
        are preserved (only ``None``/missing uses the documented default).
    """
    lines: List[str] = []
    for idx, issue in enumerate(issues, 1):
        lines.append(f"### Issue {idx}")
        lines.append(f"- **Source:** {_attr_or(issue, 'source', 'review')}")
        lines.append(f"- **Severity:** {_issue_severity(issue)}")
        lines.append(f"- **File:** {_attr_or(issue, 'file_path', 'N/A')}")
        lines.append(f"- **Description:** {_attr_or(issue, 'description', 'No description')}")
        lines.append(f"- **Recommendation:** {_attr_or(issue, 'recommendation', 'Fix the issue.')}")
        lines.append("")
    return "\n".join(lines)


def _relevant_code_for_issue(issue: Any, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files.

    Preconditions:
        ``issue`` has a ``file_path``; ``current_files`` maps paths to content.
    Postconditions:
        Returns the issue's file when known, else the first ≤10 files, else
        ``"(no code)"``. Pure.
    """
    issue_file_path = getattr(issue, "file_path", None)
    if issue_file_path and issue_file_path in current_files:
        content = current_files[issue_file_path]
        return f"--- {issue_file_path} ---\n{content}"
    # Fallback: include first files
    parts: List[str] = []
    for path, content in list(current_files.items())[:10]:
        parts.append(f"--- {path} ---\n{content}\n")
    return "\n".join(parts) if parts else "(no code)"


def run_batch_coding_fixes_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    issues: List[Any],
    current_files: Dict[str, str],
    language: str,
    task_id: str,
    phase_name: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: PhaseModels,
    batch_fix_prompt: str,
    parse_batch_fix_template: Callable[[str], Dict[str, Any]],
    runner: LlmRunner,
) -> Any:
    """Fix ALL issues from a review phase in a single batch.

    Instead of fixing issues one at a time, this sends all issues to the coding
    agent at once, allowing it to decide how to organize the fixes internally.

    Only issues with severity ``critical``, ``high``, or ``medium`` are
    considered actionable; ``low``/``info`` issues are silently ignored and
    will not be included in the batch fix.

    Preconditions:
        ``batch_fix_prompt`` has a ``{language_conventions}`` slot (both stacks
        do); ``models`` exposes ``ProblemSolvingResult``; ``profile`` supplies the
        language conventions.
    Postconditions:
        Returns a ``ProblemSolvingResult``; on LLM or parse failure returns an
        unresolved result carrying the actionable issues. A ``.py`` rewrite that
        fails to parse (see :func:`~software_engineering_team.shared.code_completeness.reject_invalid_python`)
        is discarded -- the prior version of that file is kept in ``files`` --
        and any issue whose ``file_path`` matches a rejected file stays in
        ``unresolved_issues`` even if the LLM reported it as addressed.
        ``files`` is always a fresh dict (never the caller's input object).
    """
    problem_solving_result_cls = models.ProblemSolvingResult

    microtask_id = microtask.id
    actionable = [i for i in issues if _is_actionable_issue(i)]

    if not actionable:
        logger.info("[%s] Batch fix for %s: no actionable issues.", task_id, phase_name)
        return problem_solving_result_cls(
            resolved=True,
            files=dict(current_files),
            summary=f"No actionable {phase_name} issues to fix.",
        )

    lang_conv = profile.conventions_for(language)

    logger.info(
        "[%s] Microtask %s: batch fixing %d %s issues. Sending all issues to coding agent.",
        task_id,
        microtask_id,
        len(actionable),
        phase_name,
    )

    if detail_callback:
        detail_callback(f"Fixing all {len(actionable)} {phase_name} issues in batch...")

    formatted_issues = _format_issues_for_batch(actionable)
    current_code = _format_all_code(current_files)

    prompt = _fill_named_placeholders(
        batch_fix_prompt,
        language_conventions=lang_conv,
        issue_count=len(actionable),
        phase_name=phase_name,
        formatted_issues=formatted_issues,
        current_code=current_code,
    )

    try:
        raw = runner.run(llm, prompt)
    except Exception as exc:
        logger.error(
            "[%s] Microtask %s: batch fix LLM call failed: %s",
            task_id,
            microtask_id,
            exc,
        )
        return problem_solving_result_cls(
            resolved=False,
            files=dict(current_files),
            summary=f"Batch fix failed: {exc}",
            unresolved_issues=actionable,
        )

    try:
        parsed = parse_batch_fix_template(raw)
    except Exception as exc:
        logger.error(
            "[%s] Microtask %s: batch fix output parsing failed: %s",
            task_id,
            microtask_id,
            exc,
        )
        return problem_solving_result_cls(
            resolved=False,
            files=dict(current_files),
            summary=f"Batch fix failed: could not parse LLM output: {exc}",
            unresolved_issues=actionable,
        )

    if not isinstance(parsed, dict):  # defensive: a malformed parser result must not crash
        parsed = {}
    fixed_files_raw = parsed.get("files")
    fixed_files = fixed_files_raw if isinstance(fixed_files_raw, dict) else {}
    if fixed_files_raw is not None and not isinstance(fixed_files_raw, dict):
        logger.warning(
            "[%s] Microtask %s: batch fix returned non-dict files; ignoring.",
            task_id,
            microtask_id,
        )
    issues_addressed_raw = parsed.get("issues_addressed")
    issues_addressed = issues_addressed_raw if isinstance(issues_addressed_raw, list) else []
    summary = str(parsed.get("summary") or f"Batch fixed {len(fixed_files)} file(s)")

    fixed_files, rejected_files = reject_invalid_python(fixed_files)
    if rejected_files:
        logger.warning(
            "[%s] Microtask %s: batch fix returned unparsable Python for %d file(s); "
            "discarding and keeping the prior version: %s",
            task_id,
            microtask_id,
            len(rejected_files),
            sorted(rejected_files),
        )
        summary += (
            f" ({len(rejected_files)} file(s) rejected: unparsable Python, kept prior version)"
        )

    merged = dict(current_files)
    merged.update(fixed_files)

    unresolved_issues: List[Any] = []
    unresolved_indices: set = set()
    addressed_indices: set = set()
    for item in issues_addressed:
        if not isinstance(item, dict):
            # Defensive: the LLM may emit a non-dict entry (e.g. a bare
            # string) which has no ``.get`` — skip it rather than crash.
            continue
        try:
            idx = int(item.get("issue_index", 0)) - 1
            if 0 <= idx < len(actionable):
                addressed_indices.add(idx)
        except (ValueError, TypeError):
            pass
    addressed_count = len(addressed_indices)
    for idx, issue in enumerate(actionable):
        if idx not in addressed_indices:
            unresolved_issues.append(issue)
            unresolved_indices.add(idx)

    # A rejected file's issue must stay unresolved even if the LLM claimed to
    # have addressed it -- the merge kept the prior (unfixed) version. Tracked
    # by position in ``actionable`` rather than ``id(issue)``: ``ReviewIssue``
    # is an unhashable, value-equal Pydantic model with no unique id/key
    # field, so object identity would silently misbehave if issues were ever
    # copied/reconstructed, while the list index is stable for the duration
    # of this call and distinguishes duplicate-content issues correctly.
    if rejected_files:
        for idx, issue in enumerate(actionable):
            if (
                getattr(issue, "file_path", None) in rejected_files
                and idx not in unresolved_indices
            ):
                unresolved_issues.append(issue)
                unresolved_indices.add(idx)

    resolved = len(unresolved_issues) == 0

    logger.info(
        "[%s] Microtask %s: batch fix complete. %d files updated, %d/%d issues addressed.",
        task_id,
        microtask_id,
        len(fixed_files),
        addressed_count,
        len(actionable),
    )

    if detail_callback:
        detail_callback(f"Batch fix complete: {addressed_count}/{len(actionable)} issues addressed")

    return problem_solving_result_cls(
        resolved=resolved,
        files=merged,
        summary=summary,
        fixes_applied=[
            {
                "microtask": microtask_id,
                "phase": phase_name,
                "batch_size": len(actionable),
                "addressed": addressed_count,
            }
        ],
        unresolved_issues=unresolved_issues,
    )


def _fix_issues_one_at_a_time_impl(
    *,
    llm: LLMClient,
    actionable: List[Any],
    current_files: Dict[str, str],
    lang_conv: str,
    task_id: str,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    has_language_conventions: bool,
    runner: LlmRunner,
    microtask_id: str = "",
    phase_name: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> tuple[Dict[str, str], List[Dict[str, Any]], List[Any]]:
    """Fix each actionable issue in isolation, up to MAX_ITERATIONS_PER_ISSUE attempts.

    Shared inner loop behind ``run_problem_solving``,
    ``run_problem_solving_for_microtask`` and the backend phase-fix functions. The
    callers differ only in logging labels and which keys each ``fixes_applied``
    entry carries (``microtask``/``phase``).

    Preconditions:
        ``actionable`` is already filtered to actionable severities;
        ``single_issue_prompt`` carries a ``{language_conventions}`` slot iff
        ``has_language_conventions``.
    Postconditions:
        Returns ``(merged_files, fixes_applied, unresolved_issues)``;
        ``merged_files`` is a fresh dict (never the caller's input object). A
        ``.py`` rewrite that fails to parse is discarded per-attempt (the
        prior version of that file is kept); if the discarded file is the
        current issue's own ``file_path``, that attempt is never counted as a
        resolution -- even if the LLM's response claimed ``resolved`` -- and
        the loop retries up to ``MAX_ITERATIONS_PER_ISSUE``.
    """
    assert has_language_conventions == ("{language_conventions}" in single_issue_prompt), (
        "single_issue_prompt must contain {language_conventions} iff has_language_conventions"
    )

    merged = dict(current_files)
    fixes_applied: List[Dict[str, Any]] = []
    unresolved_issues: List[Any] = []

    default_source = phase_name or "review"
    default_recommendation = f"Fix the {phase_name} issue." if phase_name else "Fix the issue."
    mt_ctx = f"Microtask {microtask_id}: " if microtask_id else ""
    phase_ctx = f"{phase_name} " if phase_name else ""

    for issue_idx, issue in enumerate(actionable):
        desc_short = _attr_or(issue, "description", "")
        if detail_callback:
            detail_callback(
                f"Fixing {phase_ctx}issue {issue_idx + 1}/{len(actionable)}: {desc_short}..."
            )
        logger.info(
            "[%s] %sfixing %sissue %d/%d — %s. Next step -> Attempting fix (up to %d iterations)",
            task_id,
            mt_ctx,
            phase_ctx,
            issue_idx + 1,
            len(actionable),
            desc_short,
            MAX_ITERATIONS_PER_ISSUE,
        )
        working = dict(merged)
        resolved_this = False

        for attempt in range(1, MAX_ITERATIONS_PER_ISSUE + 1):
            relevant_code = _relevant_code_for_issue(issue, working)
            fmt: Dict[str, Any] = dict(
                source=_attr_or(issue, "source", default_source),
                severity=_issue_severity(issue),
                description=_attr_or(issue, "description", ""),
                file_path=_attr_or(issue, "file_path", "N/A"),
                recommendation=_attr_or(issue, "recommendation", default_recommendation),
                current_code=relevant_code,
            )
            if has_language_conventions:
                fmt["language_conventions"] = lang_conv
            prompt = _fill_named_placeholders(single_issue_prompt, **fmt)
            try:
                raw = runner.run(llm, prompt)
            except Exception as exc:
                logger.warning(
                    "[%s] %s%sfix LLM call failed (issue %d, attempt %d): %s",
                    task_id,
                    mt_ctx,
                    phase_ctx,
                    issue_idx + 1,
                    attempt,
                    exc,
                )
                break

            try:
                parsed = parse_single(raw)
                if not isinstance(parsed, dict):  # defensive: malformed parser result
                    parsed = {}
                fixed_files = parsed.get("files") or {}
                had_files = bool(fixed_files)
                rejected_files = {}
                if fixed_files:
                    fixed_files, rejected_files = reject_invalid_python(fixed_files)
            except Exception as exc:
                logger.warning(
                    "[%s] %s%sfix parsing/validation failed (issue %d, attempt %d): %s",
                    task_id,
                    mt_ctx,
                    phase_ctx,
                    issue_idx + 1,
                    attempt,
                    exc,
                )
                continue

            if not had_files:
                if parsed.get("resolved"):
                    resolved_this = True
                break

            if rejected_files:
                logger.warning(
                    "[%s] %s%sfix (issue %d, attempt %d) returned unparsable Python for "
                    "%d file(s); discarding and retrying: %s",
                    task_id,
                    mt_ctx,
                    phase_ctx,
                    issue_idx + 1,
                    attempt,
                    len(rejected_files),
                    sorted(rejected_files),
                )
            if not fixed_files:
                continue

            working.update(fixed_files)
            merged.update(fixed_files)

            # The LLM may claim "resolved" even though the fix for THIS issue's
            # file was rejected above (a mixed response can have other, valid
            # files) -- never trust "resolved" when the issue's own file didn't
            # survive the completeness check, and don't record a fix entry for
            # an attempt that didn't actually land the issue's file; retry
            # instead.
            if getattr(issue, "file_path", None) in rejected_files:
                continue

            entry: Dict[str, Any] = {}
            if microtask_id:
                entry["microtask"] = microtask_id
            if phase_name:
                entry["phase"] = phase_name
            entry["issue"] = desc_short
            entry["fix"] = parsed.get("summary", "updated file(s)")
            entry["root_cause"] = parsed.get("root_cause", "")
            fixes_applied.append(entry)
            if parsed.get("resolved"):
                resolved_this = True
                break

        if not resolved_this:
            unresolved_issues.append(issue)
            logger.warning(
                "[%s] %s%sissue unresolved. Recovery summary: "
                "1) Attempted %d fix iterations, 2) No successful resolution. Issue: %s",
                task_id,
                mt_ctx,
                phase_ctx,
                MAX_ITERATIONS_PER_ISSUE,
                desc_short,
            )

    return merged, fixes_applied, unresolved_issues


def _apply_tool_agents_problem_solve(
    *,
    tool_agents: Dict[Any, Any],
    phase_inp: Any,
    merged: Dict[str, str],
    fixes_applied: List[Dict[str, Any]],
    summary_parts: List[str],
    task_id: str,
    microtask_id: str = "",
) -> None:
    """Run every tool agent's ``problem_solve``; merge files, record recommendations.

    Preconditions:
        ``tool_agents`` maps kinds to agents; ``phase_inp`` is a
        ``ToolAgentPhaseInput``.
    Postconditions:
        Mutates ``merged``, ``fixes_applied`` and ``summary_parts`` in place; a
        failing agent is logged and skipped. A ``.py`` file a tool agent
        returns that fails to parse is discarded before merging -- the prior
        version of that file in ``merged`` is left untouched. Summary parts are
        appended when the agent returns files and/or recommendations. File rewrites append a counted applied-fix entry; recommendation-only entries set ``advisory=True`` and are excluded from applied-fix counts.
    """
    for kind, agent in tool_agents.items():
        if not hasattr(agent, "problem_solve"):
            continue
        try:
            out = agent.problem_solve(phase_inp)
            tool_summary = f"Tool {kind.value}: {out.summary or 'suggestions applied.'}"
            wrote_files = False
            files_written = 0
            if out.files:
                valid_files, rejected_files = reject_invalid_python(out.files)
                if rejected_files:
                    logger.warning(
                        "[%s] %stool agent %s returned unparsable Python for %d file(s); "
                        "discarding and keeping the prior version: %s",
                        task_id,
                        f"Microtask {microtask_id}: " if microtask_id else "",
                        kind.value,
                        len(rejected_files),
                        sorted(rejected_files),
                    )
                if valid_files:
                    merged.update(valid_files)
                    wrote_files = True
                    files_written = len(valid_files)
            if wrote_files:
                # File-producing tool results are applied fixes (counted), distinct
                # from advisory recommendation entries below.
                file_entry: Dict[str, Any] = {
                    "source": kind.value,
                    "issue": out.summary or f"Tool {kind.value} file updates",
                    "fix": f"updated {files_written} file(s)",
                    "root_cause": "",
                }
                if microtask_id:
                    file_entry["microtask"] = microtask_id
                fixes_applied.append(file_entry)
            if out.recommendations:
                for r in out.recommendations:
                    entry: Dict[str, Any] = {
                        "source": kind.value,
                        "issue": out.summary or f"Tool {kind.value} recommendation",
                        "recommendation": r,
                        "fix": "suggestion noted",
                        "root_cause": "",
                        _ADVISORY_KEY: True,
                    }
                    if microtask_id:
                        entry["microtask"] = microtask_id
                    fixes_applied.append(entry)
            if wrote_files or out.recommendations:
                summary_parts.append(tool_summary)
        except Exception as exc:
            mt_ctx = f"Microtask {microtask_id}: " if microtask_id else ""
            logger.warning(
                "[%s] %stool agent %s problem_solve() failed: %s",
                task_id,
                mt_ctx,
                kind.value,
                exc,
            )


def run_problem_solving_impl(
    *,
    llm: LLMClient,
    task: Task,
    review_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    profile: StackProfile,
    models: PhaseModels,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    runner: LlmRunner,
) -> Any:
    """Analyse review issues and produce fixes, one issue at a time.

    Preconditions:
        ``models`` exposes ``ProblemSolvingResult``, ``Phase``,
        ``ToolAgentPhaseInput``; ``profile`` supplies conventions and the
        prompt-slot flag.
    Postconditions:
        Returns a ``ProblemSolvingResult``; unresolved issues are surfaced for
        the caller to escalate into fix microtasks. ``files`` is always a fresh
        dict. The summary always includes a quantitative base count and any
        tool-agent narrative parts.
    """
    problem_solving_result_cls = models.ProblemSolvingResult
    phase_enum = models.Phase
    phase_input_cls = models.ToolAgentPhaseInput

    task_id = task.id
    review_issues = getattr(review_result, "issues", None) or []
    actionable = [i for i in review_issues if _is_actionable_issue(i)]
    if not actionable:
        logger.info("[%s] Problem-solving: no actionable issues.", task_id)
        return problem_solving_result_cls(
            resolved=True, files=dict(current_files), summary="No actionable issues."
        )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=profile.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        has_language_conventions=profile.has_language_conventions,
        runner=runner,
    )
    summary_parts: List[str] = []

    if tool_agents:
        phase_inp = phase_input_cls(
            phase=phase_enum.PROBLEM_SOLVING,
            repo_path=repo_path,
            spec_context=task.description or "",
            language=language,
            current_files=merged,
            review_issues=review_issues,
            task_title=task.title or "",
            task_description=task.description or "",
        )
        _apply_tool_agents_problem_solve(
            tool_agents=tool_agents,
            phase_inp=phase_inp,
            merged=merged,
            fixes_applied=fixes_applied,
            summary_parts=summary_parts,
            task_id=task_id,
        )

    resolved = len(unresolved_issues) == 0
    base_summary = (
        f"Applied {_applied_fix_count(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    )
    summary = " ".join([base_summary] + summary_parts) if summary_parts else base_summary
    logger.info(
        "[%s] Problem-solving: %s — %s (%d unresolved)",
        task_id,
        "resolved" if resolved else "partial",
        _format_summary_for_log(summary),
        len(unresolved_issues),
    )
    return problem_solving_result_cls(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


def _run_phase_fixes_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    phase_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    task_id: str,
    phase_name: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: PhaseModels,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    runner: LlmRunner,
) -> Any:
    """Fix a single review phase's actionable issues, one at a time.

    Common implementation behind the four phase-specific fix functions
    (``run_code_review_fixes``/``run_qa_fixes``/``run_security_fixes``/
    ``run_documentation_fixes``) a stack exposes via
    :func:`make_phase_fix_functions`. ``tool_agents`` is accepted for
    signature symmetry with :func:`_run_phase_fixes_with_tool_agent_impl` but
    is not consulted here — this function never runs a tool agent's
    ``problem_solve``.

    Preconditions: ``phase_result.issues`` is a list of review issues;
      ``models`` exposes ``ProblemSolvingResult``; ``profile`` supplies the
      language conventions.
    Postconditions: returns a ``ProblemSolvingResult``. When no issue is
      actionable (severity critical/high/medium), returns a resolved,
      unmodified-files result without invoking the fix loop.
    """
    problem_solving_result_cls = models.ProblemSolvingResult

    microtask_id = microtask.id
    actionable = [i for i in phase_result.issues if _is_actionable_issue(i)]
    if not actionable:
        return problem_solving_result_cls(
            resolved=True, files=current_files, summary=f"No actionable {phase_name} issues."
        )

    logger.info(
        "[%s] %s fixes for microtask %s: %d actionable issues",
        task_id,
        phase_name.title(),
        microtask_id,
        len(actionable),
    )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=profile.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        has_language_conventions=profile.has_language_conventions,
        runner=runner,
        microtask_id=microtask_id,
        phase_name=phase_name,
        detail_callback=detail_callback,
    )

    resolved = len(unresolved_issues) == 0
    summary = (
        f"Microtask {microtask_id} {phase_name}: applied {len(fixes_applied)} fix(s); "
        f"{len(unresolved_issues)} unresolved."
    )
    logger.info("[%s] %s", task_id, summary)

    return problem_solving_result_cls(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


def _run_phase_fixes_with_tool_agent_impl(
    *,
    phase_name: str,
    llm: LLMClient,
    microtask: Any,
    phase_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    task_id: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: PhaseModels,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    runner: LlmRunner,
    phase_fix_tool_agent: Dict[str, Any],
) -> Any:
    """Run the generic phase fixes, then let the phase's dedicated tool agent take a pass.

    Preconditions: ``phase_name`` is a key of ``phase_fix_tool_agent``; ``models``
      exposes ``Phase`` and ``ToolAgentPhaseInput`` in addition to
      ``ProblemSolvingResult``.
    Postconditions: returns the phase fix result from the generic per-issue fix
      loop, with the tool agent's file updates merged in when that agent is
      wired, supports ``problem_solve``, and the call succeeds. If the tool
      agent's ``problem_solve`` raises, the exception is logged (with
      traceback), the generic loop's file updates already in ``result.files``
      are preserved unchanged, and ``result.resolved`` is forced to ``False``
      with a note appended to ``result.summary`` describing the failure.
    """
    result = _run_phase_fixes_impl(
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        phase_name=phase_name,
        detail_callback=detail_callback,
        profile=profile,
        models=models,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        runner=runner,
    )

    kind = phase_fix_tool_agent[phase_name]
    if tool_agents and kind in tool_agents:
        agent = tool_agents[kind]
        if hasattr(agent, "problem_solve"):
            try:
                phase_input_cls = models.ToolAgentPhaseInput
                phase_enum = models.Phase
                phase_inp = phase_input_cls(
                    phase=phase_enum.PROBLEM_SOLVING,
                    microtask=microtask,
                    repo_path=repo_path,
                    spec_context=microtask.description or "",
                    language=language,
                    current_files=result.files,
                    review_issues=phase_result.issues,
                    task_title=microtask.title or "",
                    task_description=microtask.description or "",
                    task_id=task_id,
                )
                out = agent.problem_solve(phase_inp)
                if out.files:
                    result.files.update(out.files)
            except Exception as exc:
                logger.exception(
                    "[%s] %s tool agent problem_solve failed: %s", task_id, phase_name, exc
                )
                result.resolved = False
                result.summary += f" (tool-agent fix pass failed: {exc})"

    return result


class PhaseFixFunctions(NamedTuple):
    """The four phase-specific fix callables a stack's ``problem_solving.py`` exposes."""

    run_code_review_fixes: Callable[..., Any]
    run_qa_fixes: Callable[..., Any]
    run_security_fixes: Callable[..., Any]
    run_documentation_fixes: Callable[..., Any]


def make_phase_fix_functions(
    *,
    profile: StackProfile,
    models: PhaseModels,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    runner_factory: Callable[[], LlmRunner],
    phase_fix_tool_agent: Dict[str, Any],
) -> PhaseFixFunctions:
    """Build a stack's ``run_code_review_fixes``/``run_qa_fixes``/
    ``run_security_fixes``/``run_documentation_fixes`` functions.

    "qa" and "security" never consult a tool agent's ``problem_solve``: the QA
    and Security tool agents are review-only (they report findings; fixing
    them is the generic per-issue coding-agent loop's job) on every stack that
    wires this factory. Only the phase names present in ``phase_fix_tool_agent``
    (typically ``code_review`` -> Build Specialist, ``documentation`` ->
    Documentation) get a second, tool-agent-driven fix pass.

    ``runner_factory`` is called once per returned-function invocation (not
    once here) so each call re-reads the caller module's own ``Agent``/
    ``resolve_text_mode_strands_model`` globals -- matching
    ``run_batch_coding_fixes``'s existing ``runner=_llm_runner()`` pattern and
    keeping each stack module's own ``_llm_runner`` the test monkeypatch
    surface (tests patch ``<stack module>.Agent`` /
      ``<stack module>.resolve_text_mode_strands_model``, not this module's).

    A ``language`` argument the caller omits (falsy) defaults to
    ``profile.default_language`` — the same value each function's previous
    hand-written ``language: str = "python"``/``"typescript"`` default
    resolved to.

    Preconditions: ``phase_fix_tool_agent`` contains entries for at least
      ``"code_review"`` and ``"documentation"`` (the two phases with a
      tool-agent-driven second pass); ``single_issue_prompt`` carries a
      ``{language_conventions}`` slot iff ``profile.has_language_conventions``.
    Postconditions: returns a :class:`PhaseFixFunctions` whose four callables
      each accept ``(*, llm, microtask, phase_result, current_files,
      language="", repo_path="", tool_agents=None, task_id="",
      detail_callback=None)`` and return a ``ProblemSolvingResult``.
    """

    def run_code_review_fixes(
        *,
        llm: LLMClient,
        microtask: Any,
        phase_result: Any,
        current_files: Dict[str, str],
        language: str = "",
        repo_path: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_id: str = "",
        detail_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Fix issues from code review phase (build errors, lint issues, code quality)."""
        return _run_phase_fixes_with_tool_agent_impl(
            phase_name="code_review",
            llm=llm,
            microtask=microtask,
            phase_result=phase_result,
            current_files=current_files,
            language=language or profile.default_language,
            repo_path=repo_path,
            tool_agents=tool_agents,
            task_id=task_id,
            detail_callback=detail_callback,
            profile=profile,
            models=models,
            single_issue_prompt=single_issue_prompt,
            parse_single=parse_single,
            runner=runner_factory(),
            phase_fix_tool_agent=phase_fix_tool_agent,
        )

    def run_qa_fixes(
        *,
        llm: LLMClient,
        microtask: Any,
        phase_result: Any,
        current_files: Dict[str, str],
        language: str = "",
        repo_path: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_id: str = "",
        detail_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Fix issues from QA testing phase (bugs, missing tests, quality issues).

        The QA tool agent only reports findings — all fixing here is done by
        the generic per-issue coding-agent loop; there is no second,
        tool-agent-driven fix pass. ``tool_agents`` is accepted for signature
        parity with the other phase-fix functions but unused.
        """
        del tool_agents  # QA is review-only; fixing is the coding agent's job.
        return _run_phase_fixes_impl(
            llm=llm,
            microtask=microtask,
            phase_result=phase_result,
            current_files=current_files,
            language=language or profile.default_language,
            repo_path=repo_path,
            tool_agents=None,
            task_id=task_id,
            phase_name="qa",
            detail_callback=detail_callback,
            profile=profile,
            models=models,
            single_issue_prompt=single_issue_prompt,
            parse_single=parse_single,
            runner=runner_factory(),
        )

    def run_security_fixes(
        *,
        llm: LLMClient,
        microtask: Any,
        phase_result: Any,
        current_files: Dict[str, str],
        language: str = "",
        repo_path: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_id: str = "",
        detail_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Fix issues from security testing phase (vulnerabilities, security best practices).

        The security tool agent only reports findings — all fixing here is
        done by the generic per-issue coding-agent loop; there is no second,
        tool-agent-driven fix pass. ``tool_agents`` is accepted for signature
        parity with the other phase-fix functions but unused.
        """
        del tool_agents  # Security is review-only; fixing is the coding agent's job.
        return _run_phase_fixes_impl(
            llm=llm,
            microtask=microtask,
            phase_result=phase_result,
            current_files=current_files,
            language=language or profile.default_language,
            repo_path=repo_path,
            tool_agents=None,
            task_id=task_id,
            phase_name="security",
            detail_callback=detail_callback,
            profile=profile,
            models=models,
            single_issue_prompt=single_issue_prompt,
            parse_single=parse_single,
            runner=runner_factory(),
        )

    def run_documentation_fixes(
        *,
        llm: LLMClient,
        microtask: Any,
        phase_result: Any,
        current_files: Dict[str, str],
        language: str = "",
        repo_path: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_id: str = "",
        detail_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Fix issues from documentation review phase (missing docs, incomplete comments)."""
        return _run_phase_fixes_with_tool_agent_impl(
            phase_name="documentation",
            llm=llm,
            microtask=microtask,
            phase_result=phase_result,
            current_files=current_files,
            language=language or profile.default_language,
            repo_path=repo_path,
            tool_agents=tool_agents,
            task_id=task_id,
            detail_callback=detail_callback,
            profile=profile,
            models=models,
            single_issue_prompt=single_issue_prompt,
            parse_single=parse_single,
            runner=runner_factory(),
            phase_fix_tool_agent=phase_fix_tool_agent,
        )

    return PhaseFixFunctions(
        run_code_review_fixes=run_code_review_fixes,
        run_qa_fixes=run_qa_fixes,
        run_security_fixes=run_security_fixes,
        run_documentation_fixes=run_documentation_fixes,
    )


def run_problem_solving_for_microtask_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    review_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    task_id: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: PhaseModels,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    runner: LlmRunner,
) -> Any:
    """Fix issues for a single microtask, one issue at a time.

    Similar to :func:`run_problem_solving_impl` but scoped to a single microtask's
    files, enabling per-microtask problem-solving within the review loop.

    Preconditions / Postconditions: see :func:`run_problem_solving_impl`.
    """
    problem_solving_result_cls = models.ProblemSolvingResult
    phase_enum = models.Phase
    phase_input_cls = models.ToolAgentPhaseInput

    microtask_id = microtask.id
    review_issues = getattr(review_result, "issues", None) or []
    actionable = [i for i in review_issues if _is_actionable_issue(i)]
    if not actionable:
        return problem_solving_result_cls(
            resolved=True, files=dict(current_files), summary="No actionable issues."
        )

    logger.info(
        "[%s] Problem-solving for microtask %s: %d actionable issues",
        task_id,
        microtask_id,
        len(actionable),
    )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=profile.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        has_language_conventions=profile.has_language_conventions,
        runner=runner,
        microtask_id=microtask_id,
        detail_callback=detail_callback,
    )
    summary_parts: List[str] = []

    if tool_agents:
        phase_inp = phase_input_cls(
            phase=phase_enum.PROBLEM_SOLVING,
            microtask=microtask,
            repo_path=repo_path,
            spec_context=microtask.description or "",
            language=language,
            current_files=merged,
            review_issues=review_issues,
            task_title=microtask.title or "",
            task_description=microtask.description or "",
            task_id=task_id,
        )
        _apply_tool_agents_problem_solve(
            tool_agents=tool_agents,
            phase_inp=phase_inp,
            merged=merged,
            fixes_applied=fixes_applied,
            summary_parts=summary_parts,
            task_id=task_id,
            microtask_id=microtask_id,
        )

    resolved = len(unresolved_issues) == 0
    base_summary = (
        f"Microtask {microtask_id}: applied {_applied_fix_count(fixes_applied)} fix(s); "
        f"{len(unresolved_issues)} unresolved."
    )
    summary = " ".join([base_summary] + summary_parts) if summary_parts else base_summary
    logger.info("[%s] %s", task_id, _format_summary_for_log(summary))

    return problem_solving_result_cls(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )
