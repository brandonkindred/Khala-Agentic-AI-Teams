"""Frontend Expert agent: Angular implementation."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task, TaskUpdate

from .models import FrontendInput, FrontendOutput, FrontendWorkflowResult
from .prompts import FRONTEND_PROMPT

logger = logging.getLogger(__name__)

# Workflow constants
MAX_CODE_REVIEW_ITERATIONS = 10
MAX_CLARIFICATION_REFINEMENTS = 10
MAX_EXISTING_CODE_CHARS = 40_000
MAX_API_SPEC_CHARS = 20_000


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


def _read_repo_code(repo_path: Path, extensions: List[str] | None = None) -> str:
    """Read code files from repo, concatenated."""
    if extensions is None:
        extensions = [".ts", ".tsx", ".html", ".scss"]
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
    """Truncate text for agent context."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n\n... [truncated, {len(text) - max_chars} more chars]"

# Validation constants
MAX_PATH_SEGMENT_LENGTH = 30
ANGULAR_PATH_PATTERN = re.compile(r"^src/")
BAD_NAME_PATTERN = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+")  # 4+ hyphenated words = likely sentence
VERB_PREFIX_PATTERN = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)-"
)
FILLER_WORD_PATTERN = re.compile(r"-(the|that|with|using|which|for|and|a|an)-")

# Well-known directory names that are always allowed
_ALLOWED_DIRS = frozenset({
    "src", "app", "lib", "components", "services", "models", "guards", "pipes",
    "shared", "pages", "features", "assets", "styles", "environments", "modules",
    "interceptors", "directives", "utils", "helpers", "test", "spec", "dist", "node_modules",
})

# Only browser-compatible file extensions are allowed
_ALLOWED_EXTENSIONS = frozenset({
    ".ts", ".html", ".scss", ".css", ".json",
})


