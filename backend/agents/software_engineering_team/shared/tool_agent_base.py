"""
Shared base class for the code-v2 "review + single-issue fix" tool agents.

Both ``frontend_code_v2_team`` and ``backend_code_v2_team`` ship a family of
tool agents (security, testing/QA, accessibility, performance, UX, build
specialist, …) that share the exact same shape:

* ``run`` delegates to ``execute`` (a logging stub),
* ``plan`` returns static recommendations,
* ``review`` asks an LLM to find issues and parses them (text-template or
  lenient JSON) into :class:`ReviewIssue` objects, and
* ``problem_solve`` fixes its own issues one at a time, feeding each issue's
  relevant code into a single-issue prompt and merging the returned files.

Only a handful of values differ per agent: the labels used in summaries/logs,
the issue ``source`` and accepted source aliases, the review prompt, the code
truncation limits, whether review parses text or JSON, and the single-issue
prompt. :class:`BaseReviewToolAgent` captures the behavior; subclasses set the
differing values as class attributes.

Two behaviors are load-bearing and must be preserved by every subclass:

* The concrete ``agent.py`` keeps a top-level ``from strands import Agent`` so
  tests can ``monkeypatch.setattr(<agent_module>, "Agent", ...)``. The base
  resolves ``Agent`` from the *subclass* module (:meth:`_agent_factory`) so the
  patch is honored.
* ``__init__`` resolves the LLM model(s) via ``resolve_strands_model`` imported
  *inside* the method, so tests that monkeypatch the resolver are honored.
"""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_review_agent.profiles import ReviewProfile

from software_engineering_team.shared.v2_models import (
    ReviewIssue,
    ToolAgentOutput,
    ToolAgentPhaseOutput,
)

# Default per-issue context budget (subclasses may override via class attr).
DEFAULT_MAX_RELEVANT_CODE_CHARS = 8_000


def relevant_code_for_issue(
    issue: ReviewIssue,
    current_files: Dict[str, str],
    max_chars: int = DEFAULT_MAX_RELEVANT_CODE_CHARS,
) -> str:
    """Return code context for a single issue: prefer issue's file, else first files.

    Preconditions: ``current_files`` maps path -> content; ``max_chars`` > 0.
    Postconditions: returns a non-empty string; falls back to ``"(no code)"``
    when no content can be included.
    """
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        if len(content) <= max_chars:
            return f"--- {issue.file_path} ---\n{content}"
        return f"--- {issue.file_path} ---\n{content[:max_chars]}\n... [truncated]"
    parts: List[str] = []
    total = 0
    for path, content in list(current_files.items())[:10]:
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                chunk = f"--- {path} ---\n{content[:remaining]}\n... [truncated]"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else "(no code)"


