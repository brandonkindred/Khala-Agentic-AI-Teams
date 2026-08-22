"""
Frontend stack profile: language/tooling detection + the knobs that select
frontend behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import resolve_text_mode_strands_model
from shared.dev_models.models import ReviewContext, Task
from shared.repo_context.repo_utils import find_repo_files
from software_engineering_team.code_review_agent.coordinator import run_coordinator
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.phases.review import (
    _run_build_verification_impl,
    _run_qa_agent_impl,
    _run_security_agent_impl,
    run_code_review_phase_impl,
    run_qa_testing_phase_impl,
    run_security_testing_phase_impl,
)
from software_engineering_team.shared.review_utils import (
    DOC_QUALITY_THRESHOLD,
    MANY_CHUNKS_WARN_THRESHOLD,
    MAX_DOC_SELF_REVIEW_ITERATIONS,
    MAX_REVIEW_CODE_CHARS,
    MIN_DOC_SELF_REVIEW_ITERATIONS,
)
from software_engineering_team.shared.review_utils import (
    run_documentation_self_review as _shared_run_documentation_self_review,
)
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_output_templates import _section as _section  # noqa: F401
from software_engineering_team.shared.v2_phase_bindings import build_phase_bindings
from software_engineering_team.shared.v2_review import (
    LlmReviewOutput,
    ReviewConfig,
    _review_steps_run_sequentially,  # noqa: F401  (re-exported for tests)
    run_coordinator_llm_review,
)
from software_engineering_team.shared.v2_review import (
    run_microtask_review as _shared_run_microtask_review,
)
from software_engineering_team.shared.v2_review import run_review as _shared_run_review
from software_engineering_team.shared.v2_team_config import V2TeamConfig

from .. import models as _models
from ..models import (
    DocumentationSelfReviewResult,
    ExecutionResult,
    Microtask,
    PhaseReviewResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..prompts import (
    DOCUMENTATION_SELF_REVIEW_PROMPT,
    PLANNING_FIXES_FOR_ISSUES_PROMPT,
    PLANNING_PROMPT,
    TYPESCRIPT_CONVENTIONS,
)

logger = logging.getLogger(__name__)

# Frontend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_FRONTEND_REPO_EXTENSIONS = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml"}
)
_FRONTEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "dist", "build", ".angular"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_FRONTEND_REPO_BRIEFING_MAX_CHARS = 30_000


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer frontend stack from repo or task.

    Preconditions:
        ``repo_path`` is a ``Path`` (may or may not exist); ``task`` is a
        ``Task`` whose ``description``/``requirements`` may be ``None``.
    Postconditions:
        Returns one of ``"angular"``, ``"react"``, or ``"typescript"``; never
        raises. Repo signals take precedence over task-text heuristics;
        ``"typescript"`` is the default.
    """
    if repo_path.is_dir():
        if (repo_path / "angular.json").exists():
            return "angular"
        pkg = repo_path / "package.json"
        if pkg.exists():
            try:
                content = pkg.read_text(encoding="utf-8")
                if "@angular/core" in content or "@angular/common" in content:
                    return "angular"
                if '"react"' in content or "'react'" in content:
                    return "react"
            except (OSError, UnicodeDecodeError) as exc:
                # Best-effort substring probe on the raw text (no json.loads),
                # so only file-read (OSError) and decode (UnicodeDecodeError)
                # failures can land here — a malformed package.json just means
                # no stack signal was found and the repo walk / task-text
                # heuristics decide instead. Logged at DEBUG (mirroring
                # _detect_tooling) so a real config problem stays observable.
                logger.debug("[%s] failed to read/decode package.json: %s", repo_path, exc)
        # Pruned os.walk (find_repo_files) so a checkout with a large
        # node_modules/.git/dist/.angular is never descended into while probing
        # for tsconfig / *.ts / *.tsx — the same I/O discipline as
        # read_repo_code_budgeted, replacing the rglob calls that enumerated
        # those excluded subtrees before filtering.
        if find_repo_files(repo_path, names={"tsconfig.json"}):
            return "typescript"
        if find_repo_files(repo_path, suffixes={".tsx", ".ts"}):
            return "typescript"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "angular" in desc:
        return "angular"
    if "react" in desc:
        return "react"
    if "typescript" in desc or "ts " in desc:
        return "typescript"
    return "typescript"


