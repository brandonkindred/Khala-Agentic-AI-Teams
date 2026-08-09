"""
Shared base class for the code-v2 "review" tool agents.

Both ``frontend_code_v2_team`` and ``backend_code_v2_team`` ship a family of
tool agents (security, testing/QA, accessibility, performance, UX, build
specialist, …) that share the exact same shape:

* ``run`` delegates to ``execute`` (a logging stub),
* ``plan`` returns static recommendations, and
* ``review`` asks an LLM to find issues and parses them (text-template or
  lenient JSON) into :class:`ReviewIssue` objects.

Only a handful of values differ per agent: the labels used in summaries/logs,
the issue ``source`` and accepted source aliases, the review prompt, the code
truncation limits, whether review parses text or JSON, and the single-issue
prompt. :class:`BaseReviewToolAgent` captures the behavior; subclasses set the
differing values as class attributes.

Reviewers only report findings — fixing is the responsibility of the agent
that requested the review (or, when an orchestrator/team-lead requested it,
whichever agent that lead delegates to). Accordingly ``BaseReviewToolAgent``
itself has no fix capability. A separate opt-in, :class:`SingleIssueProblemSolveMixin`,
carries the one-issue-at-a-time self-fix loop for the small number of tool
agents that legitimately own fixing their own findings (currently only the
build specialist, whose "fix" is mechanically re-running the build rather than
a subjective opinion about someone else's code — see that mixin's docstring).

Two behaviors are load-bearing and must be preserved by every subclass:

* The concrete ``agent.py`` keeps a top-level ``from strands import Agent`` so
  tests can ``monkeypatch.setattr(<agent_module>, "Agent", ...)``. ``Agent`` is
  resolved from the *subclass* module via :meth:`LlmToolAgentBase._agent_factory`
  (inherited) so the patch is honored.
* Model resolution is opted in via :class:`~software_engineering_team.shared.llm_tool_agent_base.LlmToolAgentBase`
  (``resolve_models = True``), which lazy-imports
  ``llm_service.strands_model.resolve_strands_model``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from llm_service.interface import LLMError
from software_engineering_team.code_review_agent.profiles import ReviewProfile
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase
from software_engineering_team.shared.v2_models import (
    ReviewIssue,
    ToolAgentOutput,
    ToolAgentPhaseOutput,
)


def _strands_llm_call_errors() -> Tuple[type, ...]:
    """Collect Strands provider/runtime exception types available in this install.

    Preconditions: none (import of ``strands.types.exceptions`` may fail).
    Postconditions: returns a (possibly empty) tuple of exception classes that
    exist on the installed ``strands-agents`` package. Newer symbols such as
    ``ConcurrencyException`` are included only when present, so this module
    still imports under the declared floor (``strands-agents>=1.35``).
    """
    try:
        from strands.types import exceptions as strands_exc
    except ImportError:  # pragma: no cover - strands is a required dependency
        return ()
    names = (
        "ModelThrottledException",
        "ContextWindowOverflowException",
        "MaxTokensReachedException",
        "ConcurrencyException",
        "EventLoopException",
        "ProviderTokenCountError",
    )
    found: List[type] = []
    for name in names:
        cls = getattr(strands_exc, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


# Strands provider/runtime failures that are not subclasses of ``LLMError`` but
# are still routine model-call failures. ``_run_agent`` normalizes these to
# ``LLMError`` so per-issue handlers (e.g. ``problem_solve``) can isolate them
# without catching programming bugs. Built defensively so older SDK installs
# missing newer symbols still load this module.
_STRANDS_LLM_CALL_ERRORS = _strands_llm_call_errors()

# Only these named placeholders are substituted on the one-shot review path.
# Values are never re-scanned, so task text/code may safely contain the
# placeholder tokens or arbitrary braces.
_REVIEW_PLACEHOLDER_RE = re.compile(r"\{(task_description|code)\}")

# Default per-issue context budget for the single-issue fix prompt (subclasses
# may override via class attr). Deliberately generous (on the same scale as
# CODE_REVIEW_ABS_CHUNK_CHARS in context_sizing.py): unlike the review phase's
# read-only code context, this feeds a prompt the model edits, so truncating
# it risks a broken fix — the cap exists only to bound a pathological single
# file, not to restrict normal usage.
DEFAULT_MAX_RELEVANT_CODE_CHARS = 100_000

# Placeholders filled explicitly in ``SingleIssueProblemSolveMixin.problem_solve``.
# ``_problem_solving_kwargs`` must not return these keys (they are dropped with a
# warning if it does), so ``str.format`` cannot raise
# ``TypeError: got multiple values for keyword argument``.
_PROBLEM_SOLVE_RESERVED_KEYS = frozenset(
    {"source", "severity", "description", "file_path", "recommendation", "current_code"}
)


def fill_review_prompt(template: str, *, task_description: str, code: str) -> str:
    """Substitute ``{task_description}`` and ``{code}`` without rescanning values.

    Preconditions:
        ``template``, ``task_description``, and ``code`` are strings.
    Postconditions:
        Each known placeholder is replaced once with the corresponding value.
        Inserted values are never re-scanned for placeholders or braces; other
        ``{...}`` sequences in ``template`` are left unchanged.
    """
    values = {"task_description": task_description, "code": code}
    return _REVIEW_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def relevant_code_for_issue(
    issue: ReviewIssue,
    current_files: Optional[Dict[str, str]],
    max_chars: int = DEFAULT_MAX_RELEVANT_CODE_CHARS,
) -> str:
    """Return code context for a single issue: prefer issue's file, else all files.

    Preconditions: ``current_files`` is ``None`` or maps path -> content.
    Postconditions: returns a non-empty string; when ``max_chars`` is positive
        and content is available, the returned string is bounded by
        ``max_chars``; falls back to ``"(no code)"`` when ``current_files`` is
        empty/``None``, ``max_chars`` is non-positive, or no content can be
        included. Truncated context carries an explicit marker so the fix agent
        does not mistake a prefix for the complete file or submission.
    """
    if max_chars <= 0 or not current_files:
        return "(no code)"
    if issue.file_path and issue.file_path in current_files:
        candidates = [(issue.file_path, current_files[issue.file_path])]
    else:
        candidates = list(current_files.items())
    if not candidates:
        return "(no code)"

    parts: List[str] = []
    used = 0
    truncated = False
    for path, content in candidates:
        chunk = f"--- {path} ---\n{content}\n"
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        parts.append(chunk[:remaining])
        used += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
            break

    rendered = "".join(parts)
    if truncated:
        marker = "\n[truncated; select or retrieve additional file context before editing]"
        rendered = rendered[: max(0, max_chars - len(marker))] + marker[:max_chars]
    return rendered.rstrip("\n") or "(no code)"


def lenient_json_object(
    raw: str, *, logger: logging.Logger, context: str, on_fail_msg: str
) -> Dict[str, Any]:
    """Parse a JSON object from ``raw``, tolerating surrounding prose.

    Mirrors the historical inline fallback: try ``json.loads`` directly, then
    the substring between the first ``{`` and last ``}``; on failure log a
    warning and return ``{}``.

    Preconditions: ``raw`` is a str; ``logger``/``context``/``on_fail_msg`` set.
    Postconditions: returns a dict (``{}`` when no JSON object can be parsed,
        or when the parsed JSON value is not a mapping).
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start:end])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.warning(
                    "%s: model output did not parse as JSON: %r; %s",
                    context,
                    raw,
                    on_fail_msg,
                )
                return {}
        logger.warning(
            "%s: model output contained no JSON object: %r; %s",
            context,
            raw,
            on_fail_msg,
        )
        return {}


