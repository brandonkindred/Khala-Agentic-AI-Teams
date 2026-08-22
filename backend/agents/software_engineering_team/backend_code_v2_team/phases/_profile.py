"""
Backend stack profile: language/tooling detection + the knobs that select
backend behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

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
from software_engineering_team.shared.text_utils import has_section_header, toml_has_section
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
    JAVA_CONVENTIONS,
    PLANNING_FIXES_FOR_ISSUES_PROMPT,
    PLANNING_PROMPT,
    PYTHON_CONVENTIONS,
)

logger = logging.getLogger(__name__)

# Backend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_BACKEND_REPO_EXTENSIONS = frozenset(
    {".py", ".java", ".kt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt"}
)
_BACKEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "__pycache__", "venv", ".venv"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_BACKEND_REPO_BRIEFING_MAX_CHARS = 30_000


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer whether the project is Python or Java from the repo.

    Preconditions:
        ``repo_path`` is a ``Path`` (may or may not exist); ``task`` is a
        ``Task`` whose ``description``/``requirements`` may be ``None``.
    Postconditions:
        Returns ``"java"`` or ``"python"``; never raises. Repo signals take
        precedence over task-text heuristics; ``"python"`` is the default.
    """
    if repo_path.is_dir():
        # Pruned os.walk (find_repo_files) so a checkout with a large
        # node_modules/.git/.venv is never descended into while probing for
        # build files / *.java — the same I/O discipline as
        # read_repo_code_budgeted, replacing the rglob calls that enumerated
        # those excluded subtrees before filtering.
        if find_repo_files(repo_path, names={"pom.xml", "build.gradle"}):
            return "java"
        if find_repo_files(repo_path, names={"requirements.txt", "pyproject.toml"}):
            return "python"
        if find_repo_files(repo_path, suffixes={".java"}):
            return "java"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "spring" in desc or "java" in desc or "maven" in desc or "gradle" in desc:
        return "java"
    return "python"