def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Return ``(has_lint, has_test)`` for the configured frontend tooling.

    Detects ESLint/Angular configs as lint, and Vitest/Jest/Karma or a real
    ``npm test`` script as testing. Best-effort: an unparseable ``package.json``
    just means no test script was found.

    Preconditions: ``repo_path`` is a directory.
    Postconditions: returns two booleans. Raises ``AssertionError`` if the
      precondition is violated (a non-directory ``repo_path`` is a caller
      bug, not a runtime failure mode this method recovers from).
    """
    assert repo_path.is_dir(), "repo_path must be a directory"
    has_lint = (
        next(repo_path.glob("eslint.config.*"), None) is not None
        or next(repo_path.glob(".eslintrc*"), None) is not None
        or (repo_path / "angular.json").exists()
    )
    has_test = False
    if (
        next(repo_path.glob("vitest.config.*"), None) is not None
        or next(repo_path.glob("jest.config.*"), None) is not None
        or (repo_path / "karma.conf.js").exists()
    ):
        has_test = True
    else:
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                test_script = pkg.get("scripts", {}).get("test", "")
                if test_script and "no test" not in test_script and "exit 1" not in test_script:
                    has_test = True
            except Exception as exc:
                # A malformed package.json means no test script was found;
                # log at DEBUG so a real config problem is observable during
                # debugging without failing the best-effort pre-flight gate.
                logger.debug("[%s] failed to parse package.json: %s", repo_path, exc)
    return has_lint, has_test


PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": TYPESCRIPT_CONVENTIONS},
    has_language_conventions=False,
    build_verify_label="frontend_code_v2",
    detect_language=_detect_language,
    repo_extensions=_FRONTEND_REPO_EXTENSIONS,
    repo_exclude_dirs=_FRONTEND_REPO_EXCLUDE_DIRS,
    repo_max_chars=_FRONTEND_REPO_BRIEFING_MAX_CHARS,
    detect_tooling=_detect_tooling,
)


# ---------------------------------------------------------------------------
# Review config: the knobs that select frontend behaviour in the shared
# ``shared.v2_review.run_review`` / ``run_microtask_review`` bodies.
# ---------------------------------------------------------------------------


def _frontend_summary_review(
    passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for the frontend full-Review phase.

    Preconditions: all args are the booleans/ints the shared reviewer computes.
    Postconditions: returns a single human-readable line naming pass/fail and
    the issue count; ignores ``build_ok``/``lint_ok``/``n_critical`` (frontend
    keeps its summary terse — the per-gate status is logged separately).
    """
    return f"Review {'passed' if passed else 'failed'}; {n_issues} issue(s)."