class SingleIssueProblemSolveMixin:
    """Opt-in one-issue-at-a-time self-fix loop.

    Reserved for tool agents whose owning phase treats their own findings as
    their own responsibility to fix — currently only
    :class:`~software_engineering_team.shared.tool_agent_build_specialist.BuildSpecialistToolAgentBase`,
    whose "fix" is mechanically re-running the build/tests until they pass,
    not a subjective opinion about another agent's code. Review-lens tool
    agents (security, testing/QA, accessibility, performance, UX) must NOT mix
    this in: per the fix-ownership principle, a reviewer reports issues; the
    agent that requested the review (or its orchestrator/team-lead) is
    responsible for delegating the fix, never the reviewer itself.
    :class:`~software_engineering_team.shared.tool_agent_documentation.DocumentationToolAgentBase`
    defines its own independent ``problem_solve`` and does not use this mixin.

    Preconditions:
        Must be combined with a class supplying ``self._model``,
        ``self._logger``, ``self._run_agent``, ``self._problem_solving_kwargs``,
        ``self.max_relevant_code_chars``, ``self.problem_solving_prompt``,
        ``self._parse_single_issue``, ``self.default_severity``,
        ``self.default_recommendation``, ``self.issue_source``, ``self.name``,
        ``self.empty_label`` — i.e. exactly what :class:`BaseReviewToolAgent`
        supplies.  Subclasses must override :attr:`problem_solve_sources` (a
        tuple of source strings) to select which review issues this agent will
        fix; the default empty tuple means no issues are selected.
    """

    problem_solve_sources: Tuple[str, ...] = ()

    def problem_solve(self, inp) -> ToolAgentPhaseOutput:
        """Fix issues one at a time whose source is in :attr:`problem_solve_sources`.

        Preconditions:
            - ``inp`` exposes ``review_issues`` (each with ``source``,
              ``severity``, ``description``, ``file_path``, ``recommendation``)
              and ``current_files`` (a mapping or ``None``; ``None`` is treated
              as an empty file map).
            - The attributes listed in this mixin's class-level Preconditions
              are supplied by the combining class.
            - ``_problem_solving_kwargs(inp)`` must not return keys in
              ``_PROBLEM_SOLVE_RESERVED_KEYS`` (overlapping keys are dropped
              with a warning rather than raising). ``None`` is treated as ``{}``.

        Postconditions:
            - Returns a :class:`ToolAgentPhaseOutput`. When there is no LLM or
              no matching issues, ``files`` is empty (the default) and
              ``current_files`` is left untouched; otherwise ``files`` is the
              merged file set after applying each successful single-issue fix.
            - An ``LLMError`` / ``TimeoutError`` from the LLM call (including
              routine Strands provider failures normalized to ``LLMError`` by
              :meth:`BaseReviewToolAgent._run_agent`), or a ``ValueError`` /
              ``json.JSONDecodeError`` from ``_parse_single_issue``, is logged
              and skips that issue without aborting the loop. Non-mapping parse
              results are likewise skipped.
            - Any other exception (programming errors) propagates to the caller.

        Invariants:
            - ``inp`` is never modified; changes apply to a local ``merged`` copy.
        """
        if not self._model:
            return ToolAgentPhaseOutput(summary=f"{self.name} problem_solve skipped (no LLM).")
        issues = [
            i for i in inp.review_issues if (i.source or "").strip() in self.problem_solve_sources
        ]
        if not issues:
            return ToolAgentPhaseOutput(summary=f"No {self.empty_label} to fix.")
        extra = dict(self._problem_solving_kwargs(inp) or {})
        overlapping = _PROBLEM_SOLVE_RESERVED_KEYS.intersection(extra)
        if overlapping:
            self._logger.warning(
                "%s _problem_solving_kwargs reserved keys dropped: %s",
                self.name,
                overlapping,
            )
            for key in overlapping:
                del extra[key]
        merged = dict(inp.current_files or {})
        fixed_count = 0
        for issue in issues:
            relevant_code = relevant_code_for_issue(issue, merged, self.max_relevant_code_chars)
            # Keyword ``str.format`` does not re-parse braces inside values, so
            # issue fields and code are passed through unescaped. Escaping here
            # would corrupt the prompt the model edits.
            prompt = self.problem_solving_prompt.format(
                source=issue.source or self.issue_source,
                severity=issue.severity or self.default_severity,
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or self.default_recommendation,
                current_code=relevant_code,
                **extra,
            )
            try:
                raw = self._run_agent(self._model, prompt)
            except (LLMError, TimeoutError) as e:
                self._logger.warning(
                    "%s fix for issue %s failed: %s",
                    self.name,
                    (issue.description or "")[:50],
                    e,
                    exc_info=True,
                )
                continue
            try:
                parsed = self._parse_single_issue(raw)
            except (ValueError, json.JSONDecodeError) as e:
                self._logger.warning(
                    "%s parse for issue %s failed: %s",
                    self.name,
                    (issue.description or "")[:50],
                    e,
                    exc_info=True,
                )
                continue
            if not isinstance(parsed, Mapping):
                self._logger.warning(
                    "%s parse for issue %s returned non-mapping %s; skipping",
                    self.name,
                    (issue.description or "")[:50],
                    type(parsed).__name__,
                )
                continue
            fixed_files = parsed.get("files") or {}
            if fixed_files:
                merged.update(fixed_files)
                fixed_count += 1
        return ToolAgentPhaseOutput(
            files=merged,
            summary=f"{self.name}: fixed {fixed_count} of {len(issues)} issue(s) (one at a time).",
        )


