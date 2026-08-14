"""Build verification + one-at-a-time LLM repair loop, extracted from the SE orchestrator.

Reached in production from ``quality_gate_tools.run_build_verification`` (the
coding_team build gate) via ``SECodeEngineProvider.run_build_verification``.
The functions keep their ``_``-prefixed names so they do not collide with the
public ``run_build_verification`` wrapper in ``quality_gate_tools`` that calls
``_run_build_verification`` here.

Invariants:
    - ``_run_build_verification`` is the only consumer of
      ``EXCEPTION_HANDLER_TEST_PATTERNS``.
    - The public build-gate wrappers convert failures into a failed result
      (``(False, summary)`` or ``BuildResult(success=False)``), including
      exceptions that escape ``_run_build_verification`` /
      ``_try_build_fix_one_at_a_time``. Those two functions do not wrap every
      helper; they return ``(False, summary)`` for handled failures and
      otherwise propagate (see ``_try_build_fix_one_at_a_time`` Raises).

No ``sys.path`` mutation on import: per-team imports (``backend_code_v2_team`` /
``frontend_code_v2_team`` prompts and templates) use absolute
``software_engineering_team.*`` package paths, so importing this module — for
static analysis, test discovery, or as a transitive dependency — leaves the
interpreter path untouched (unlike the pre-existing ``sys.path.insert``
bootstrap still used in ``orchestrator.py``).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from strands import Agent

from llm_service import get_strands_model
from shared.repo_context.repo_utils import find_repo_files

logger = logging.getLogger(__name__)


EXCEPTION_HANDLER_TEST_PATTERNS = (
    "test-generic-error",
    "test_generic_exception_handler",
    "test_error_handlers",
)

# One-at-a-time build-fix loop knobs. Named (not inline magic numbers) so the
# retry ceiling and the repo-briefing char budget are discoverable and single-
# sourced; both are integration-only (this module runs the live build/LLM loop).
_MAX_BUILD_FIX_ATTEMPTS = 15
_BUILD_FIX_MAX_CODE_CHARS = 30_000

# Directories pruned from the build-fix LLM-context file collection. Kept as an
# explicit set (rather than reusing REPO_INSPECT_EXCLUDE_DIRS) so the collection
# preserves the exact pre-refactor exclusion semantics: ``build/`` is excluded
# (artifact output for backend projects) while venvs are not, matching the
# original ``rglob`` post-filter. The win over ``rglob`` is that ``os.walk``
# prunes these in place — the traversal never descends into ``node_modules`` /
# ``.git`` / ``dist`` / ``build`` / ``__pycache__`` / ``.angular`` — instead of
# enumerating every entry under them and discarding after the fact.
_BUILD_FIX_EXCLUDE_DIRS = frozenset(
    {"node_modules", ".git", "dist", "build", "__pycache__", ".angular"}
)


def _run_build_verification(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> tuple[bool, str]:
    """
    Run build verification for the given agent type.
    Returns (success, error_output).
    For frontend: runs ng build.
    For backend: runs python syntax check, then (if a tests/ dir with test_*.py
    files and requirements.txt exist) a non-fatal ``pip install -r requirements.txt``
    before pytest.
    For devops: validates .github/workflows and top-level *.yml/*.yaml files,
    then runs a docker build when a Dockerfile is present and Docker is installed.

    The v2 phase-pipeline teams (``backend_code_v2_team``/``frontend_code_v2_team``)
    pass ``"backend_code_v2"``/``"frontend_code_v2"`` as ``agent_type`` — normalize
    those to their base ``"backend"``/``"frontend"`` verification path so v2 jobs
    actually run syntax check / ``ng build`` instead of silently no-op'ing to
    ``(True, "")`` via the fallthrough at the end of this function.

    Raises:
        Propagates uncaught exceptions from ``_try_build_fix_one_at_a_time``
        (this function has no extra boundary around those calls) and from
        unbounded ``Path.rglob`` project probes. Production callers convert
        those into a failed result:
        :func:`software_engineering_team.quality_gate_tools.run_build_verification`,
        :func:`software_engineering_team.shared.deliver_utils.run_pre_merge_quality_gate`,
        and the v2 review ``_run_build_verification`` wrappers.
    """
    from shared.command_runner.angular_repair import run_ng_build_with_nvm_fallback
    from shared.command_runner.executor import (
        run_command,
        run_pytest,
        run_python_syntax_check,
    )

    base_agent_type = (
        agent_type.removesuffix("_code_v2")
        if agent_type in ("backend_code_v2", "frontend_code_v2")
        else agent_type
    )

    if (
        base_agent_type == "frontend"
    ):  # pragma: no cover  # integration-only: invokes ng build and downstream LLM fix loop
        # repo_path may be frontend repo root (package.json here) or work path (frontend/ subdir)
        frontend_dir = (
            repo_path if (repo_path / "package.json").exists() else (repo_path / "frontend")
        )
        if not (frontend_dir / "package.json").exists():
            logger.info("Build verification: no frontend project found, skipping frontend build")
            return True, ""
        from shared.command_runner.executor import is_ng_build_environment_failure

        result = run_ng_build_with_nvm_fallback(frontend_dir)
        if not result.success:
            if is_ng_build_environment_failure(result):
                # Environment (e.g. Node version) - caller should fail task, not retry
                return False, "ENV:" + result.error_summary
            # Try tool-agent build fix (review all issues, fix one at a time)
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, base_agent_type, task_id)
            if fixed:
                logger.info(
                    "Build verification passed for frontend task %s after tool-agent fix", task_id
                )
                return True, ""
            failures = result.parsed_failures("ng_build")
            if failures:
                from shared.command_runner.error_parsing import (
                    build_agent_feedback,
                    get_failure_class_tag,
                )

                feedback = build_agent_feedback(failures)
                logger.warning(
                    "Build verification failed for task %s: %s",
                    task_id,
                    get_failure_class_tag(failures[0].failure_class),
                )
                return False, feedback
            logger.warning(
                "Build verification failed for task %s: %s", task_id, result.error_summary
            )
            return False, result.error_summary
        logger.info("Build verification passed for frontend task %s", task_id)
        return True, ""

    elif base_agent_type == "backend":
        # repo_path may be backend repo root (py files here) or work path (backend/ subdir)
        backend_dir = repo_path if any(repo_path.rglob("*.py")) else (repo_path / "backend")
        if not backend_dir.exists() or not any(backend_dir.rglob("*.py")):
            logger.info("Build verification: no Python files found, skipping")
            return True, ""
        result = run_python_syntax_check(backend_dir)
        if not result.success:  # pragma: no cover  # integration-only: syntax-check + LLM fix loop
            logger.warning("Syntax check failed for task %s: %s", task_id, result.error_summary)
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, base_agent_type, task_id)
            if fixed:
                logger.info(
                    "Build verification passed for backend task %s after tool-agent fix", task_id
                )
                return True, ""
            return False, result.error_summary
        # Also try pytest if tests directory exists
        tests_dir = backend_dir / "tests"
        if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
            # Install deps before pytest so agent-added packages (e.g. sqlalchemy) are available
            req_txt = backend_dir / "requirements.txt"
            if (
                req_txt.exists()
            ):  # pragma: no cover  # integration-only: shells out to `pip install`
                try:
                    pip_result = run_command(
                        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=backend_dir,
                        timeout=120,
                    )
                    if not pip_result.success:
                        logger.warning(
                            "pip install -r requirements.txt failed (non-fatal): %s",
                            pip_result.error_summary,
                        )
                except Exception as e:
                    logger.warning("pip install before pytest failed (non-fatal): %s", e)
            test_result = run_pytest(backend_dir, python_exe=sys.executable)
            if not test_result.success:
                failures = test_result.parsed_failures("pytest")
                if failures:
                    from shared.command_runner.error_parsing import (
                        build_agent_feedback,
                        get_failure_class_tag,
                    )

                    summary = build_agent_feedback(failures)
                    logger.warning(
                        "Tests failed for task %s: %s",
                        task_id,
                        get_failure_class_tag(failures[0].failure_class),
                    )
                else:
                    summary = test_result.pytest_error_summary()
                # When failure matches exception-handler test patterns, append canonical FIX line
                if any(p in summary for p in EXCEPTION_HANDLER_TEST_PATTERNS):
                    summary += (
                        "\n\nFIX: Preserve the /test-generic-error route in app/main.py and "
                        "ensure the exception handler returns JSONResponse; do not re-raise."
                    )
                fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, base_agent_type, task_id)
                if fixed:
                    logger.info(
                        "Build verification passed for backend task %s after tool-agent fix",
                        task_id,
                    )
                    return True, ""
                return False, summary
        logger.info("Build verification passed for backend task %s", task_id)
        return True, ""

    elif (
        base_agent_type == "devops"
    ):  # pragma: no cover  # integration-only: docker build + yaml parsing on real workflow files
        # Validate YAML files and run docker build if Dockerfile exists
        import yaml

        errors: list[str] = []
        # Validate .github/workflows/*.yml
        workflows_dir = repo_path / ".github" / "workflows"
        if workflows_dir.exists():
            for yml_file in workflows_dir.glob("*.yml"):
                try:
                    content = yml_file.read_text(encoding="utf-8", errors="replace")
                    yaml.safe_load(content)
                except yaml.YAMLError as e:
                    errors.append(f"YAML parse error in {yml_file.relative_to(repo_path)}: {e}")
                except Exception as e:
                    errors.append(f"Error reading {yml_file.relative_to(repo_path)}: {e}")
        # Validate top-level *.yml and *.yaml
        for pattern in ("*.yml", "*.yaml"):
            for yml_file in repo_path.glob(pattern):
                if yml_file.name.startswith("."):
                    continue
                try:
                    content = yml_file.read_text(encoding="utf-8", errors="replace")
                    yaml.safe_load(content)
                except yaml.YAMLError as e:
                    errors.append(f"YAML parse error in {yml_file.name}: {e}")
                except Exception as e:
                    errors.append(f"Error reading {yml_file.name}: {e}")
        if errors:
            return False, "\n".join(errors[:10])

        # Docker build if Dockerfile exists and Docker is installed
        dockerfile = repo_path / "Dockerfile"
        if dockerfile.exists():
            # Check if Docker is available before attempting build
            docker_check = run_command(["docker", "--version"], cwd=repo_path, timeout=10)
            if not docker_check.success or "Command not found" in docker_check.stderr:
                logger.info(
                    "Docker not installed; skipping docker build verification for task %s. "
                    "Dockerfile was created but cannot be verified.",
                    task_id,
                )
            else:
                result = run_command(
                    ["docker", "build", "-t", "devops-verify", "."],
                    cwd=repo_path,
                    timeout=120,
                )
                if not result.success:
                    logger.warning(
                        "Docker build failed for task %s: %s", task_id, result.error_summary
                    )
                    return False, result.error_summary

        logger.info("Build verification passed for devops task %s", task_id)
        return True, ""

    return True, ""


def _try_build_fix_one_at_a_time(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> tuple[bool, str]:
    """
    Use a tool-agent style flow to identify all build issues, then fix them one at a time.

    Preconditions:
        ``agent_type`` is already normalized to the base type (``"backend"`` or
        ``"frontend"``) by the caller (:func:`_run_build_verification`) — this
        function never receives a ``_code_v2``-suffixed value.
        ``repo_path`` is a ``Path`` to the generated task repo.

    Postconditions:
        Returns ``(True, "")`` when the frontend build or backend syntax/tests
        pass after the one-at-a-time repair loop. Returns ``(False, error_summary)``
        when the project is missing, ``agent_type`` is unsupported, verification
        still fails, or a recoverable helper failure is logged and converted
        into that False result.

        Recoverable failures are logged and do **not** propagate: ng-build
        launch, ``requirements.txt`` install, per-file reads/writes, Strands
        model acquisition, and LLM calls. ``find_repo_files`` never raises
        (best-effort walk). ``run_pytest`` / ``run_command`` map subprocess
        failures (timeout, missing binary, unexpected errors) into
        ``CommandResult`` rather than raising ``subprocess.CalledProcessError``.

    Raises:
        OSError from unbounded ``Path.rglob`` project probes, and exceptions
        from ``parse_problem_solving_single_issue_template`` or the uncaught
        post-fix re-run of ``run_ng_build_with_nvm_fallback``. Those paths are
        not wrapped the way the LLM / install / file I/O helpers are.
        :func:`_run_build_verification` does not catch them either, so they
        reach the public build-gate wrappers listed on that function, which
        convert them into a failed result rather than leaking into the gate.
    """
    from shared.command_runner.angular_repair import run_ng_build_with_nvm_fallback
    from shared.command_runner.executor import (
        run_command,
        run_pytest,
        run_python_syntax_check,
    )

    # Set in the frontend/backend branches below; checked in the model-error
    # fallback so a future branch that skips assignment degrades to a plain
    # "Build failed" message rather than a NameError.
    result = None

    if (
        agent_type == "frontend"
    ):  # pragma: no cover  # integration-only: invokes ng build + LLM repair loop
        project_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
        if not (project_dir / "package.json").exists():
            return False, "No frontend project found"
        try:
            from shared.command_runner.executor import (
                is_ng_build_environment_failure,
            )

            result = run_ng_build_with_nvm_fallback(project_dir)
        except Exception as e:
            logger.warning("Build fix: ng build failed to run: %s", e)
            return False, str(e)
        if result.success:
            return True, ""
        if is_ng_build_environment_failure(result):
            return False, result.error_summary
        failures = result.parsed_failures("ng_build")
        issues = []
        for f in failures:
            issues.append(
                {
                    "description": (f.message or f.raw_excerpt or ""),
                    "file_path": (f.file_path or ""),
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix the build error."),
                }
            )
        if not issues:
            issues.append(
                {
                    "description": result.error_summary,
                    "file_path": "",
                    "recommendation": "Fix the build error.",
                }
            )
        language = "typescript"
        prompt_module = "frontend_code_v2_team.prompts"
    elif (
        agent_type == "backend"
    ):  # pragma: no cover  # integration-only: runs python syntax check + pytest + LLM repair loop
        project_dir = repo_path if any(repo_path.rglob("*.py")) else repo_path / "backend"
        if not project_dir.exists() or not any(project_dir.rglob("*.py")):
            return False, "No Python project found"
        result = run_python_syntax_check(project_dir)
        test_result = None
        issues = []
        if not result.success:
            stderr = (result.stderr or "").strip()
            if stderr.startswith("Syntax errors found:"):
                for line in stderr.split("\n")[1:]:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    path, _, msg = line.partition(":")
                    path, msg = path.strip(), msg.strip()
                    if path and msg:
                        issues.append(
                            {
                                "description": msg,
                                "file_path": path,
                                "recommendation": "Fix the syntax error in this file.",
                            }
                        )
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary,
                        "file_path": "",
                        "recommendation": "Fix the syntax errors.",
                    }
                )
        else:
            tests_dir = project_dir / "tests"
            if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
                req_txt = project_dir / "requirements.txt"
                if req_txt.exists():
                    try:
                        run_command(
                            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                            cwd=project_dir,
                            timeout=120,
                        )
                    except Exception as e:
                        logger.warning(
                            "Build fix: failed to install requirements.txt before test run: %s", e
                        )
                test_result = run_pytest(project_dir, python_exe=sys.executable)
                if not test_result.success:
                    for f in test_result.parsed_failures("pytest"):
                        issues.append(
                            {
                                "description": (f.message or f.raw_excerpt or ""),
                                "file_path": (f.file_path or ""),
                                "recommendation": (
                                    f.suggestion
                                    or f.playbook_hint
                                    or "Fix the test or implementation."
                                ),
                            }
                        )
                    if not issues:
                        issues.append(
                            {
                                "description": test_result.pytest_error_summary(),
                                "file_path": "",
                                "recommendation": "Fix the failing tests.",
                            }
                        )
        if not issues:
            return True, ""
        if test_result is not None:
            result = test_result
        language = "python"
        prompt_module = "backend_code_v2_team.prompts"
    else:
        return False, "Unsupported agent_type for build fix"

    # Read current files from project_dir (relative paths)
    current_files: dict[str, str] = {}
    ext_map = {
        "frontend": (".ts", ".tsx", ".html", ".scss", ".css", ".js", ".jsx"),
        "backend": (".py",),
    }
    exts = ext_map.get(agent_type, (".py",))
    max_chars = _BUILD_FIX_MAX_CODE_CHARS
    total = 0
    # Pruned os.walk (find_repo_files) so excluded subtrees are never descended
    # into — the prior ``rglob("*{ext}")`` materialized every entry under
    # node_modules/.git/dist/build/__pycache__/.angular before the post-filter
    # discarded them, the same redundant I/O the streamed repo-walk refactor
    # removed elsewhere. ``find_repo_files`` returns regular files only, so the
    # old ``is_file()`` guard is now handled inside it.
    for f in find_repo_files(project_dir, suffixes=exts, exclude_dirs=_BUILD_FIX_EXCLUDE_DIRS):
        try:
            rel = str(f.relative_to(project_dir))
            content = f.read_text(encoding="utf-8", errors="replace")
            current_files[rel] = content
            total += len(content) + len(rel)
            if total > max_chars:
                break
        except Exception as e:
            logger.debug("Build fix: could not read file %s: %s", f, e)
            continue

    try:
        # response_format="text": the build-fix loop parses the assistant
        # content as the template-based output of
        # parse_problem_solving_single_issue_template, not JSON. JSON mode
        # would break the template parser.
        _build_fix_model = get_strands_model("build_fix_specialist", response_format="text")
    except Exception as e:
        logger.warning("Build fix: could not get model: %s", e)
        return False, result.error_summary if result is not None else "Build failed"

    from software_engineering_team.backend_code_v2_team.output_templates import (
        parse_problem_solving_single_issue_template,
    )

    if prompt_module == "frontend_code_v2_team.prompts":
        from software_engineering_team.frontend_code_v2_team.prompts import (
            PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT,
        )

        language_conventions = ""
    else:
        from software_engineering_team.backend_code_v2_team.prompts import (
            JAVA_CONVENTIONS,
            PYTHON_CONVENTIONS,
        )
        from software_engineering_team.backend_code_v2_team.prompts import (
            PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT,
        )

        language_conventions = JAVA_CONVENTIONS if language == "java" else PYTHON_CONVENTIONS

    max_fix_attempts = _MAX_BUILD_FIX_ATTEMPTS
    for attempt in range(
        max_fix_attempts
    ):  # pragma: no cover  # integration-only: LLM fix loop reruns build/test after each repair
        if not issues:
            break
        issue = issues.pop(0)
        desc = issue["description"]
        logger.info(
            "[%s] Build fix attempt %d/%d: Next step -> Fixing issue: %s",
            task_id,
            attempt + 1,
            max_fix_attempts,
            desc[:80],
        )
        file_path = issue.get("file_path") or ""
        rec = issue.get("recommendation") or "Fix the issue."
        # Build relevant code snippet
        if file_path and file_path in current_files:
            relevant_code = f"--- {file_path} ---\n{current_files[file_path][:50_000]}"
        else:
            parts = []
            remaining = 50_000
            for p, c in current_files.items():
                if remaining <= 0:
                    break
                snippet = c[:remaining]
                parts.append(f"--- {p} ---\n{snippet}")
                remaining -= len(snippet)
            relevant_code = "\n".join(parts) if parts else "(no code)"
        if prompt_module == "frontend_code_v2_team.prompts":
            prompt = FIX_PROMPT.format(
                source="build",
                severity="critical",
                description=desc,
                file_path=file_path or "N/A",
                recommendation=rec,
                current_code=relevant_code,
            )
        else:
            prompt = FIX_PROMPT.format(
                language_conventions=language_conventions,
                source="build",
                severity="critical",
                description=desc,
                file_path=file_path or "N/A",
                recommendation=rec,
                current_code=relevant_code,
            )
        try:
            _agent = Agent(model=_build_fix_model)
            _result = _agent(prompt)
            raw = str(_result).strip()
        except Exception as e:
            logger.warning(
                "[%s] Build fix attempt %d/%d failed: LLM call error: %s. Next step -> Skipping to next issue",
                task_id,
                attempt + 1,
                max_fix_attempts,
                e,
            )
            continue
        parsed = parse_problem_solving_single_issue_template(raw)
        fixed_files = parsed.get("files") or {}
        if not fixed_files:
            continue
        for rel_path, content in fixed_files.items():
            out_path = project_dir / rel_path
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
                current_files[rel_path] = content
            except Exception as e:
                logger.warning("Build fix: could not write %s: %s", rel_path, e)
        # Re-run build
        if agent_type == "frontend":
            result = run_ng_build_with_nvm_fallback(project_dir)
        else:
            result = run_python_syntax_check(project_dir)
            if result.success:
                tests_dir = project_dir / "tests"
                if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
                    result = run_pytest(project_dir, python_exe=sys.executable)
        if result.success:
            logger.info(
                "Build fix (tool agent): task %s build passed after fixing one issue at a time",
                task_id,
            )
            return True, ""
        # Collect remaining issues for next iteration
        if agent_type == "frontend":
            failures = result.parsed_failures("ng_build")
            issues = [
                {
                    "description": (f.message or f.raw_excerpt or ""),
                    "file_path": (f.file_path or ""),
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix."),
                }
                for f in failures
            ]
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary,
                        "file_path": "",
                        "recommendation": "Fix.",
                    }
                )
        else:
            if not result.success:
                stderr = (result.stderr or "").strip()
                issues = []
                if stderr.startswith("Syntax errors found:"):
                    for line in stderr.split("\n")[1:]:
                        line = line.strip()
                        if ":" in line:
                            path, _, msg = line.partition(":")
                            path, msg = path.strip(), msg.strip()
                            if path and msg:
                                issues.append(
                                    {
                                        "description": msg,
                                        "file_path": path,
                                        "recommendation": "Fix syntax.",
                                    }
                                )
                if not issues:
                    issues.append(
                        {
                            "description": result.error_summary,
                            "file_path": "",
                            "recommendation": "Fix.",
                        }
                    )
            else:
                test_result = run_pytest(project_dir, python_exe=sys.executable)
                result = test_result
                if not result.success:
                    issues = [
                        {
                            "description": (f.message or f.raw_excerpt or ""),
                            "file_path": (f.file_path or ""),
                            "recommendation": (f.suggestion or f.playbook_hint or "Fix."),
                        }
                        for f in result.parsed_failures("pytest")
                    ]
                    if not issues:
                        issues.append(
                            {
                                "description": result.pytest_error_summary(),
                                "file_path": "",
                                "recommendation": "Fix.",
                            }
                        )

    error_summary = (
        result.error_summary
        if hasattr(result, "error_summary")
        else "Build still failing after fix attempts"
    )
    logger.error(
        "[%s] Build fix exhausted. Recovery summary: attempted %d fix iterations, "
        "each applying LLM-generated patches then re-running build. Final error: %s",
        task_id,
        max_fix_attempts,
        error_summary,
    )
    return False, error_summary