def _frontend_summary_microtask(
    microtask_id: str, passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for a frontend microtask review.

    Preconditions: ``microtask_id`` is the microtask's id; the rest are the
    reviewer-computed booleans/ints.
    Postconditions: returns a single line naming the microtask, pass/fail, and
    issue count; ignores ``build_ok``/``lint_ok``/``n_critical`` (terse summary).
    """
    return (
        f"Microtask {microtask_id} review {'passed' if passed else 'failed'}; {n_issues} issue(s)."
    )


def _frontend_microtask_intro(microtask_id: str, n_files: int) -> str:
    """Intro line emitted when a frontend microtask review begins.

    Preconditions: ``microtask_id`` is the microtask's id; ``n_files`` >= 0 is
    the number of files scoped into the review.
    Postconditions: returns a single line naming the microtask, its file count,
    and the next quality-gate step.
    """
    return (
        f"Microtask review for {microtask_id} ({n_files} files). "
        "Next step -> Build verification, lint, code review"
    )


REVIEW_CONFIG = ReviewConfig(
    lint_agent_type="frontend",
    build_fail_recommendation_review="Fix build errors; consider triggering Build Specialist.",
    # Frontend keeps the raw linter severity (no remap).
    lint_severity_remap=None,
    # Frontend uses kind.value verbatim (no "tool_" prefix) and a blank rec.
    tool_rec_source_prefix=None,
    tool_rec_recommendation_uses_rec=False,
    # Frontend omits existing_code/spec_context/language on the tool phase input.
    tool_phase_includes_context=False,
    # Frontend run-review `passed` includes lint_ok.
    passed_includes_lint_review=True,
    # Frontend run-review does not log its summary line.
    log_review_summary=False,
    tool_phase_input_factory=ToolAgentPhaseInput,
    summary_review=_frontend_summary_review,
    summary_microtask=_frontend_summary_microtask,
    microtask_intro=_frontend_microtask_intro,
)


# ---------------------------------------------------------------------------
# Extra review clause: frontend-specific accessibility-verification note
# injected into code-review task requirements.
# Defined here (rather than in review.py) so V2TeamConfig can reference it
# without creating a circular import (review.py already imports from _profile).
# ---------------------------------------------------------------------------

_ACCESSIBILITY_VERIFY_NOTE = (
    "Also verify accessibility: semantic markup, ARIA attributes, keyboard "
    "navigation, and color contrast."
)

# ---------------------------------------------------------------------------
# V2TeamConfig: the single source of truth for frontend's team-specific knobs.
# Defined here (alongside PROFILE) so that both the orchestrator and tool agents
# can import it without creating a circular dependency.
# ---------------------------------------------------------------------------

FRONTEND_CONFIG = V2TeamConfig(
    stack_profile=PROFILE,
    tool_agent_kinds=frozenset(k.value for k in ToolAgentKind if k is not ToolAgentKind.GENERAL),
    extra_review_clause=_ACCESSIBILITY_VERIFY_NOTE,
    output_template_path_prefixes=("frontend/", "./frontend/"),
    output_template_allowed_languages=("angular", "react", "vue", "typescript", "javascript"),
    output_template_coerce_unknown=False,
)


# ---------------------------------------------------------------------------
# Phase bindings: the config-driven documentation/planning/output-template
# entry points, bound once here from the shared implementations via
# ``FRONTEND_CONFIG`` (see ``shared/v2_phase_bindings.py``). Replaces the
# former per-team ``phases/documentation.py``, ``phases/planning.py``, and
# ``output_templates.py`` twin wrapper modules.
# ---------------------------------------------------------------------------

_bindings = build_phase_bindings(
    models=_models,
    config=FRONTEND_CONFIG,
    planning_prompt=PLANNING_PROMPT,
    planning_fixes_prompt=PLANNING_FIXES_FOR_ISSUES_PROMPT,
)

run_documentation_phase = _bindings.run_documentation_phase
run_planning = _bindings.run_planning
plan_fixes_for_unresolved_issues = _bindings.plan_fixes_for_unresolved_issues
_parse_planning_output = _bindings.parse_planning_output
parse_files_and_summary_template = _bindings.parse_files_and_summary_template
parse_planning_template = _bindings.parse_planning_template
parse_review_template = _bindings.parse_review_template
parse_problem_solving_template = _bindings.parse_problem_solving_template
parse_problem_solving_single_issue_template = _bindings.parse_problem_solving_single_issue_template
parse_batch_fix_template = _bindings.parse_batch_fix_template
parse_documentation_self_review_template = _bindings.parse_documentation_self_review_template


# ---------------------------------------------------------------------------
# Extra review clause: frontend-specific accessibility-verification note is
# defined above (``_ACCESSIBILITY_VERIFY_NOTE``) and threaded through
# ``FRONTEND_CONFIG.extra_review_clause`` -- consumed below by
# ``_run_llm_review``, not injected via a forked ``review.py``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review entry points (Story 3c, Step 2): thin wrappers over the shared
# ``v2_review``/``shared.phases.review`` engines, driven by ``FRONTEND_CONFIG``
# and ``REVIEW_CONFIG``. Replaces the former per-team ``phases/review.py``
# twin wrapper module.
#
# Kept as real ``def``s here (not closures returned from a factory) so
# ``monkeypatch.setattr(_profile, "_run_llm_review", ...)`` and friends keep
# working exactly as they did against the deleted ``phases/review.py`` --
# each function below looks up its collaborators (``run_coordinator``,
# ``_run_llm_review``, ``_run_qa_agent``, ``_run_security_agent``,
# ``_run_build_verification``, ``Agent``, ``resolve_text_mode_strands_model``)
# from this module's globals at call time, not at import/bind time.
# ---------------------------------------------------------------------------


def _run_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> LlmReviewOutput[ReviewIssue]:
    """Lightweight code review when no external review agent is available.

    Thin wrapper over the shared
    :func:`software_engineering_team.shared.v2_review.run_coordinator_llm_review`.
    Passes :data:`FRONTEND_CONFIG.extra_review_clause` as
    ``extra_task_requirements`` -- frontend-specific, since backend's code
    has no UI to check accessibility on.

    Preconditions:
        - ``language`` defaults to this team's ``PROFILE.default_language``
          ("typescript") so an existing caller that does not pass it yet
          keeps reviewing under the correct language; this team's
          ``_detect_language`` may also pass ``"angular"``/``"react"``
          explicitly.
        - ``enable_llm_review_grounding`` is accepted for call-signature
          compatibility with ``llm_review_fn``'s contract but is otherwise
          unused (see the shared function's docstring).
    """
    return run_coordinator_llm_review(
        llm=llm,
        task=task,
        files=files,
        language=language,
        run_coordinator_fn=run_coordinator,
        review_context=review_context,
        extra_task_requirements=FRONTEND_CONFIG.extra_review_clause,
    )


def _run_qa_agent(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> Any:
    """Run the external QA agent; thin wrapper over the shared impl.

    Postconditions: see ``software_engineering_team.shared.agent_review``; QA
    bugs become ``ReviewIssue``s with ``source="qa"``.
    """
    return _run_qa_agent_impl(
        qa_agent=qa_agent,
        files=files,
        language=language,
        task_description=task_description,
        task_id=task_id,
        issue_factory=ReviewIssue,
        context=context,
        cache=cache,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
    )


def _run_security_agent(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> Any:
    """Run the external security agent; thin wrapper over the shared impl.

    Postconditions: see ``software_engineering_team.shared.agent_review``;
    vulnerabilities become ``ReviewIssue``s with ``source="security"``.
    """
    return _run_security_agent_impl(
        security_agent=security_agent,
        files=files,
        language=language,
        task_description=task_description,
        task_id=task_id,
        issue_factory=ReviewIssue,
        context=context,
        cache=cache,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
    )


def _run_build_verification(
    repo_path: Path,
    build_verifier: Optional[Callable[..., Tuple[bool, str]]],
    task_id: str,
) -> Tuple[bool, str]:
    """Run the build verifier if provided, else assume success."""
    return _run_build_verification_impl(
        repo_path, build_verifier, task_id, build_verify_label=PROFILE.build_verify_label
    )


def run_review(
    *,
    llm: LLMClient,
    task: Task,
    execution_result: ExecutionResult,
    repo_path: Path,
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> ReviewResult:
    """Execute the Review phase.

    Thin wrapper over the shared parametrised reviewer
    (:func:`software_engineering_team.shared.v2_review.run_review`) driven by
    :data:`REVIEW_CONFIG`. The per-team code-review/QA/security runners are
    injected as module-level callables so this module stays the test patch
    surface for ``_run_llm_review`` / ``_run_qa_agent`` / ``_run_security_agent``.

    Preconditions: ``execution_result`` exposes ``.files``.
    Postconditions: see the shared ``run_review``.
    """
    return _shared_run_review(
        config=REVIEW_CONFIG,
        llm=llm,
        task=task,
        execution_result=execution_result,
        repo_path=repo_path,
        build_verifier=build_verifier,
        qa_agent=qa_agent,
        security_agent=security_agent,
        code_review_agent=code_review_agent,
        linting_tool_agent=linting_tool_agent,
        tool_agents=tool_agents,
        language=language,
        llm_review_fn=_run_llm_review,
        qa_agent_fn=_run_qa_agent,
        security_agent_fn=_run_security_agent,
        build_verify_fn=_run_build_verification,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )


def run_microtask_review(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Microtask,
    repo_path: Path,
    files: Dict[str, str],
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    cache: Optional[AgentReviewCache] = None,
    tool_agent_cache: Optional[AgentReviewCache] = None,
) -> ReviewResult:
    """Run full review on a single microtask's output files.

    Thin wrapper over
    :func:`software_engineering_team.shared.v2_review.run_microtask_review`;
    see :func:`run_review` for the injection rationale.

    Preconditions:
        - ``microtask`` exposes ``.id``/``.title``/``.description``.
        - ``cache``: see ``software_engineering_team.shared.agent_review``;
          forwarded to the QA/security steps only.
        - ``tool_agent_cache``: within *this* call, consulted/populated only
          by the tool-agent fan-out step (not the QA/security LLM steps,
          which use ``cache`` instead). Because the three gates
          (``_code_review_gate``/``_qa_gate``/``_security_gate``) each pass
          the *same* ``tool_agent_cache`` instance (read off
          ``ReviewDependencies.tool_agent_cache``, one instance per microtask
          cycle), a tool agent computed by an earlier gate's call within the
          same cycle is reused by a later gate's call instead of recomputed.

    Postconditions:
        - Delegates to ``_shared_run_microtask_review``. See the shared
          function for the full review-result contract.
        - When given, ``tool_agent_cache`` and ``cache`` may each be mutated
          (populated with new entries) as a side effect of a live agent call.
    """
    return _shared_run_microtask_review(
        config=REVIEW_CONFIG,
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=build_verifier,
        qa_agent=qa_agent,
        security_agent=security_agent,
        code_review_agent=code_review_agent,
        linting_tool_agent=linting_tool_agent,
        tool_agents=tool_agents,
        detail_callback=detail_callback,
        language=language,
        llm_review_fn=_run_llm_review,
        qa_agent_fn=_run_qa_agent,
        security_agent_fn=_run_security_agent,
        build_verify_fn=_run_build_verification,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        agent_review_cache=cache,
        tool_agent_cache=tool_agent_cache,
    )


def run_code_review_phase(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Microtask,
    repo_path: Path,
    files: Dict[str, str],
    code_review_agent: Any = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> PhaseReviewResult:
    """Run code review phase only: the code review step.

    This is the first phase after coding, focusing on code quality, syntax,
    and adherence to coding standards. Build verification and linting run
    elsewhere (this team's own pre-review quality gate, or the separate
    ``run_review``/``run_microtask_review`` path), not in this phase.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_code_review_phase_impl`)
    that injects this team's LLM-based reviewer via ``llm_review_fn`` and the
    per-team result class via ``phase_review_result_cls``.
    """
    return run_code_review_phase_impl(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        code_review_agent=code_review_agent,
        detail_callback=detail_callback,
        language=language,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        llm_review_fn=_run_llm_review,
        phase_review_result_cls=PhaseReviewResult,
    )


def run_qa_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    qa_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """Run QA testing phase: bug detection, test coverage, quality assurance.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_qa_testing_phase_impl`).
    """
    return run_qa_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=qa_agent,
        agent_runner=partial(_run_qa_agent, qa_agent=qa_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
        tool_phase_includes_context=REVIEW_CONFIG.tool_phase_includes_context,
    )


def run_security_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    security_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """Run security testing phase: vulnerability scanning, security best practices.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_security_testing_phase_impl`).
    """
    return run_security_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=security_agent,
        agent_runner=partial(_run_security_agent, security_agent=security_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
        tool_phase_includes_context=REVIEW_CONFIG.tool_phase_includes_context,
    )


def run_documentation_self_review(
    *,
    llm: LLMClient,
    documentation: Dict[str, str],
    code_files: Dict[str, str],
    task_description: str = "",
    min_iterations: int = MIN_DOC_SELF_REVIEW_ITERATIONS,
    max_iterations: int = MAX_DOC_SELF_REVIEW_ITERATIONS,
    quality_threshold: float = DOC_QUALITY_THRESHOLD,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> DocumentationSelfReviewResult:
    """Self-review documentation across iterations for quality refinement.

    Thin wrapper that delegates the chunking/iteration orchestration to the
    shared ``run_documentation_self_review`` helper, passing this team's
    prompt, parser, and ``DocumentationSelfReviewResult`` factory. The
    Strands ``Agent`` invocation is built here so this module stays the patch
    surface for ``Agent`` and ``resolve_text_mode_strands_model``.
    """

    def _invoke(prompt: str) -> str:
        return str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()

    return _shared_run_documentation_self_review(
        documentation=documentation,
        code_files=code_files,
        prompt_template=DOCUMENTATION_SELF_REVIEW_PROMPT,
        parse_template=parse_documentation_self_review_template,
        result_factory=DocumentationSelfReviewResult,
        invoke_model=_invoke,
        task_description=task_description,
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        quality_threshold=quality_threshold,
        detail_callback=detail_callback,
    )


__all__ = [
    "PROFILE",
    "REVIEW_CONFIG",
    "FRONTEND_CONFIG",
    "run_documentation_phase",
    "run_planning",
    "plan_fixes_for_unresolved_issues",
    "_parse_planning_output",
    "_detect_language",
    "parse_files_and_summary_template",
    "parse_planning_template",
    "parse_review_template",
    "parse_problem_solving_template",
    "parse_problem_solving_single_issue_template",
    "parse_batch_fix_template",
    "parse_documentation_self_review_template",
    "run_review",
    "run_microtask_review",
    "run_code_review_phase",
    "run_qa_testing_phase",
    "run_security_testing_phase",
    "run_documentation_self_review",
]
