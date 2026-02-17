"""Backend Expert agent: Python/Java implementation and autonomous workflow."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task, TaskUpdate

from .models import (
    BackendInput,
    BackendOutput,
    BackendWorkflowResult,
    ReviewIterationRecord,
)
from .prompts import BACKEND_PROMPT

logger = logging.getLogger(__name__)

# Validation constants
MAX_PATH_SEGMENT_LENGTH = 30
BAD_NAME_PATTERN = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+")  # 4+ hyphenated words = likely sentence
BAD_NAME_SNAKE_PATTERN = re.compile(r"^[a-z]+_[a-z]+_[a-z]+_[a-z]+_[a-z]+")  # 5+ underscored words = likely sentence
VERB_PREFIX_PATTERN = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)[_-]"
)
FILLER_WORD_PATTERN = re.compile(r"[_-](the|that|with|using|which|for|and|a|an)[_-]")

# Well-known directory names that are always allowed
_ALLOWED_DIRS = frozenset({
    "app", "src", "lib", "tests", "test", "routers", "models", "schemas",
    "services", "controllers", "repository", "middleware", "config", "utils",
    "helpers", "main", "infrastructure", "dist", "build",
})


def _validate_file_paths(files: Dict[str, str]) -> tuple[Dict[str, str], list[str]]:
    """
    Validate and sanitize file paths from LLM output.

    Returns (validated_files, warnings).
    Rejects files with:
    - Path segments > MAX_PATH_SEGMENT_LENGTH
    - Names that look like sentences (4+ hyphenated or 5+ underscored words)
    - Names starting with verbs (implement_, create_, build_, etc.)
    - Names containing filler words (_the_, _with_, _using_, etc.)
    - Empty content
    """
    validated = {}
    warnings = []
    for path, content in files.items():
        segments = path.split("/")
        bad_segment = False
        for seg in segments:
            name_part = seg.split(".")[0]
            if not name_part:
                continue
            # Skip well-known directory names
            if name_part.lower() in _ALLOWED_DIRS:
                continue
            if len(name_part) > MAX_PATH_SEGMENT_LENGTH:
                warnings.append(f"Path segment too long: '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(f"Path segment looks like a sentence (4+ hyphenated words): '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_SNAKE_PATTERN.match(name_part):
                warnings.append(f"Path segment looks like a sentence (5+ underscored words): '{seg}' in '{path}'")
                bad_segment = True
                break
            if VERB_PREFIX_PATTERN.match(name_part):
                warnings.append(f"Path segment starts with a verb (task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
            if FILLER_WORD_PATTERN.search(name_part):
                warnings.append(f"Path segment contains filler words (task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
        if bad_segment:
            continue
        if not content or not content.strip():
            if path.endswith("__init__.py") or path == "tests/__init__.py":
                validated[path] = '"""Package."""\n'
            else:
                warnings.append(f"Empty file content for '{path}' - skipping")
                continue
        else:
            validated[path] = content
    return validated, warnings


# ── Workflow constants ──────────────────────────────────────────────────────
MAX_REVIEW_ITERATIONS = 20
MAX_SAME_BUILD_FAILURES = 3  # Stop retrying if build fails identically this many times
MAX_CLARIFICATION_ROUNDS = 5
MAX_EXISTING_CODE_CHARS = 40_000

# Patterns that indicate pytest failed due to missing /test-generic-error route or
# exception handler re-raising (test client gets exception instead of response).
# When matched, we give the agent a targeted suggestion instead of generic "fix errors".
# This special handling ensures the agent receives an explicit instruction to preserve
# the route and return JSONResponse, avoiding repeated build failures.
EXCEPTION_HANDLER_TEST_PATTERNS = (
    "test-generic-error",
    "test_generic_exception_handler",
    "test_error_handlers",
)


# Test-only routes that must be preserved when modifying app/main.py.
# Scanned from tests/ via client.get("/...") or similar.
_TEST_ROUTE_PATTERNS = ("/test-generic-error",)


def _test_routes_referenced_in_tests(repo_path: Path) -> List[str]:
    """Scan tests/ for routes that tests call (e.g. client.get(\"/test-generic-error\"))."""
    tests_dir = repo_path / "tests"
    if not tests_dir.exists() or not tests_dir.is_dir():
        return []
    found: List[str] = []
    for f in tests_dir.rglob("test_*.py"):
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            for route in _TEST_ROUTE_PATTERNS:
                if route in content and route not in found:
                    found.append(route)
        except Exception:
            pass
    return found


def _test_routes_missing_from_main_py(repo_path: Path, files: Dict[str, str]) -> List[str]:
    """Return routes that tests reference but are missing from main.py in files."""
    required = _test_routes_referenced_in_tests(repo_path)
    if not required:
        return []
    main_content = ""
    for path in ("app/main.py", "main.py"):
        if path in files:
            main_content = files.get(path, "")
            break
    if not main_content:
        return []  # No main.py in output, nothing to check
    missing = [r for r in required if r not in main_content]
    return missing


def _build_code_review_issues_for_missing_test_routes() -> List[Dict[str, Any]]:
    """Build code_review_issues for pre-write check: tests reference routes missing from main.py."""
    suggestion = (
        "Tests expect the route `/test-generic-error` and a generic "
        "exception handler that returns a JSONResponse (e.g. status_code=500). "
        "Preserve this route in `app/main.py` and ensure the handler does not "
        "re-raise; otherwise the test client gets an exception and the test fails."
    )
    return [
        {
            "severity": "critical",
            "category": "build",
            "file_path": "app/main.py",
            "description": "Pre-write check: tests reference /test-generic-error but app/main.py does not include it.",
            "suggestion": suggestion,
        }
    ]


def _build_code_review_issues_for_build_failure(build_errors: str) -> List[Dict[str, Any]]:
    """Build code_review_issues from build/test failure output.

    When failure matches exception-handler test patterns, returns a targeted
    suggestion (preserve /test-generic-error, return JSONResponse) and file_path
    app/main.py so the agent knows exactly what to fix.
    """
    is_exception_handler_failure = any(
        p in build_errors for p in EXCEPTION_HANDLER_TEST_PATTERNS
    )
    if is_exception_handler_failure:
        suggestion = (
            "Tests expect the route `/test-generic-error` and a generic "
            "exception handler that returns a JSONResponse (e.g. status_code=500). "
            "Preserve this route in `app/main.py` and ensure the handler does not "
            "re-raise; otherwise the test client gets an exception and the test fails."
        )
        file_path = "app/main.py"
    else:
        suggestion = "Fix the compilation/test errors"
        file_path = ""
    return [
        {
            "severity": "critical",
            "category": "build",
            "file_path": file_path,
            "description": f"Build/test failed: {build_errors[:2500]}",
            "suggestion": suggestion,
        }
    ]


def _read_repo_code(repo_path: Path, extensions: List[str] | None = None) -> str:
    """Read code files from repo, concatenated.

    Only reads application source code by default (.py, .java).
    DevOps/infrastructure files (.yml, .yaml) are excluded to avoid
    polluting backend coding context with unrelated content.
    Excludes .git to avoid errors on missing/corrupt git objects.
    """
    if extensions is None:
        extensions = [".py", ".java"]
    parts: List[str] = []
    for f in repo_path.rglob("*"):
        if ".git" in f.parts:
            continue
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(
                    f"### {f.relative_to(repo_path)} ###\n"
                    f"{f.read_text(encoding='utf-8', errors='replace')}"
                )
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


def _truncate_for_context(text: str, max_chars: int) -> str:
    """Truncate text for agent context, with truncation notice."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n\n... [truncated, {len(text) - max_chars} more chars]"


def _task_requirements(task: Task) -> str:
    """Build full requirements string from a Task object."""
    parts: List[str] = []
    if task.description:
        parts.append(f"Task Description:\n{task.description}")
    if getattr(task, "user_story", None):
        parts.append(f"User Story: {task.user_story}")
    if task.requirements:
        parts.append(f"Technical Requirements:\n{task.requirements}")
    if getattr(task, "acceptance_criteria", None):
        parts.append("Acceptance Criteria:\n- " + "\n- ".join(task.acceptance_criteria))
    return "\n\n".join(parts) if parts else task.description


class BackendExpertAgent:
    """
    Backend expert that implements solutions in Python or Java.

    Has two modes of operation:
    - ``run()``: Stateless code generation via LLM (original behaviour).
    - ``run_workflow()``: Autonomous 9-step lifecycle that creates a feature
      branch, generates code, triggers QA/DBC/code-review agents, iterates on
      feedback (up to 20 rounds), merges to development, and notifies the Tech Lead.

    Invariants:
        - ``self.llm`` is always a valid LLMClient.
        - ``run()`` never modifies the repository; ``run_workflow()`` does.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    # ── Autonomous workflow ─────────────────────────────────────────────────

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        qa_agent: Any,
        security_agent: Any,
        dbc_agent: Any,
        code_review_agent: Any,
        acceptance_verifier_agent: Any | None = None,
        tech_lead: Any = None,  # Required but default None for backward compat in tests
        build_verifier: Callable[..., Tuple[bool, str]],
        doc_agent: Any | None = None,
        completed_tasks: List[Task] | None = None,
        remaining_tasks: List[Task] | None = None,
        all_tasks: Dict[str, Task] | None = None,
        execution_queue: List[str] | None = None,
        append_task_fn: Optional[Callable[[Task], None]] = None,
    ) -> BackendWorkflowResult:
        """
        Execute the full backend task lifecycle autonomously.

        Steps:
            1. Create a feature branch from ``development``.
            2. Generate backend code that satisfies the task requirements.
            3. Write files and commit to the feature branch.
            4. Trigger QA and DBC (Design by Contract) agents to review.
            5. Wait for and collect all review responses.
            6. Implement fixes for reported issues and commit.
            7. Merge the feature branch into ``development``.
            8. Delete the feature branch.
            9. Inform the Tech Lead that the task is complete.

        Steps 4-6 repeat for up to ``MAX_REVIEW_ITERATIONS`` (20) rounds.
        The loop exits early when no issues are reported.

        Preconditions:
            - ``repo_path`` is a valid git repository.
            - The ``development`` branch exists.
            - All agent references are initialised and callable.

        Postconditions:
            - On success: code is merged into ``development``, feature branch is deleted,
              and the Tech Lead has been notified.
            - On failure: the repo is checked out back to ``development`` and
              ``BackendWorkflowResult.failure_reason`` is populated.

        Args:
            repo_path: Absolute path to the git repository.
            task: The Task object assigned by the Tech Lead.
            spec_content: Full project specification text.
            architecture: System architecture (may be None).
            qa_agent: QA Expert agent instance.
            security_agent: Security (Cybersecurity) agent instance.
            dbc_agent: DbC Comments agent instance.
            code_review_agent: Code Review agent instance.
            tech_lead: Tech Lead agent instance.
            build_verifier: Callable(repo_path, agent_type, task_id) -> (ok, errors).
            doc_agent: Optional Documentation agent instance.
            completed_tasks: Tasks already completed (for Tech Lead context).
            remaining_tasks: Tasks still in the queue (for Tech Lead context).
            all_tasks: Full task registry dict (for adding QA fix tasks).
            execution_queue: Mutable execution queue list (for adding QA fix tasks).

        Returns:
            BackendWorkflowResult with success status, review history, and final files.
        """
        from shared.git_utils import (
            DEVELOPMENT_BRANCH,
            checkout_branch,
            create_feature_branch,
            delete_branch,
            merge_branch,
        )
        from shared.repo_writer import write_agent_output, NO_FILES_TO_WRITE_MSG

        task_id = task.id
        workflow_start = time.monotonic()
        review_history: List[ReviewIterationRecord] = []

        logger.info(
            "[%s] WORKFLOW START: Backend agent beginning autonomous workflow "
            "for task '%s'",
            task_id,
            task.title or task.description[:80],
        )

        # ── Step 1: Create feature branch ───────────────────────────────────
        logger.info("[%s] WORKFLOW Step 1/9: Creating feature branch", task_id)
        ok, branch_msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, task_id)
        if not ok:
            logger.error(
                "[%s] WORKFLOW FAILED at Step 1: Could not create feature branch: %s",
                task_id,
                branch_msg,
            )
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=f"Feature branch creation failed: {branch_msg}",
            )
        branch_name = f"feature/{task_id}" if not task_id.startswith("feature/") else task_id
        logger.info("[%s] WORKFLOW   Branch created: %s", task_id, branch_name)

        # ── Step 2: Generate initial code ───────────────────────────────────
        logger.info("[%s] WORKFLOW Step 2/9: Generating backend code", task_id)
        current_task = task
        result: Optional[BackendOutput] = None

        # Handle clarification sub-loop (separate from the review loop)
        for clar_round in range(MAX_CLARIFICATION_ROUNDS + 1):
            existing_code = _truncate_for_context(
                _read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS
            )
            result = self.run(
                BackendInput(
                    task_description=current_task.description,
                    requirements=_task_requirements(current_task),
                    user_story=getattr(current_task, "user_story", "") or "",
                    spec_content=_truncate_for_context(
                        spec_content, MAX_EXISTING_CODE_CHARS
                    ),
                    architecture=architecture,
                    language="python",
                    existing_code=(
                        existing_code
                        if existing_code and existing_code != "# No code files found"
                        else None
                    ),
                )
            )

            if result.needs_clarification and result.clarification_requests:
                if clar_round < MAX_CLARIFICATION_ROUNDS:
                    logger.info(
                        "[%s] WORKFLOW   Clarification needed (round %d/%d), "
                        "refining task via Tech Lead",
                        task_id,
                        clar_round + 1,
                        MAX_CLARIFICATION_ROUNDS,
                    )
                    current_task = tech_lead.refine_task(
                        current_task,
                        result.clarification_requests,
                        spec_content,
                        architecture,
                    )
                    continue
                else:
                    logger.warning(
                        "[%s] WORKFLOW FAILED at Step 2: Still needs clarification "
                        "after %d rounds",
                        task_id,
                        MAX_CLARIFICATION_ROUNDS,
                    )
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        failure_reason=(
                            "Agent still needs clarification after "
                            f"{MAX_CLARIFICATION_ROUNDS} refinement rounds"
                        ),
                    )
            break  # Got code, move on

        assert result is not None, "Result should be populated after clarification loop"

        # ── Step 3: Write files and commit ──────────────────────────────────
        logger.info("[%s] WORKFLOW Step 3/9: Writing files and committing", task_id)
        # Pre-write check: if tests reference /test-generic-error but main.py doesn't include it, regenerate
        for _ in range(MAX_SAME_BUILD_FAILURES):
            missing = _test_routes_missing_from_main_py(repo_path, result.files)
            if not missing:
                break
            logger.warning(
                "[%s] WORKFLOW Step 3: Pre-write: tests reference %s but main.py missing; regenerating",
                task_id,
                missing,
            )
            result = self._regenerate_with_issues(
                repo_path=repo_path,
                current_task=current_task,
                spec_content=spec_content,
                architecture=architecture,
                code_review_issues=_build_code_review_issues_for_missing_test_routes(),
            )
        ok, write_msg = write_agent_output(repo_path, result, subdir="")
        if not ok:
            failure_reason = (
                "Backend agent did not propose any file changes for this task"
                if write_msg == NO_FILES_TO_WRITE_MSG
                else f"Initial write failed: {write_msg}"
            )
            logger.error(
                "[%s] WORKFLOW FAILED at Step 3: %s",
                task_id,
                failure_reason,
            )
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name=branch_name,
                failure_reason=failure_reason,
            )
        logger.info("[%s] WORKFLOW   Initial commit successful", task_id)

        # ── Steps 4-6: Review feedback loop ─────────────────────────────────
        logger.info(
            "[%s] WORKFLOW Steps 4-6: Entering review feedback loop "
            "(max %d iterations)",
            task_id,
            MAX_REVIEW_ITERATIONS,
        )

        last_build_error_sig: Optional[str] = None
        consecutive_same_build_failures = 0
        repeated_build_failure_reason: Optional[str] = None

        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            iter_start = time.monotonic()
            logger.info(
                "[%s] WORKFLOW ── Review iteration %d/%d ──",
                task_id,
                iteration,
                MAX_REVIEW_ITERATIONS,
            )
            record = ReviewIterationRecord(iteration=iteration)

            # ─── 4a. Build verification ─────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Build verification...",
                task_id,
                iteration,
            )
            build_ok, build_errors = build_verifier(repo_path, "backend", task_id)
            record.build_passed = build_ok
            record.build_errors = build_errors

            if not build_ok:
                logger.warning(
                    "[%s] WORKFLOW   [%d] Build FAILED: %s",
                    task_id,
                    iteration,
                    build_errors[:800],
                )
                record.action_taken = "fixed_build"
                review_history.append(record)

                # Stop if the same build error repeats (avoids infinite loop on env/config issues)
                build_error_sig = (build_errors[:800] or build_errors).strip()
                if build_error_sig == last_build_error_sig:
                    consecutive_same_build_failures += 1
                else:
                    last_build_error_sig = build_error_sig
                    consecutive_same_build_failures = 1
                if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                    repeated_build_failure_reason = (
                        f"Build failed {MAX_SAME_BUILD_FAILURES} times with the same error; "
                        "stopping to avoid loop. Last error: " + build_errors[:800]
                    )
                    logger.error(
                        "[%s] WORKFLOW   [%d] %s",
                        task_id,
                        iteration,
                        repeated_build_failure_reason[:800],
                    )
                    break

                # Feed build errors back as code-review issues and regenerate.
                code_review_issues = _build_code_review_issues_for_build_failure(build_errors)
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    code_review_issues=code_review_issues,
                )
                # Pre-write check: if tests reference /test-generic-error but result's
                # main.py doesn't include it, regenerate with targeted issue before writing.
                for _ in range(MAX_SAME_BUILD_FAILURES):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                    )
                ok, write_msg = write_agent_output(repo_path, result, subdir="")
                if not ok:
                    logger.error(
                        "[%s] WORKFLOW   [%d] Write failed after build fix: %s",
                        task_id,
                        iteration,
                        write_msg,
                    )
                continue  # Re-run build verification

            logger.info(
                "[%s] WORKFLOW   [%d] Build: PASS",
                task_id,
                iteration,
            )

            # ─── 4b. Code review ────────────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Code review...",
                task_id,
                iteration,
            )
            code_on_branch = _read_repo_code(repo_path)
            review_result = self._run_code_review(
                code_review_agent=code_review_agent,
                code=code_on_branch,
                spec_content=spec_content,
                task=current_task,
                architecture=architecture,
                existing_code=_truncate_for_context(
                    _read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS
                ),
            )
            record.code_review_approved = review_result.approved
            record.code_review_issue_count = len(review_result.issues)

            if not review_result.approved:
                logger.warning(
                    "[%s] WORKFLOW   [%d] Code review: REJECTED (%d issues)",
                    task_id,
                    iteration,
                    len(review_result.issues),
                )
                for i, issue in enumerate(review_result.issues, 1):
                    logger.warning(
                        "[%s] WORKFLOW     Issue %d: [%s] %s: %s",
                        task_id,
                        i,
                        issue.severity,
                        issue.category,
                        issue.description[:120],
                    )
                record.action_taken = "fixed_review_issues"
                review_history.append(record)

                cr_issues = [
                    i.model_dump() if hasattr(i, "model_dump") else i.dict()
                    for i in review_result.issues
                ]
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    code_review_issues=cr_issues,
                )
                # Pre-write check: preserve test-only routes in main.py
                for _ in range(MAX_SAME_BUILD_FAILURES):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                    )
                ok, write_msg = write_agent_output(repo_path, result, subdir="")
                if not ok:
                    logger.error(
                        "[%s] WORKFLOW   [%d] Write failed after code review fix: %s",
                        task_id,
                        iteration,
                        write_msg,
                    )
                continue  # Re-run from build verification

            logger.info(
                "[%s] WORKFLOW   [%d] Code review: APPROVED",
                task_id,
                iteration,
            )

            # ─── 4b2. Acceptance criteria verification (optional) ───────
            if acceptance_verifier_agent and getattr(current_task, "acceptance_criteria", None):
                code_for_verify = _read_repo_code(repo_path)
                if code_for_verify and code_for_verify != "# No code files found":
                    from acceptance_verifier_agent.models import AcceptanceVerifierInput
                    av_result = acceptance_verifier_agent.run(AcceptanceVerifierInput(
                        code=code_for_verify,
                        task_description=current_task.description,
                        acceptance_criteria=current_task.acceptance_criteria,
                        spec_content=spec_content,
                        architecture=architecture,
                        language="python",
                    ))
                    if not av_result.all_satisfied:
                        unsatisfied = [c for c in av_result.per_criterion if not c.satisfied]
                        logger.warning(
                            "[%s] WORKFLOW   [%d] Acceptance verifier: %s/%s criteria unsatisfied",
                            task_id,
                            iteration,
                            len(unsatisfied),
                            len(av_result.per_criterion),
                        )
                        code_review_issues = [
                            {
                                "severity": "major",
                                "category": "acceptance_criteria",
                                "file_path": "",
                                "description": f"Criterion not satisfied: {c.criterion}. Evidence: {c.evidence}",
                                "suggestion": f"Implement or fix code to satisfy: {c.criterion}",
                            }
                            for c in unsatisfied
                        ]
                        result = self._regenerate_with_issues(
                            repo_path=repo_path,
                            current_task=current_task,
                            spec_content=spec_content,
                            architecture=architecture,
                            code_review_issues=code_review_issues,
                        )
                        for _ in range(MAX_SAME_BUILD_FAILURES):
                            missing = _test_routes_missing_from_main_py(repo_path, result.files)
                            if not missing:
                                break
                            result = self._regenerate_with_issues(
                                repo_path=repo_path,
                                current_task=current_task,
                                spec_content=spec_content,
                                architecture=architecture,
                                code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                            )
                        ok, write_msg = write_agent_output(repo_path, result, subdir="")
                        if not ok:
                            logger.error(
                                "[%s] WORKFLOW   [%d] Write failed after acceptance verifier fix: %s",
                                task_id,
                                iteration,
                                write_msg,
                            )
                        continue  # Re-run from build verification

            # ─── 4c. Security review ────────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Triggering Security review...",
                task_id,
                iteration,
            )
            security_issues = self._run_security_review(
                security_agent=security_agent,
                repo_path=repo_path,
                task=current_task,
                architecture=architecture,
            )
            record.security_approved = len(security_issues) == 0
            record.security_issue_count = len(security_issues)

            if security_issues:
                logger.warning(
                    "[%s] WORKFLOW   [%d] Security: found %d issues",
                    task_id,
                    iteration,
                    len(security_issues),
                )
                for i, issue in enumerate(security_issues, 1):
                    logger.warning(
                        "[%s] WORKFLOW     Security Issue %d: [%s] %s",
                        task_id,
                        i,
                        issue.get("severity", "unknown"),
                        issue.get("description", "")[:120],
                    )
                record.action_taken = "fixed_security_issues"
                review_history.append(record)

                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    security_issues=security_issues,
                )
                for _ in range(MAX_SAME_BUILD_FAILURES):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                    )
                ok, write_msg = write_agent_output(repo_path, result, subdir="")
                if not ok:
                    logger.error(
                        "[%s] WORKFLOW   [%d] Write failed after security fix: %s",
                        task_id,
                        iteration,
                        write_msg,
                    )
                continue  # Re-run from build verification

            logger.info(
                "[%s] WORKFLOW   [%d] Security: APPROVED",
                task_id,
                iteration,
            )

            # ─── 4d. QA review ──────────────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Triggering QA review...",
                task_id,
                iteration,
            )
            qa_issues, qa_output = self._run_qa_review(
                qa_agent=qa_agent,
                repo_path=repo_path,
                task=current_task,
                architecture=architecture,
            )
            record.qa_approved = len(qa_issues) == 0
            record.qa_issue_count = len(qa_issues)

            # Persist QA-generated artifacts (tests, README) when provided
            artifacts_written = self._persist_qa_artifacts(
                repo_path=repo_path,
                qa_output=qa_output,
                task_id=task_id,
            )

            if qa_issues:
                logger.warning(
                    "[%s] WORKFLOW   [%d] QA: found %d issues",
                    task_id,
                    iteration,
                    len(qa_issues),
                )
                for i, issue in enumerate(qa_issues, 1):
                    logger.warning(
                        "[%s] WORKFLOW     QA Issue %d: [%s] %s",
                        task_id,
                        i,
                        issue.get("severity", "unknown"),
                        issue.get("description", "")[:120],
                    )

            # ─── 4e. DBC comments review ────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Triggering DBC comments review...",
                task_id,
                iteration,
            )
            dbc_issues_count, dbc_updated_count, dbc_compliant = (
                self._run_dbc_review(
                    dbc_agent=dbc_agent,
                    repo_path=repo_path,
                    task=current_task,
                    architecture=architecture,
                )
            )
            record.dbc_already_compliant = dbc_compliant
            record.dbc_comments_added = dbc_issues_count
            record.dbc_comments_updated = dbc_updated_count

            if not dbc_compliant:
                logger.info(
                    "[%s] WORKFLOW   [%d] DBC: %d comments added, %d updated",
                    task_id,
                    iteration,
                    dbc_issues_count,
                    dbc_updated_count,
                )
            else:
                logger.info(
                    "[%s] WORKFLOW   [%d] DBC: already compliant",
                    task_id,
                    iteration,
                )

            # ─── Step 5: Check if there are issues to fix ───────────────
            has_issues = len(qa_issues) > 0
            if not has_issues:
                # If we just wrote new tests, re-run build verification to ensure they pass
                if artifacts_written:
                    logger.info(
                        "[%s] WORKFLOW   [%d] Re-running build verification for QA-persisted tests...",
                        task_id,
                        iteration,
                    )
                    build_ok, build_errors = build_verifier(repo_path, "backend", task_id)
                    if not build_ok:
                        logger.warning(
                            "[%s] WORKFLOW   [%d] Build failed after QA artifact persist: %s",
                            task_id,
                            iteration,
                            build_errors[:500],
                        )
                        code_review_issues = _build_code_review_issues_for_build_failure(build_errors)
                        result = self._regenerate_with_issues(
                            repo_path=repo_path,
                            current_task=current_task,
                            spec_content=spec_content,
                            architecture=architecture,
                            code_review_issues=code_review_issues,
                        )
                        ok, write_msg = write_agent_output(repo_path, result, subdir="")
                        if not ok:
                            logger.error(
                                "[%s] WORKFLOW   [%d] Write failed after build fix: %s",
                                task_id,
                                iteration,
                                write_msg,
                            )
                        continue  # Re-run from build verification
                logger.info(
                    "[%s] WORKFLOW   [%d] All reviews passed -- no issues to fix",
                    task_id,
                    iteration,
                )
                record.action_taken = "no_issues"
                review_history.append(record)
                break  # All clean, proceed to merge
            else:
                # ─── Step 6: Fix QA issues and commit ───────────────────
                logger.info(
                    "[%s] WORKFLOW   [%d] Fixing %d QA issues...",
                    task_id,
                    iteration,
                    len(qa_issues),
                )
                record.action_taken = "fixed_qa_issues"
                review_history.append(record)

                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    qa_issues=qa_issues,
                )
                # Pre-write check: preserve test-only routes in main.py
                for _ in range(MAX_SAME_BUILD_FAILURES):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                    )
                ok, write_msg = write_agent_output(repo_path, result, subdir="")
                if not ok:
                    logger.error(
                        "[%s] WORKFLOW   [%d] Write failed after QA fix: %s",
                        task_id,
                        iteration,
                        write_msg,
                    )
                # Continue to next iteration (re-run all reviews)

            iter_elapsed = time.monotonic() - iter_start
            logger.info(
                "[%s] WORKFLOW   [%d] Iteration completed in %.1fs",
                task_id,
                iteration,
                iter_elapsed,
            )
        else:
            # Loop exhausted without a clean pass
            logger.warning(
                "[%s] WORKFLOW   Review loop exhausted after %d iterations "
                "-- proceeding to merge with remaining issues",
                task_id,
                MAX_REVIEW_ITERATIONS,
            )

        if repeated_build_failure_reason is not None:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name=branch_name,
                iterations_used=len(review_history),
                review_history=review_history,
                summary=result.summary if result else "",
                failure_reason=repeated_build_failure_reason,
            )

        # ── Step 7: Merge feature branch into development ───────────────────
        logger.info("[%s] WORKFLOW Step 7/9: Merging to development", task_id)
        merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
        if not merge_ok:
            logger.error(
                "[%s] WORKFLOW FAILED at Step 7: Merge failed: %s",
                task_id,
                merge_msg,
            )
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name=branch_name,
                iterations_used=len(review_history),
                review_history=review_history,
                summary=result.summary if result else "",
                failure_reason=f"Merge failed: {merge_msg}",
            )
        logger.info(
            "[%s] WORKFLOW   Merged %s into %s",
            task_id,
            branch_name,
            DEVELOPMENT_BRANCH,
        )

        # ── Step 8: Delete feature branch ───────────────────────────────────
        logger.info("[%s] WORKFLOW Step 8/9: Deleting feature branch", task_id)
        del_ok, del_msg = delete_branch(repo_path, branch_name)
        if del_ok:
            logger.info("[%s] WORKFLOW   Deleted branch %s", task_id, branch_name)
        else:
            logger.warning(
                "[%s] WORKFLOW   Could not delete branch %s: %s (non-blocking)",
                task_id,
                branch_name,
                del_msg,
            )

        # Ensure we're on development after merge
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)

        # ── Step 9: Inform Tech Lead ────────────────────────────────────────
        logger.info("[%s] WORKFLOW Step 9/9: Notifying Tech Lead", task_id)
        final_files = dict(result.files) if result else {}
        task_update = TaskUpdate(
            task_id=task_id,
            agent_type="backend",
            status="completed",
            summary=result.summary if result else "",
            files_changed=list(final_files.keys()),
            needs_followup=False,
        )

        try:
            codebase_summary = _truncate_for_context(
                _read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS
            )
            new_tasks = tech_lead.review_progress(
                task_update=task_update,
                spec_content=spec_content,
                architecture=architecture,
                completed_tasks=completed_tasks or [],
                remaining_tasks=remaining_tasks or [],
                codebase_summary=codebase_summary,
            )
            if new_tasks:
                if append_task_fn is not None:
                    for nt in new_tasks:
                        append_task_fn(nt)
                elif all_tasks is not None and execution_queue is not None:
                    for nt in new_tasks:
                        if nt.id not in all_tasks:
                            all_tasks[nt.id] = nt
                            execution_queue.append(nt.id)
                logger.info(
                    "[%s] WORKFLOW   Tech Lead created %d new tasks from review",
                    task_id,
                    len(new_tasks),
                )

            # Trigger documentation update if available
            if doc_agent is not None:
                try:
                    tech_lead.trigger_documentation_update(
                        doc_agent=doc_agent,
                        repo_path=repo_path,
                        task_update=task_update,
                        spec_content=spec_content,
                        architecture=architecture,
                        codebase_summary=codebase_summary,
                    )
                except Exception as doc_err:
                    logger.warning(
                        "[%s] WORKFLOW   Documentation update failed (non-blocking): %s",
                        task_id,
                        doc_err,
                    )
        except Exception as review_err:
            logger.warning(
                "[%s] WORKFLOW   Tech Lead review failed (non-blocking): %s",
                task_id,
                review_err,
            )

        workflow_elapsed = time.monotonic() - workflow_start
        logger.info(
            "[%s] WORKFLOW COMPLETE: merged to development in %d iterations, "
            "%.1fs total, %d files",
            task_id,
            len(review_history),
            workflow_elapsed,
            len(final_files),
        )

        return BackendWorkflowResult(
            task_id=task_id,
            success=True,
            branch_name=branch_name,
            iterations_used=len(review_history),
            final_files=final_files,
            review_history=review_history,
            summary=result.summary if result else "",
        )

    # ── Private helpers for run_workflow ─────────────────────────────────────

    def _regenerate_with_issues(
        self,
        *,
        repo_path: Path,
        current_task: Task,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        qa_issues: List[Dict[str, Any]] | None = None,
        security_issues: List[Dict[str, Any]] | None = None,
        code_review_issues: List[Dict[str, Any]] | None = None,
    ) -> BackendOutput:
        """
        Re-invoke the code generator with issues to fix.

        Preconditions:
            - ``repo_path`` is checked out on the feature branch.
            - At least one of the issue lists is non-empty.

        Postconditions:
            - Returns a new ``BackendOutput`` with fixes applied.
        """
        existing_code = _truncate_for_context(
            _read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS
        )
        return self.run(
            BackendInput(
                task_description=current_task.description,
                requirements=_task_requirements(current_task),
                user_story=getattr(current_task, "user_story", "") or "",
                spec_content=_truncate_for_context(
                    spec_content, MAX_EXISTING_CODE_CHARS
                ),
                architecture=architecture,
                language="python",
                existing_code=(
                    existing_code
                    if existing_code and existing_code != "# No code files found"
                    else None
                ),
                qa_issues=qa_issues or [],
                security_issues=security_issues or [],
                code_review_issues=code_review_issues or [],
            )
        )

    @staticmethod
    def _run_code_review(
        *,
        code_review_agent: Any,
        code: str,
        spec_content: str,
        task: Task,
        architecture: Optional[SystemArchitecture],
        existing_code: str | None = None,
    ) -> Any:
        """
        Invoke the code review agent.

        Preconditions:
            - ``code_review_agent`` is initialised.
            - ``code`` contains the files on the feature branch.

        Postconditions:
            - Returns a ``CodeReviewOutput`` with ``approved`` and ``issues``.
        """
        from code_review_agent.models import CodeReviewInput

        return code_review_agent.run(
            CodeReviewInput(
                code=code,
                spec_content=spec_content,
                task_description=task.description,
                task_requirements=_task_requirements(task),
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language="python",
                architecture=architecture,
                existing_codebase=existing_code,
            )
        )

    @staticmethod
    def _run_security_review(
        *,
        security_agent: Any,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture],
    ) -> List[Dict[str, Any]]:
        """
        Invoke the Security agent and return issues as a list of dicts.

        Preconditions:
            - security_agent is initialised.
            - Code is committed on the current branch.

        Postconditions:
            - Returns a (possibly empty) list of security issue dicts.
            - Each dict has keys: severity, category, description, location, recommendation.
        """
        from security_agent.models import SecurityInput

        code_to_review = _read_repo_code(repo_path)
        if not code_to_review or code_to_review == "# No code files found":
            return []

        sec_result = security_agent.run(
            SecurityInput(
                code=code_to_review,
                language="python",
                task_description=task.description,
                architecture=architecture,
            )
        )

        if sec_result.approved:
            return []

        return [
            v.model_dump() if hasattr(v, "model_dump") else v.dict()
            for v in (sec_result.vulnerabilities or [])
        ]

    @staticmethod
    def _run_qa_review(
        *,
        qa_agent: Any,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture],
    ) -> Tuple[List[Dict[str, Any]], Any]:
        """
        Invoke the QA agent and return issues plus full output.

        Preconditions:
            - ``qa_agent`` is initialised.
            - Code is committed on the current branch.

        Postconditions:
            - Returns (issues_list, QAOutput).
            - issues_list is a (possibly empty) list of QA issue dicts.
            - QAOutput contains integration_tests, unit_tests, readme_content for persistence.
        """
        from qa_agent.models import QAInput

        code_to_review = _read_repo_code(repo_path)
        qa_result = qa_agent.run(
            QAInput(
                code=code_to_review,
                language="python",
                task_description=task.description,
                architecture=architecture,
            )
        )

        issues = []
        if not qa_result.approved:
            issues = [
                b.model_dump() if hasattr(b, "model_dump") else b.dict()
                for b in (qa_result.bugs_found or [])
            ]
        return issues, qa_result

    @staticmethod
    def _persist_qa_artifacts(
        *,
        repo_path: Path,
        qa_output: Any,
        task_id: str,
    ) -> bool:
        """
        Persist QA-generated integration_tests, unit_tests, and readme_content to the repo.

        When QA returns non-empty artifacts, writes them to appropriate files and commits.
        New tests are picked up by the next build verification (pytest).

        Returns True if any test files were written (so caller can re-run build verification).
        """
        from shared.git_utils import write_files_and_commit

        files_to_write: Dict[str, str] = {}
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:50]

        if getattr(qa_output, "integration_tests", "").strip():
            content = qa_output.integration_tests.strip()
            if "import pytest" not in content and "import unittest" not in content:
                content = "import pytest\n\n" + content
            files_to_write[f"tests/test_integration_qa_{safe_id}.py"] = content

        if getattr(qa_output, "unit_tests", "").strip():
            content = qa_output.unit_tests.strip()
            if "import pytest" not in content and "import unittest" not in content:
                content = "import pytest\n\n" + content
            files_to_write[f"tests/test_unit_qa_{safe_id}.py"] = content

        if getattr(qa_output, "readme_content", "").strip():
            files_to_write["README.md"] = qa_output.readme_content.strip()

        if not files_to_write:
            return False

        try:
            tests_dir = repo_path / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            msg = getattr(qa_output, "suggested_commit_message", "") or "test(qa): add QA-generated tests and docs"
            if not msg or len(msg) < 10:
                msg = "test(qa): add QA-generated tests and docs"
            ok, err = write_files_and_commit(repo_path, files_to_write, msg)
            if ok:
                logger.info(
                    "[%s] Persisted QA artifacts: %s",
                    task_id,
                    list(files_to_write.keys()),
                )
                # Return True if we wrote any test files (integration or unit)
                return any(
                    p.startswith("tests/") and p.endswith(".py")
                    for p in files_to_write
                )
            else:
                logger.warning("[%s] Failed to persist QA artifacts: %s", task_id, err)
                return False
        except Exception as e:
            logger.warning("[%s] Persist QA artifacts failed (non-blocking): %s", task_id, e)
            return False

    @staticmethod
    def _run_dbc_review(
        *,
        dbc_agent: Any,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture],
    ) -> Tuple[int, int, bool]:
        """
        Invoke the DBC comments agent and commit any changes.

        Preconditions:
            - ``dbc_agent`` is initialised.
            - Code is committed on the current branch.

        Postconditions:
            - If DBC comments were added, they are committed to the branch.
            - Returns (comments_added, comments_updated, already_compliant).

        Returns:
            Tuple of (comments_added, comments_updated, already_compliant).
        """
        from dbc_comments_agent.models import DbcCommentsInput
        from shared.git_utils import write_files_and_commit

        try:
            dbc_code = _read_repo_code(repo_path)
            if not dbc_code or dbc_code == "# No code files found":
                return 0, 0, True

            dbc_result = dbc_agent.run(
                DbcCommentsInput(
                    code=dbc_code,
                    language="python",
                    task_description=task.description,
                    architecture=architecture,
                )
            )

            if not dbc_result.already_compliant and dbc_result.files:
                ok, msg = write_files_and_commit(
                    repo_path,
                    dbc_result.files,
                    dbc_result.suggested_commit_message,
                )
                if not ok:
                    logger.warning("DBC commit failed: %s", msg)

            return (
                dbc_result.comments_added,
                dbc_result.comments_updated,
                dbc_result.already_compliant,
            )
        except Exception as e:
            logger.warning("DBC review failed (non-blocking): %s", e)
            return 0, 0, True

    # ── Stateless code generation (original interface) ──────────────────────

    def run(self, input_data: BackendInput) -> BackendOutput:
        """Implement backend functionality."""
        logger.info(
            "Backend: received task - description=%s | requirements=%s | user_story=%s | language=%s | "
            "has_architecture=%s | has_existing_code=%s | has_api_spec=%s | has_spec=%s | "
            "qa_issues=%s | security_issues=%s | code_review_issues=%s",
            input_data.task_description[:120],
            input_data.requirements[:120] if input_data.requirements else "",
            input_data.user_story[:80] if input_data.user_story else "",
            input_data.language,
            input_data.architecture is not None,
            bool(input_data.existing_code),
            bool(input_data.api_spec),
            bool(input_data.spec_content),
            len(input_data.qa_issues) if input_data.qa_issues else 0,
            len(input_data.security_issues) if input_data.security_issues else 0,
            len(input_data.code_review_issues) if input_data.code_review_issues else 0,
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
            f"**Language:** {input_data.language}",
        ]
        if input_data.user_story:
            context_parts.extend(["", f"**User Story:** {input_data.user_story}"])
        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Project Specification (full spec for the application being built):**",
                "---",
                input_data.spec_content,
                "---",
            ])
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type}): {c.technology}" for c in input_data.architecture.components if c.technology],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_spec:
            context_parts.extend(["", "**API spec:**", input_data.api_spec])
        if input_data.qa_issues:
            qa_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.qa_issues
            )
            context_parts.extend(["", "**QA issues to fix (implement these):**", qa_text])
        if input_data.security_issues:
            sec_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.security_issues
            )
            context_parts.extend(["", "**Security issues to fix (implement these):**", sec_text])
        if input_data.code_review_issues:
            cr_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category', 'general')}: {i.get('description')} "
                f"(file: {i.get('file_path', 'unknown')})\n  Suggestion: {i.get('suggestion', '')}"
                for i in input_data.code_review_issues
            )
            context_parts.extend(["", "**Code review issues to resolve:**", cr_text])

        prompt = BACKEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")
        tests = data.get("tests", "")
        if tests and "\\n" in tests:
            tests = tests.replace("\\n", "\n")

        # Process files dict - unescape newlines in file contents
        raw_files = data.get("files", {})
        if raw_files and isinstance(raw_files, dict):
            for fpath, fcontent in list(raw_files.items()):
                if isinstance(fcontent, str) and "\\n" in fcontent:
                    raw_files[fpath] = fcontent.replace("\\n", "\n")
        else:
            raw_files = {}

        # Content fallback: when LLM returns raw content wrapper, try to extract files from code blocks
        if not raw_files and data.get("content"):
            from shared.llm_response_utils import extract_files_from_content
            extracted = extract_files_from_content(str(data["content"]))
            if extracted:
                raw_files = extracted
                for fpath, fcontent in list(raw_files.items()):
                    if isinstance(fcontent, str) and "\\n" in fcontent:
                        raw_files[fpath] = fcontent.replace("\\n", "\n")

        # Validate file paths
        validated_files, validation_warnings = _validate_file_paths(raw_files)
        for warn in validation_warnings:
            logger.warning("Backend output validation: %s", warn)

        # If all files were rejected but we have code, that's a problem
        if not validated_files and not data.get("needs_clarification", False):
            if raw_files:
                logger.error(
                    "Backend: ALL %d files were rejected by validation. Raw filenames: %s",
                    len(raw_files),
                    list(raw_files.keys()),
                )
            elif code:
                logger.warning("Backend: returned 'code' but no 'files' dict. Code will be written as fallback.")
            else:
                logger.error("Backend: produced no files and no code. Task may have failed.")

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "Backend: done, code=%s chars, files=%s (validated from %s), tests=%s chars, "
            "summary=%s chars, needs_clarification=%s",
            len(code), len(validated_files), len(raw_files), len(tests),
            len(summary), needs_clarification,
        )
        return BackendOutput(
            code=code,
            language=data.get("language", input_data.language),
            summary=summary,
            files=validated_files,
            tests=tests,
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
        clarification_requests=clarification_requests,
        gitignore_entries=[str(e).strip() for e in (data.get("gitignore_entries") or []) if str(e).strip()],
    )