def _validate_file_paths(files: Dict[str, str]) -> tuple[Dict[str, str], list[str]]:
    """
    Validate and sanitize file paths from LLM output.

    Returns (validated_files, warnings).
    Rejects files with:
    - Paths not starting with 'src/' (Angular project root)
    - Non-browser file extensions (only .ts, .html, .scss, .css, .json allowed)
    - Path segments > MAX_PATH_SEGMENT_LENGTH
    - Names that look like sentences (4+ hyphenated words)
    - Names starting with verbs (implement-, create-, build-, etc.)
    - Names containing filler words (-the-, -with-, -using-, etc.)
    - Empty content
    """
    validated = {}
    warnings = []
    for path, content in files.items():
        # Reject files not under src/ (Angular project root)
        if not ANGULAR_PATH_PATTERN.match(path):
            warnings.append(f"Path does not start with 'src/' (not Angular project structure): '{path}'")
            continue

        # Reject non-browser file extensions (e.g. .py, .java)
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if ext not in _ALLOWED_EXTENSIONS:
            warnings.append(f"File extension '{ext}' is not a browser-compatible frontend file: '{path}'")
            continue

        segments = path.split("/")
        bad_segment = False
        for seg in segments:
            name_part = seg.split(".")[0]  # strip extension
            if not name_part:  # pragma: no cover
                continue
            # Skip well-known directory names
            if name_part.lower() in _ALLOWED_DIRS:
                continue
            if len(name_part) > MAX_PATH_SEGMENT_LENGTH:
                warnings.append(f"Path segment too long (likely task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(f"Path segment looks like a sentence (4+ hyphenated words): '{seg}' in '{path}'")
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

        # Ensure content is non-empty
        if not content or not content.strip():
            warnings.append(f"Empty file content for '{path}' - skipping")
            continue

        validated[path] = content

    return validated, warnings


class FrontendExpertAgent:
    """
    Frontend expert that implements solutions using Angular.
    Validates output to ensure proper naming conventions and project structure.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: FrontendInput) -> FrontendOutput:
        """Implement frontend functionality in Angular."""
        logger.info(
            "Frontend: received task - description=%s | requirements=%s | user_story=%s | "
            "has_architecture=%s | has_existing_code=%s | has_api_endpoints=%s | has_spec=%s | "
            "qa_issues=%s | security_issues=%s | accessibility_issues=%s | code_review_issues=%s",
            input_data.task_description[:120],
            input_data.requirements[:120] if input_data.requirements else "",
            input_data.user_story[:80] if input_data.user_story else "",
            input_data.architecture is not None,
            bool(input_data.existing_code),
            bool(input_data.api_endpoints),
            bool(input_data.spec_content),
            len(input_data.qa_issues) if input_data.qa_issues else 0,
            len(input_data.security_issues) if input_data.security_issues else 0,
            len(input_data.accessibility_issues) if input_data.accessibility_issues else 0,
            len(input_data.code_review_issues) if input_data.code_review_issues else 0,
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
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
                *[f"- {c.name} ({c.type})" for c in input_data.architecture.components if c.type == "frontend"],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_endpoints:
            context_parts.extend(["", "**API endpoints:**", input_data.api_endpoints])
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
        if input_data.accessibility_issues:
            a11y_text = "\n".join(
                f"- [{i.get('severity')}] WCAG {i.get('wcag_criterion', '')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.accessibility_issues
            )
            context_parts.extend(["", "**Accessibility issues to fix (implement these):**", a11y_text])
        if input_data.code_review_issues:
            cr_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category', 'general')}: {i.get('description')} "
                f"(file: {i.get('file_path', 'unknown')})\n  Suggestion: {i.get('suggestion', '')}"
                for i in input_data.code_review_issues
            )
            context_parts.extend(["", "**Code review issues to resolve:**", cr_text])

        prompt = FRONTEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")

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

        # Validate file paths - reject bad names/empty files
        validated_files, validation_warnings = _validate_file_paths(raw_files)
        for warn in validation_warnings:
            logger.warning("Frontend output validation: %s", warn)

        # If all files were rejected but we have code, that's a problem - log it
        if not validated_files and not data.get("needs_clarification", False):  # pragma: no cover
            if raw_files:
                logger.error(
                    "Frontend: ALL %d files were rejected by validation. Raw filenames: %s",
                    len(raw_files),
                    list(raw_files.keys()),
                )
            elif code:
                logger.warning("Frontend: returned 'code' but no 'files' dict. Code will be written as fallback.")
            else:
                logger.error("Frontend: produced no files and no code. Task may have failed.")

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "Frontend: done, code=%s chars, files=%s (validated from %s), summary=%s chars, needs_clarification=%s",
            len(code), len(validated_files), len(raw_files), len(summary), needs_clarification,
        )
        npm_packages = data.get("npm_packages_to_install") or []
        if not isinstance(npm_packages, list):
            npm_packages = [str(npm_packages)] if npm_packages else []
        npm_packages = [str(p).strip() for p in npm_packages if str(p).strip()]

        return FrontendOutput(
            code=code,
            summary=summary,
            files=validated_files,
            components=data.get("components", []),
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
            gitignore_entries=[str(e).strip() for e in (data.get("gitignore_entries") or []) if str(e).strip()],
            npm_packages_to_install=npm_packages,
        )

    def run_workflow(
        self,
        *,
        repo_path: Path,
        backend_dir: Path,
        task: Task,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        qa_agent: Any,
        accessibility_agent: Any,
        security_agent: Any,
        code_review_agent: Any,
        acceptance_verifier_agent: Any | None = None,
        dbc_agent: Any = None,
        tech_lead: Any = None,
        build_verifier: Callable[..., Tuple[bool, str]],
        doc_agent: Any | None = None,
        completed_tasks: List[Task] | None = None,
        remaining_tasks: List[Task] | None = None,
        all_tasks: Dict[str, Task] | None = None,
        append_backend_task_fn: Optional[Callable[[Task], None]] = None,
        append_frontend_task_fn: Optional[Callable[[str], None]] = None,
    ) -> FrontendWorkflowResult:
        """
        Execute the full frontend task lifecycle: branch, generate, review, merge, Tech Lead.

        Quality gates: build -> code review -> QA -> accessibility -> security -> DBC -> merge.
        """
        from shared.command_runner import run_command_with_nvm
        from shared.git_utils import (
            DEVELOPMENT_BRANCH,
            checkout_branch,
            create_feature_branch,
            delete_branch,
            merge_branch,
        )
        from shared.repo_writer import write_agent_output, NO_FILES_TO_WRITE_MSG

        task_id = task.id
        branch_name = f"feature/{task_id}"

        try:
            ok, msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, task_id)
            if not ok:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=f"Feature branch failed: {msg}",
                )
        except Exception as e:
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=f"Feature branch failed: {e}",
            )

        from shared.command_runner import ensure_frontend_dependencies_installed
        install_result = ensure_frontend_dependencies_installed(repo_path)
        if not install_result.success:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason="Frontend dependency install failed: " + (
                    install_result.error_summary or install_result.stderr or "unknown"
                ),
            )

        qa_issues: List[Dict[str, Any]] = []
        sec_issues: List[Dict[str, Any]] = []
        a11y_issues: List[Dict[str, Any]] = []
        code_review_issues: List[Dict[str, Any]] = []
        result: Optional[FrontendOutput] = None
        current_task = task

        for iteration_round in range(MAX_CODE_REVIEW_ITERATIONS):
            existing_code = _truncate_for_context(
                _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"]),
                MAX_EXISTING_CODE_CHARS,
            )
            api_endpoints = _truncate_for_context(
                _read_repo_code(backend_dir, [".py"]),
                MAX_API_SPEC_CHARS,
            )

            result = self.run(FrontendInput(
                task_description=current_task.description,
                requirements=_task_requirements(current_task),
                user_story=getattr(current_task, "user_story", "") or "",
                spec_content=_truncate_for_context(spec_content, MAX_EXISTING_CODE_CHARS),
                architecture=architecture,
                existing_code=existing_code if existing_code != "# No code files found" else None,
                api_endpoints=api_endpoints if api_endpoints != "# No code files found" else None,
                qa_issues=qa_issues,
                security_issues=sec_issues,
                accessibility_issues=a11y_issues,
                code_review_issues=code_review_issues,
            ))

            if result.needs_clarification and result.clarification_requests:
                if iteration_round < MAX_CLARIFICATION_REFINEMENTS:
                    current_task = tech_lead.refine_task(
                        current_task, result.clarification_requests, spec_content, architecture,
                    )
                    code_review_issues = []
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="Agent needs clarification after max refinements",
                )

            ok, write_msg = write_agent_output(repo_path, result, subdir="")
            if not ok:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                failure_reason = (
                    "Frontend agent did not propose any file changes for this task"
                    if write_msg == NO_FILES_TO_WRITE_MSG
                    else f"Write failed: {write_msg}"
                )
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=failure_reason,
                )

            if result.npm_packages_to_install:
                install_cmd = ["npm", "install", "--save"] + result.npm_packages_to_install
                install_res = run_command_with_nvm(install_cmd, cwd=repo_path)
                if not install_res.success:
                    logger.warning(
                        "[%s] npm install for packages %s failed: %s",
                        task_id, result.npm_packages_to_install, install_res.stderr[:500],
                    )

            build_ok, build_errors = build_verifier(repo_path, "frontend", task_id)
            if not build_ok:
                if build_errors.startswith("ENV:"):
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return FrontendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        failure_reason="Unsupported environment: " + build_errors[4:].strip()[:500],
                    )
                code_review_issues = [{
                    "severity": "critical",
                    "category": "logic",
                    "file_path": "",
                    "description": f"ng build failed: {build_errors[:2000]}",
                    "suggestion": "Fix the Angular compilation errors",
                }]
                continue

            code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
            existing_code_ctx = _truncate_for_context(code_on_branch, MAX_EXISTING_CODE_CHARS)
            review_result = self._run_code_review(
                code_review_agent=code_review_agent,
                code=code_on_branch,
                spec_content=spec_content,
                task=current_task,
                architecture=architecture,
                existing_code=existing_code_ctx,
            )
            if not review_result.approved:
                code_review_issues = [
                    i.model_dump() if hasattr(i, "model_dump") else i.dict()
                    for i in (review_result.issues or [])
                ]
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="Code review did not approve after max iterations",
                )

            # Acceptance criteria verification (optional)
            if acceptance_verifier_agent and getattr(current_task, "acceptance_criteria", None):
                code_for_verify = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                if code_for_verify and code_for_verify != "# No code files found":
                    from acceptance_verifier_agent.models import AcceptanceVerifierInput
                    av_result = acceptance_verifier_agent.run(AcceptanceVerifierInput(
                        code=code_for_verify,
                        task_description=current_task.description,
                        acceptance_criteria=current_task.acceptance_criteria,
                        spec_content=spec_content,
                        architecture=architecture,
                        language="typescript",
                    ))
                    if not av_result.all_satisfied:
                        unsatisfied = [c for c in av_result.per_criterion if not c.satisfied]
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
                        if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                            continue
                        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                        return FrontendWorkflowResult(
                            task_id=task_id,
                            success=False,
                            failure_reason="Acceptance criteria not satisfied after max iterations",
                        )

            code_review_issues = []
            code_to_review = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])

            from qa_agent.models import QAInput
            qa_result = qa_agent.run(QAInput(
                code=code_to_review,
                language="typescript",
                task_description=current_task.description,
                architecture=architecture,
            ))
            from accessibility_agent.models import AccessibilityInput
            a11y_result = accessibility_agent.run(AccessibilityInput(
                code=code_to_review,
                language="typescript",
                task_description=current_task.description,
                architecture=architecture,
            ))
            from security_agent.models import SecurityInput
            sec_result = security_agent.run(SecurityInput(
                code=code_to_review,
                language="typescript",
                task_description=current_task.description,
                architecture=architecture,
            ))

            qa_issues = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_result.bugs_found or [])]
            a11y_issues = [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in (a11y_result.issues or [])]
            sec_issues = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_result.vulnerabilities or [])]

            all_approved = qa_result.approved and a11y_result.approved and sec_result.approved
            if not all_approved:
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="QA, accessibility, or security did not approve after max iterations",
                )

            if all_tasks and append_backend_task_fn:
                fix_tasks = tech_lead.evaluate_qa_and_create_fix_tasks(
                    current_task, qa_result, spec_content, architecture,
                )
                if fix_tasks:
                    for ft in fix_tasks:
                        if getattr(ft, "assignee", None) == "backend":
                            all_tasks[ft.id] = ft
                            append_backend_task_fn(ft)

            self._run_dbc_review(
                dbc_agent=dbc_agent,
                repo_path=repo_path,
                task_id=task_id,
                task_description=current_task.description,
                architecture=architecture,
            )

            merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
            if merge_ok:
                delete_branch(repo_path, branch_name)
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)

                if doc_agent and completed_tasks is not None and remaining_tasks is not None:
                    task_update = TaskUpdate(
                        task_id=task_id,
                        agent_type="frontend",
                        status="completed",
                        summary=result.summary if result else "",
                        files_changed=list((result.files or {}).keys()) if result else [],
                        needs_followup=False,
                    )
                    codebase_summary = _truncate_for_context(
                        _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"]),
                        MAX_EXISTING_CODE_CHARS,
                    )
                    new_tasks = tech_lead.review_progress(
                        task_update=task_update,
                        spec_content=spec_content,
                        architecture=architecture,
                        completed_tasks=completed_tasks,
                        remaining_tasks=remaining_tasks,
                        codebase_summary=codebase_summary,
                    )
                    if new_tasks and append_frontend_task_fn:
                        for nt in new_tasks:
                            if all_tasks and nt.id not in all_tasks:
                                all_tasks[nt.id] = nt
                            append_frontend_task_fn(nt.id)
                    if doc_agent:
                        tech_lead.trigger_documentation_update(
                            doc_agent=doc_agent,
                            repo_path=repo_path,
                            task_update=task_update,
                            spec_content=spec_content,
                            architecture=architecture,
                            codebase_summary=codebase_summary,
                        )

                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=True,
                    summary=result.summary if result else "",
                )
            else:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=f"Merge failed: {merge_msg}",
                )

        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return FrontendWorkflowResult(
            task_id=task_id,
            success=False,
            failure_reason="Review loop exhausted without merge",
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
        """Invoke the code review agent on frontend code."""
        from code_review_agent.models import CodeReviewInput
        return code_review_agent.run(CodeReviewInput(
            code=code,
            spec_content=spec_content,
            task_description=task.description,
            task_requirements=_task_requirements(task),
            acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
            language="typescript",
            architecture=architecture,
            existing_codebase=existing_code,
        ))

    @staticmethod
    def _run_dbc_review(
        *,
        dbc_agent: Any,
        repo_path: Path,
        task_id: str,
        task_description: str,
        architecture: Optional[SystemArchitecture],
    ) -> None:
        """Run DBC comments agent on frontend code and commit if changes made."""
        from dbc_comments_agent.models import DbcCommentsInput
        from shared.git_utils import write_files_and_commit

        try:
            dbc_code = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
            if not dbc_code or dbc_code == "# No code files found":
                return
            dbc_result = dbc_agent.run(DbcCommentsInput(
                code=dbc_code,
                language="typescript",
                task_description=task_description,
                architecture=architecture,
            ))
            if not dbc_result.already_compliant and dbc_result.files:
                write_files_and_commit(
                    repo_path,
                    dbc_result.files,
                    dbc_result.suggested_commit_message,
                )
        except Exception as e:
            logger.warning("[%s] DBC review failed (non-blocking): %s", task_id, e)