def lenient_json_object(
    raw: str, *, logger: logging.Logger, context: str, on_fail_msg: str
) -> Dict[str, Any]:
    """Parse a JSON object from ``raw``, tolerating surrounding prose.

    Mirrors the historical inline fallback: try ``json.loads`` directly, then
    the substring between the first ``{`` and last ``}``; on failure log a
    warning and return ``{}``.

    Preconditions: ``raw`` is a str; ``logger``/``context``/``on_fail_msg`` set.
    Postconditions: returns a dict (``{}`` when no JSON object can be parsed).
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end])
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


class BaseReviewToolAgent:
    """Template for the review + single-issue-fix tool agents.

    Invariants: instance state is limited to ``_model`` (always),
    ``_model_json`` (when :attr:`uses_json_model`), and ``llm`` — so tests that
    build instances via ``__new__`` and set those attributes behave identically
    to constructed instances.
    """

    # --- Labels (subclasses override) ------------------------------------
    name: str = "Tool"  # used for deliver, problem_solve, and "<name> review"
    execute_label: Optional[str] = None  # defaults to ``name`` when unset
    empty_label: str = "issues"  # "No <empty_label> to fix."

    # --- Issue routing ----------------------------------------------------
    issue_source: str = ""  # source set on emitted ReviewIssue
    problem_solve_sources: Tuple[str, ...] = ()

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
    max_code_chars: int = 12_000
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
    conventions_by_language: Dict[str, str] = {}

    # When set (as a ``staticmethod``), ``review`` runs this build runner over the
    # resolved ``repo_path`` instead of the LLM review path. The runner takes a
    # ``pathlib.Path`` and returns a list of :class:`ReviewIssue`.
    build_runner: Optional[Callable[[Any], List["ReviewIssue"]]] = None
    # Noun used in the build-review summary, e.g. "build/test issue(s)" (backend)
    # vs "build issue(s)" (frontend).
    build_review_noun: str = "issue(s)"

    # --- Static plan output ----------------------------------------------
    plan_recommendations: List[str] = []
    plan_summary: str = ""

    # --- Parser hooks (set by subclass as staticmethods) -----------------
    # ``_parse_review`` is required only for ``review_parse_mode == "text"``.
    _parse_review: Optional[Callable[[str], Dict[str, Any]]] = None
    _parse_single_issue: Optional[Callable[[str], Dict[str, Any]]] = None

    def __init__(self, llm=None) -> None:
        from software_engineering_team.shared.strands_model import resolve_strands_model

        self._model = resolve_strands_model(llm, response_format="text")
        if self.uses_json_model:
            self._model_json = resolve_strands_model(llm, response_format="json")
        self.llm = llm  # kept for backward compat checks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def _logger(self) -> logging.Logger:
        """Logger named for the concrete subclass module (faithful warnings)."""
        return logging.getLogger(type(self).__module__)

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass's defining module.

        This is what lets ``monkeypatch.setattr(<agent_module>, "Agent", ...)``
        intercept LLM calls made from this shared base.
        """
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

    def _run_agent(self, model, prompt: str) -> str:
        return str(self._agent_factory()(model=model)(prompt)).strip()

    def _build_code_text(self, current_files: Dict[str, str]) -> str:
        return "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(current_files.items())[:20])[
            : self.max_code_chars
        ]

    def _problem_solving_kwargs(self, inp) -> Dict[str, Any]:
        """Extra ``.format`` kwargs for the single-issue prompt.

        Driven by :attr:`conventions_by_language`: when it is non-empty, inject
        ``language_conventions`` selected by ``inp.language`` (falling back to the
        ``"_default"`` entry); when empty, inject nothing — preserving frontend
        agents whose single-issue prompt has no ``{language_conventions}`` slot.

        Preconditions: when set, ``conventions_by_language`` maps lowercased
        language names (plus optional ``"_default"``) to convention strings.
        Postconditions: returns ``{}`` or ``{"language_conventions": <str>}``.
        """
        conv = self.conventions_by_language
        if not conv:
            return {}
        lang = (getattr(inp, "language", None) or "").strip().lower()
        return {"language_conventions": conv.get(lang, conv.get("_default", ""))}

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------
    def run(self, inp) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp) -> ToolAgentOutput:
        label = self.execute_label or self.name
        self._logger.info("%s: microtask %s (execute stub)", label, inp.microtask.id)
        return ToolAgentOutput(summary=f"{label} execute — no changes applied.")

    def plan(self, inp) -> ToolAgentPhaseOutput:
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
        carry this agent's :attr:`issue_source`; an engine failure degrades to a
        "(LLM error)" summary with no issues, never raising. Each engine
        ``CodeReviewIssue.suggestion`` becomes the ``ReviewIssue.recommendation``.
        """
        from code_review_agent import CodeReviewAgent, CodeReviewInput

        review_label = f"{self.name} review"
        if not inp.current_files:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        try:
            result = CodeReviewAgent(self.llm).run(
                CodeReviewInput(
                    files=dict(inp.current_files),
                    task_description=inp.task_description or "",
                    language=(getattr(inp, "language", None) or "") or "typescript",
                    profile=self.review_profile,
                )
            )
        except Exception as e:  # noqa: BLE001 — a review failure must not break the phase
            self._logger.warning("%s engine review failed: %s", review_label, e)
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
        if self.build_runner is not None:
            return self._build_review(inp)
        if self.review_via_engine:
            return self._engine_review(inp)
        review_label = f"{self.name} review"
        if not self._model:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no LLM).")
        code_text = self._build_code_text(inp.current_files)
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        prompt = self.review_prompt.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        model = getattr(self, self.review_model_attr)
        try:
            raw = self._run_agent(model, prompt)
        except Exception as e:
            self._logger.warning("%s LLM call failed: %s", review_label, e)
            return ToolAgentPhaseOutput(summary=f"{review_label} failed (LLM error).")
        if self.review_parse_mode == "json":
            data = lenient_json_object(
                raw,
                logger=self._logger,
                context=review_label,
                on_fail_msg="reporting 0 issues.",
            )
        else:
            data = self._parse_review(raw)
        issues: List[ReviewIssue] = []
        for item in data.get("issues") or []:
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

    def problem_solve(self, inp) -> ToolAgentPhaseOutput:
        if not self._model:
            return ToolAgentPhaseOutput(summary=f"{self.name} problem_solve skipped (no LLM).")
        issues = [
            i for i in inp.review_issues if (i.source or "").strip() in self.problem_solve_sources
        ]
        if not issues:
            return ToolAgentPhaseOutput(summary=f"No {self.empty_label} to fix.")
        extra = self._problem_solving_kwargs(inp)
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in issues:
            relevant_code = relevant_code_for_issue(issue, merged, self.max_relevant_code_chars)
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
            except Exception as e:
                self._logger.warning(
                    "%s fix for issue %s failed: %s",
                    self.name,
                    (issue.description or "")[:50],
                    e,
                )
                continue
            parsed = self._parse_single_issue(raw)
            fixed_files = parsed.get("files") or {}
            if fixed_files:
                merged.update(fixed_files)
                fixed_count += 1
        return ToolAgentPhaseOutput(
            files=merged,
            summary=f"{self.name}: fixed {fixed_count} of {len(issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary=f"{self.name} deliver.")


# Preferred name for the generalized review + single-issue-fix base. The historical
# ``BaseReviewToolAgent`` name is retained above and aliased here so both stacks
# (and their tests) can import either.
ReviewToolAgent = BaseReviewToolAgent