def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Return ``(has_lint, has_test)`` for the configured backend tooling.

    Detects ruff/flake8 (or a ``[tool.ruff]`` block in ``pyproject.toml``) as
    lint, and a ``tests`` dir with a pytest config (``pytest.ini`` or a
    ``[tool.pytest`` block in ``pyproject.toml``) as testing. Reads
    ``pyproject.toml`` once and reuses it for both probes. Lint also
    recognises a ``[flake8]`` section in ``setup.cfg`` — a common flake8
    config location that the file-name-only ``.flake8`` probe would miss.

    The ``[tool.ruff]`` / ``[tool.pytest`` pyproject checks use the shared
    ``toml_has_section`` helper: a real TOML parse (stdlib ``tomllib`` on
    Python 3.11+, the ``tomli`` backport if installed) that asks whether the
    table actually exists, so a section header appearing inside a
    multi-line string value can no longer produce a false positive; on
    Python 3.10 without ``tomli`` (or on unparseable TOML) it falls back to
    the line-anchored ``has_section_header`` text scan. The ``[flake8]``
    ``setup.cfg`` probe stays on ``has_section_header`` (INI has no
    multi-line strings, so the text scan is exact there). No hard dependency
    is added: 3.11+ stdlib covers the real runtime, and 3.10 keeps the prior
    best-effort text probe. The pre-flight only decides whether to fail the
    task early for missing tooling, so a residual false positive errs toward
    proceeding (a real build/lint gate still enforces correctness).

    Preconditions: ``repo_path`` is a directory.
    Postconditions: returns two booleans. Raises ``AssertionError`` if the
      precondition is violated (a non-directory ``repo_path`` is a caller
      bug, not a runtime failure mode this method recovers from).
    """
    assert repo_path.is_dir(), "repo_path must be a directory"
    pyproject_path = repo_path / "pyproject.toml"
    pyproject_text = (
        pyproject_path.read_text(encoding="utf-8", errors="replace")
        if pyproject_path.exists()
        else ""
    )
    setup_cfg_path = repo_path / "setup.cfg"
    setup_cfg_text = (
        setup_cfg_path.read_text(encoding="utf-8", errors="replace")
        if setup_cfg_path.exists()
        else ""
    )
    has_lint = (
        (repo_path / "ruff.toml").exists()
        or (repo_path / ".flake8").exists()
        or toml_has_section(pyproject_text, "[tool.ruff]")
        or has_section_header(setup_cfg_text, "[flake8]")
    )
    has_test = (repo_path / "tests").is_dir() and (
        (repo_path / "pytest.ini").exists() or toml_has_section(pyproject_text, "[tool.pytest")
    )
    return has_lint, has_test


PROFILE = StackProfile(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS},
    has_language_conventions=True,
    build_verify_label="backend_code_v2",
    detect_language=_detect_language,
    repo_extensions=_BACKEND_REPO_EXTENSIONS,
    repo_exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
    repo_max_chars=_BACKEND_REPO_BRIEFING_MAX_CHARS,
    detect_tooling=_detect_tooling,
)


# ---------------------------------------------------------------------------
# Review config: the knobs that select backend behaviour in the shared
# ``shared.v2_review.run_review`` / ``run_microtask_review`` bodies.
# ---------------------------------------------------------------------------

# Backend remaps linter severities into review severities (frontend keeps raw).
_BACKEND_LINT_SEVERITY_MAP = {"error": "high", "warning": "medium", "info": "low"}


def _backend_summary_review(
    passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for the backend full-Review phase.

    Preconditions: all args are the booleans/ints the shared reviewer computes.
    Postconditions: returns a single human-readable line naming the build/lint
    outcome and the issue count (with the critical/high count). ``passed`` is
    unused — the backend full-review summary reports gate status, not an
    overall pass/fail label.
    """
    return (
        f"Review: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, "
        f"{n_issues} issues ({n_critical} critical/high)."
    )


def _backend_summary_microtask(
    microtask_id: str, passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for a backend microtask review.

    Preconditions: ``microtask_id`` is the microtask's id; the rest are the
    reviewer-computed booleans/ints.
    Postconditions: returns a single line naming the microtask, its build/lint
    outcome, issue count (with critical/high count), and a PASSED/FAILED label
    driven by ``passed``.
    """
    return (
        f"Microtask {microtask_id} review: build={'OK' if build_ok else 'FAIL'}, "
        f"lint={'OK' if lint_ok else 'FAIL'}, {n_issues} issues ({n_critical} critical/high). "
        f"{'PASSED' if passed else 'FAILED'}"
    )


def _backend_microtask_intro(microtask_id: str, n_files: int) -> str:
    """Intro line emitted when a backend microtask review begins.

    Preconditions: ``microtask_id`` is the microtask's id; ``n_files`` >= 0 is
    the number of files scoped into the review.
    Postconditions: returns a single line naming the microtask and its file
    count, marking the start of the per-microtask quality-gate sequence.
    """
    return f"Running microtask review for {microtask_id} ({n_files} files)"


REVIEW_CONFIG = ReviewConfig(
    lint_agent_type="backend",
    build_fail_recommendation_review="Fix compilation/test errors before proceeding.",
    lint_severity_remap=_BACKEND_LINT_SEVERITY_MAP,
    tool_rec_source_prefix="tool_",
    tool_rec_recommendation_uses_rec=True,
    tool_phase_includes_context=True,
    # Backend run-review ignores lint_ok in `passed` (only build + blocking).
    passed_includes_lint_review=False,
    log_review_summary=True,
    tool_phase_input_factory=ToolAgentPhaseInput,
    summary_review=_backend_summary_review,
    summary_microtask=_backend_summary_microtask,
    microtask_intro=_backend_microtask_intro,
)


# ---------------------------------------------------------------------------
# V2TeamConfig: the single source of truth for backend's team-specific knobs.
# Defined here (alongside PROFILE) so that both the orchestrator and tool agents
# can import it without creating a circular dependency.
# ---------------------------------------------------------------------------

BACKEND_CONFIG = V2TeamConfig(
    stack_profile=PROFILE,
    tool_agent_kinds=frozenset(k.value for k in ToolAgentKind if k is not ToolAgentKind.GENERAL),
    extra_review_clause="",
    output_template_path_prefixes=("backend/", "./backend/"),
    output_template_allowed_languages=("python", "java"),
    output_template_coerce_unknown=True,
)


# ---------------------------------------------------------------------------
# Phase bindings: the config-driven documentation/planning/output-template
# entry points, bound once here from the shared implementations via
# ``BACKEND_CONFIG`` (see ``shared/v2_phase_bindings.py``). Replaces the
# former per-team ``phases/documentation.py``, ``phases/planning.py``, and
# ``output_templates.py`` twin wrapper modules.
# ---------------------------------------------------------------------------

_bindings = build_phase_bindings(
    models=_models,
    config=BACKEND_CONFIG,
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
# Review entry points (Story 3c, Step 2): thin wrappers over the shared
# ``v2_review``/``shared.phases.review`` engines, driven by ``BACKEND_CONFIG``
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
    Always passes :data:`BACKEND_CONFIG.extra_review_clause` as
    ``extra_task_requirements`` -- backend's is ``""``, so this is a no-op,
    but it keeps this wrapper identical in shape to the frontend team's
    (whose clause is non-empty), with no ``if team is frontend`` branch
    anywhere in either.

    Preconditions:
        - ``language`` defaults to this team's ``PROFILE.default_language``
          ("python") so an existing caller that does not pass it yet keeps
          reviewing under the correct language.
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
        extra_task_requirements=BACKEND_CONFIG.extra_review_clause,
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
    see :func:`run_review` for the injection rationale. Always accepts and
    forwards ``tool_agent_cache`` (default ``None``): the shared engine
    already supports it uniformly, and this team's ``ReviewDependencies``
    never populates it (only the frontend team's does -- see
    ``docs/GATE_DEPENDENCY_GRAPH.md``), so passing ``None`` here is
    behavior-neutral.

    Preconditions:
        - ``microtask`` exposes ``.id``/``.title``/``.description``.
        - ``cache``: see ``software_engineering_team.shared.agent_review``;
          forwarded to the QA/security steps only.

    Postconditions:
        - Delegates to ``_shared_run_microtask_review``. See the shared
          function for the full review-result contract.
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
    "BACKEND_CONFIG",
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