class BaseReviewToolAgent(LlmToolAgentBase):
    """Template for the review tool agents.

    Inherits :class:`~software_engineering_team.shared.llm_tool_agent_base.LlmToolAgentBase`
    for model resolution, LLM invocation, and the ``Agent`` factory. When
    :attr:`resolve_models` / :attr:`use_run_strands_agent` are set (Review
    recipe), those behaviors come from the shared base.

    Fixing is out of scope for this class — see :class:`SingleIssueProblemSolveMixin`
    for the opt-in self-fix loop used by the small number of tool agents that
    legitimately own fixing their own findings.

    Invariants: instance state is limited to ``_model`` (always),
    ``_model_json`` (when :attr:`uses_json_model`), and ``llm`` — so tests that
    build instances via ``__new__`` and set those attributes behave identically
    to constructed instances.
    """

    # --- LlmToolAgentBase Review recipe ---------------------------------
    resolve_models: bool = True
    response_format: str = "text"
    use_run_strands_agent: bool = True
    json_parse_strategy: str = "lenient"
    # review_parse_mode / uses_json_model already declared below as "text" / False

    # --- Labels (subclasses override) ------------------------------------
    name: str = "Tool"  # used for deliver and "<name> review"
    execute_label: Optional[str] = None  # defaults to ``name`` when unset
    empty_label: str = "issues"  # "No <empty_label> to fix."

    # --- Issue routing ----------------------------------------------------
    issue_source: str = ""  # source set on emitted ReviewIssue

    # --- Shared review engine (opt-in) -----------------------------------
    # When True, ``review`` delegates to the shared code-review engine
    # (``code_review_agent.CodeReviewAgent``) with :attr:`review_profile`
    # instead of running this agent's own one-shot ``review_prompt`` — so the
    # gate inherits the engine's chunking, false-positive filtering, and
    # synthesis. Off by default so the specialized-lens agents (security,
    # accessibility, performance, …) keep their own criteria; flip it only on a
    # generic spec/standards reviewer whose criteria the engine's profile
    # already expresses. ``build_runner`` agents are unaffected (they take the
    # build-runner branch first).
    review_via_engine: bool = False
    review_profile: ReviewProfile = ReviewProfile.SPEC_CONFORMANCE

    # --- Prompts / parsing ------------------------------------------------
    review_prompt: Optional[str] = None
    problem_solving_prompt: Optional[str] = None
    max_relevant_code_chars: int = DEFAULT_MAX_RELEVANT_CODE_CHARS
    review_parse_mode: str = "text"  # "text" | "json"
    uses_json_model: bool = False
    review_model_attr: str = "_model"  # "_model" | "_model_json"

    # --- Single-issue fix defaults ---------------------------------------
    default_severity: str = "medium"
    default_recommendation: str = "Fix the issue."

    # --- Stack/discipline profile ----------------------------------------
    # Maps lowercased language name (plus optional ``"_default"``) to a
    # conventions string injected into the single-issue prompt as
    # ``language_conventions``. Empty → no injection (e.g. frontend agents whose
    # single-issue prompt has no ``{language_conventions}`` slot). Backend agents
    # set ``{"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS}``.
    conventions_by_language: Optional[Dict[str, str]] = None

    # When set (as a ``staticmethod``), ``review`` runs this build runner over the
    # resolved ``repo_path`` instead of the LLM review path. The runner takes a
    # ``pathlib.Path`` and returns a list of :class:`ReviewIssue`.
    build_runner: Optional[Callable[[Path], List["ReviewIssue"]]] = None
    # Noun used in the build-review summary, e.g. "build/test issue(s)" (backend)
    # vs "build issue(s)" (frontend).
    build_review_noun: str = "issue(s)"

    # --- Static plan output ----------------------------------------------
    plan_recommendations: Tuple[str, ...] = ()
    plan_summary: str = ""

    # --- Parser hooks (set by subclass as staticmethods) -----------------
    # ``_parse_review`` is required only for ``review_parse_mode == "text"``.
    _parse_review: Optional[Callable[[str], Dict[str, Any]]] = None
    _parse_single_issue: Optional[Callable[[str], Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def _logger(self) -> logging.Logger:
        """Logger named for the concrete subclass module (faithful warnings)."""
        return logging.getLogger(type(self).__module__)

    def _run_agent(self, model, prompt: str) -> str:
        """Invoke the LLM via the shared base path (``run_strands_agent``).

        Kept as a named alias so intermediates (e.g. documentation tool agents)
        that call ``_run_agent`` keep working without edits.

        Preconditions:
            ``use_run_strands_agent`` is True on this class (Review recipe).

        Postconditions:
            Returns the stripped string from ``_invoke_llm``. Routine Strands
            provider/runtime failures listed in ``_STRANDS_LLM_CALL_ERRORS``
            are re-raised as ``LLMError`` so per-issue callers can isolate them;
            all other exceptions propagate unchanged.
        """
        try:
            return self._invoke_llm(model, prompt)
        except _STRANDS_LLM_CALL_ERRORS as e:
            raise LLMError(str(e)) from e

    def _build_code_text(self, current_files: Dict[str, str]) -> str:
        """Render ``current_files`` as ``--- path ---\\ncontent`` blocks, joined for the prompt."""
        return "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_files.items())

    def _problem_solving_kwargs(self, inp) -> Dict[str, Any]:
        """Extra ``.format`` kwargs for the single-issue prompt.

        Driven by :attr:`conventions_by_language`: when it is non-empty, inject
        ``language_conventions`` selected by ``inp.language`` (falling back to the
        ``"_default"`` entry); when empty, inject nothing — this agent's
        single-issue prompt has no ``{language_conventions}`` slot to fill.

        Preconditions: when set, ``conventions_by_language`` maps lowercased
        language names (plus optional ``"_default"``) to convention strings.
        Postconditions: returns ``{}`` or ``{"language_conventions": <str>}``.
            Never returns keys in ``_PROBLEM_SOLVE_RESERVED_KEYS`` (``source``,
            ``severity``, ``description``, ``file_path``, ``recommendation``,
            ``current_code``); callers also filter overlaps defensively.
        """
        conv = self.conventions_by_language or {}
        if not conv:
            return {}
        lang = (getattr(inp, "language", None) or "").strip().lower()
        return {"language_conventions": conv.get(lang, conv.get("_default", ""))}

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------
    def run(self, inp) -> ToolAgentOutput:
        """Run the agent's execute phase.

        Preconditions:
            ``inp`` exposes ``microtask.id`` (see :meth:`execute`).
        Postconditions:
            Returns a ``ToolAgentOutput`` with no code changes applied.
        """
        return self.execute(inp)

    def execute(self, inp) -> ToolAgentOutput:
        """Default execute action for review tool agents; review agents perform work in :meth:`review`.

        Preconditions:
            ``inp`` exposes ``microtask.id``.
        Postconditions:
            Logs at INFO via :attr:`_logger`, then returns a ``ToolAgentOutput``
            indicating no changes were applied.
        """
        label = self.execute_label or self.name
        self._logger.info("%s: microtask %s (execute stub)", label, inp.microtask.id)
        return ToolAgentOutput(summary=f"{label} execute — no changes applied.")

    def plan(self, inp) -> ToolAgentPhaseOutput:
        """Return planning recommendations and summary.

        Preconditions:
            None (``inp`` is accepted for interface parity but unused).
        Postconditions:
            Returns a ``ToolAgentPhaseOutput`` populated verbatim from
            :attr:`plan_recommendations` and :attr:`plan_summary`, independent
            of ``inp``.
        """
        return ToolAgentPhaseOutput(
            recommendations=list(self.plan_recommendations),
            summary=self.plan_summary,
        )

    def _build_review(self, inp) -> ToolAgentPhaseOutput:
        """Run :attr:`build_runner` over ``inp.repo_path`` and report one issue per failure.

        Preconditions: :attr:`build_runner` is set (a ``staticmethod`` taking a
        ``pathlib.Path`` and returning a list of :class:`ReviewIssue`).
        Postconditions: returns a :class:`ToolAgentPhaseOutput`; when ``repo_path``
        is unset/missing the issue list is empty and the summary says "skipped".
        :attr:`build_runner` is called uncaught — any exception it raises is a
        defect and propagates to the caller.  ``build_runner`` implementations
        are responsible for setting each returned ``ReviewIssue.source`` to the
        appropriate value (typically the owning agent's :attr:`issue_source`).
        """
        from pathlib import Path

        review_label = f"{self.name} review"
        if not inp.repo_path:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no repo_path).")
        path = Path(inp.repo_path).resolve()
        if not path.exists():
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (repo path missing).")
        issues = self.build_runner(path)
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"{review_label}: {len(issues)} {self.build_review_noun} found.",
        )

    def _engine_review(self, inp) -> ToolAgentPhaseOutput:
        """Run the shared code-review engine and map its issues to ReviewIssues.

        Preconditions: :attr:`review_via_engine` is set; ``inp.current_files``
        maps path -> content.
        Postconditions: returns a :class:`ToolAgentPhaseOutput` whose ``issues``
        carry this agent's :attr:`issue_source`. Only a ``CodeReviewUnavailableError``
        (the review could not be run) is caught, and degrades to a "(LLM error)"
        summary with no issues; every other exception type is left uncaught and
        propagates unchanged to the caller. Each engine
        ``CodeReviewIssue.suggestion`` becomes the ``ReviewIssue.recommendation``.
        """
        from software_engineering_team.code_review_agent import (
            CodeReviewAgent,
            CodeReviewInput,
            CodeReviewUnavailableError,
        )

        review_label = f"{self.name} review"
        if not inp.current_files:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        try:
            result = CodeReviewAgent(self.llm).run(
                CodeReviewInput(
                    files=dict(inp.current_files),
                    task_description=inp.task_description or "",
                    # Pass the input's language through, or "" — the engine's chunk
                    # reviewer guesses per-chunk when language is empty, which is
                    # safer than forcing a default for non-TypeScript projects.
                    language=getattr(inp, "language", None) or "",
                    profile=self.review_profile,
                )
            )
        except CodeReviewUnavailableError as e:
            self._logger.warning("%s engine review unavailable: %s", review_label, e)
            return ToolAgentPhaseOutput(summary=f"{review_label} failed (LLM error).")
        issues = [
            ReviewIssue(
                source=self.issue_source,
                severity=i.severity,
                description=i.description,
                file_path=i.file_path,
                recommendation=i.suggestion,
            )
            for i in result.issues
        ]
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"{review_label}: {len(issues)} issue(s) found.",
        )

    def review(self, inp) -> ToolAgentPhaseOutput:
        """Find issues in ``inp.current_files``, via the recipe this subclass configured.

        Preconditions: ``inp`` is a ``ToolAgentPhaseInput``-shaped object. When
            neither :attr:`build_runner` nor :attr:`review_via_engine` is set,
            :attr:`review_prompt` must be a non-``None`` template string — the
            base class declares it as ``Optional[str] = None`` for subclasses
            that take one of those other two paths, so a subclass using the
            default one-shot LLM path must set it.
        Postconditions: takes the first configured path -- :attr:`build_runner`
            (a static analysis/build tool), :attr:`review_via_engine` (the
            shared code-review engine), or the LLM one-shot ``review_prompt`` --
            and returns a :class:`ToolAgentPhaseOutput` whose ``issues`` (if
            any) all carry :attr:`issue_source`. Only the default one-shot LLM
            path (neither :attr:`build_runner` nor :attr:`review_via_engine`
            set) degrades a missing model, empty code, or LLM failure to a
            skipped/failed summary with no issues rather than raising.
            ``ValueError`` is raised only when that default path is reached with
            a usable model and non-empty code but ``review_prompt`` is still
            ``None`` (no ``build_runner`` / ``review_via_engine`` either). When
            :attr:`build_runner` or :attr:`review_via_engine` is set, an
            unexpected exception from the runner/engine is a defect and
            propagates uncaught; see :meth:`_build_review` and
            :meth:`_engine_review`.
        """
        if self.build_runner is not None:
            return self._build_review(inp)
        if self.review_via_engine:
            return self._engine_review(inp)
        review_label = f"{self.name} review"
        model = getattr(self, self.review_model_attr, None)
        if self._fallback_no_model(model) is not None:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no LLM).")
        if not getattr(inp, "current_files", None):
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        code_text = self._build_code_text(inp.current_files)
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        if self.review_prompt is None:
            raise ValueError(
                f"{type(self).__name__} has no review_prompt set; subclasses must set "
                "review_prompt, review_via_engine=True, or build_runner when using the "
                "default review() path."
            )
        # Single-pass substitution of the two known placeholders. Values are
        # never re-scanned, so task text/code may contain braces or the
        # placeholder tokens themselves without corruption or duplication.
        prompt = fill_review_prompt(
            self.review_prompt,
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        status, result = self._call_with_single_fallback(
            lambda: self._invoke_llm(model, prompt),
            log_label=review_label,
        )
        if status == "error":
            return ToolAgentPhaseOutput(summary=f"{review_label} failed (LLM error).")
        raw = result
        data = self._parse_llm_json(raw, context=review_label, on_fail_msg="reporting 0 issues.")
        issues: List[ReviewIssue] = []
        for item in (data or {}).get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        source=self.issue_source,
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"{review_label}: {len(issues)} issue(s) found.",
        )

    def deliver(self, inp) -> ToolAgentPhaseOutput:
        """Stub deliver phase: this family of tool agents applies no delivery-time changes.

        Postconditions: returns a placeholder :class:`ToolAgentPhaseOutput`
            summary; never touches ``inp`` or applies any change.
        """
        return ToolAgentPhaseOutput(summary=f"{self.name} deliver.")
