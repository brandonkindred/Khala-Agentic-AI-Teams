"""Frontend Expert agent: framework-native frontend implementation (React/Angular)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import (
    LLMClient,
    LLMUnreachableAfterRetriesError,
    call_llm_with_retries,
    get_strands_model,
)
from software_engineering_team.shared.frontend_framework import resolve_frontend_framework
from software_engineering_team.shared.job_store import LLM_UNREACHABLE_AFTER_RETRIES
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskUpdate
from software_engineering_team.shared.prompt_utils import (
    build_problem_solving_header,
    log_llm_prompt,
)
from software_engineering_team.shared.repo_utils import (
    FRONTEND_EXTENSIONS,
    REPO_EXCLUDE_DIRS,
    read_repo_code,
    truncate_for_context,
)
from software_engineering_team.shared.repo_utils import (
    int_env as _int_env,
)
from software_engineering_team.shared.task_plan import TaskPlan
from software_engineering_team.shared.task_utils import (
    task_requirements,
    task_requirements_with_expectations,
)

from .models import FrontendInput, FrontendOutput, FrontendWorkflowResult
from .prompts import FRONTEND_PLANNING_PROMPT, FRONTEND_PROMPT

logger = logging.getLogger(__name__)


MAX_CODE_REVIEW_ITERATIONS = _int_env("SW_MAX_CODE_REVIEW_ITERATIONS", 100)
MAX_CLARIFICATION_REFINEMENTS = _int_env("SW_MAX_CLARIFICATION_REFINEMENTS", 100)
MAX_SAME_BUILD_FAILURES = _int_env("SW_MAX_SAME_BUILD_FAILURES", 3)

_task_requirements = task_requirements


def _resolved_framework_for_implementation(
    task_metadata: Optional[dict],
    spec_content: Optional[str],
) -> str:
    """
    Resolve framework from task metadata, spec content, or project files.
    Returns "react", "angular", "vue", or "react" as default if none detected.
    """
    resolved = resolve_frontend_framework(task_metadata, spec_content)
    if resolved is None:
        return "react"
    return resolved


def _task_requirements_with_route_expectations(task: Task, repo_path: Path) -> str:
    """Build requirements string including route/component expectations from repo."""
    return task_requirements_with_expectations(task, repo_path, "frontend")


_READ_REPO_EXCLUDE_PARTS = REPO_EXCLUDE_DIRS


def _read_repo_code(repo_path: Path, extensions: List[str] | None = None) -> str:
    """Read code files from repo, concatenated. Delegates to shared.repo_utils."""
    if extensions is None:
        extensions = FRONTEND_EXTENSIONS
    return read_repo_code(repo_path, extensions)


_truncate_for_context = truncate_for_context


# ---------------------------------------------------------------------------
# Build-fix helpers (frontend-specific, analogous to backend_agent helpers)
# ---------------------------------------------------------------------------


def _extract_affected_file_paths_from_frontend_build_errors(
    build_errors: str, repo_path: Path
) -> List[str]:
    """Extract TypeScript/template file paths mentioned in ng build errors."""
    seen: set = set()
    paths: List[str] = []
    for m in re.finditer(r"(src/[^\s:\"']+\.(?:ts|html|scss|css)):?\d*", build_errors):
        p = m.group(1).strip()
        if p and p not in seen and (repo_path / p).exists():
            seen.add(p)
            paths.append(p)
    for m in re.finditer(r'Could not resolve ["\']([^"\']+)["\']', build_errors):
        raw = m.group(1)
        if raw.startswith("./"):
            raw = "src/" + raw[2:]
        for ext in (".ts", ".component.ts"):
            candidate = raw if raw.endswith(ext) else raw + ext
            if candidate not in seen and (repo_path / candidate).exists():
                seen.add(candidate)
                paths.append(candidate)
    routes_file = "src/app/app.routes.ts"
    if routes_file not in seen and (repo_path / routes_file).exists():
        paths.insert(0, routes_file)
    return paths[:10]


def _read_frontend_affected_files_code(repo_path: Path, file_paths: List[str]) -> str:
    """Read content of affected frontend files for BuildFixSpecialist context."""
    parts: List[str] = []
    total = 0
    for p in file_paths:
        f = repo_path / p
        if f.is_file():
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### {p} ###\n{content}")
                total += len(content)
                if total > 12000:
                    break
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "# No affected files found"


def _apply_frontend_build_fix_edits(
    repo_path: Path, edits: List[Any]
) -> Tuple[bool, str, Dict[str, str]]:
    """Apply BuildFixSpecialist edits to frontend files.

    Returns (success, message, files_dict_for_write).
    """
    from build_fix_specialist.models import CodeEdit

    file_edits: Dict[str, List[Any]] = {}
    for edit in edits:
        if not isinstance(edit, CodeEdit):
            continue
        file_edits.setdefault(edit.file_path, []).append(edit)

    files_to_write: Dict[str, str] = {}
    for file_path, edit_list in file_edits.items():
        fp = repo_path / file_path
        if not fp.exists():
            logger.warning("BuildFixSpecialist edit target not found: %s", file_path)
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("BuildFixSpecialist could not read %s: %s", file_path, exc)
            continue
        for edit in edit_list:
            if edit.old_text not in content:
                logger.warning(
                    "BuildFixSpecialist old_text not found in %s",
                    file_path,
                )
                return False, f"old_text not found in {file_path}", {}
            content = content.replace(edit.old_text, edit.new_text, 1)
        files_to_write[file_path] = content

    if not files_to_write:
        return False, "No edits could be applied", {}
    return True, f"Applied {len(files_to_write)} edit(s)", files_to_write


# Validation constants
MAX_PATH_SEGMENT_LENGTH = 30
ANGULAR_PATH_PATTERN = re.compile(r"^src/")
BAD_NAME_PATTERN = re.compile(
    r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+"
)  # 4+ hyphenated words = likely sentence
VERB_PREFIX_PATTERN = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)-"
)
FILLER_WORD_PATTERN = re.compile(r"-(the|that|with|using|which|for|and|a|an)-")

# Well-known directory names that are always allowed
_ALLOWED_DIRS = frozenset(
    {
        "src",
        "app",
        "lib",
        "components",
        "services",
        "models",
        "guards",
        "pipes",
        "shared",
        "pages",
        "features",
        "assets",
        "styles",
        "environments",
        "modules",
        "interceptors",
        "directives",
        "utils",
        "helpers",
        "test",
        "spec",
        "dist",
        "node_modules",
    }
)

# Browser-compatible and frontend-build file extensions (JS runs in browser; .env* loaded at build time)
_ALLOWED_EXTENSIONS = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".html",
        ".scss",
        ".css",
        ".json",
        ".env",  # .env.* (e.g. .env.local) allowed via basename check in _validate_file_paths
    }
)
# Fallback when llm_client is None: only these root paths are allowed. When llm_client is
# provided, root paths are validated by the LLM instead (any framework: Vite, Angular, Jest, etc.).
_ALLOWED_ROOT_FILES = frozenset(
    {
        "angular.json",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "tsconfig.app.json",
        "tsconfig.spec.json",
        "vite.config.ts",
        "vite.config.js",
        "index.html",
        "vitest.config.ts",
        "vitest.config.js",
    }
)
_ANGULAR_ROOT_FILES = _ALLOWED_ROOT_FILES  # backward compatibility


def _llm_allowed_extensions(llm_client: Any, extensions: set[str]) -> set[str]:
    """
    Ask the LLM which of the given extensions are valid for browser or frontend build.
    Returns the subset it considers allowed. Used as optional fallback when extension is not in _ALLOWED_EXTENSIONS.
    """
    if not extensions:
        return set()
    exts_str = ", ".join(sorted(extensions))
    prompt = (
        "Which of these file extensions are typically used in a browser or in a frontend build (e.g. Angular, React, Vite)? "
        "Answer with a comma-separated list of ONLY the extensions to allow (e.g. .svg, .md), or 'none' if none.\n"
        f"Extensions: {exts_str}\n"
        "Allowed:"
    )
    try:
        from strands.models.model import Model as _StrandsModel

        _model = llm_client if (llm_client is not None and isinstance(llm_client, _StrandsModel)) else get_strands_model()
        response = (lambda _r: str(_r))(Agent(model=_model)(prompt, temperature=0.0)).strip()
        text = (response or "").strip().lower()
        if not text or "none" in text:
            return set()
        allowed = set()
        for part in text.replace(",", " ").split():
            part = part.strip(".")
            if part and f".{part}" in extensions:
                allowed.add(f".{part}")
        return allowed
    except Exception:
        return set()


def _llm_allowed_root_paths(llm_client: Any, paths: list[str]) -> set[str]:
    """
    Ask the LLM which of these root-level file paths are appropriate for a frontend project
    (config, entry point, test runner, etc.). Any framework (Vite, Angular, Jest, Vitest, etc.) is fine.
    Returns the subset of paths it considers legitimate.
    """
    if not paths:
        return set()
    paths_str = "\n".join(f"- {p}" for p in sorted(paths))
    prompt = (
        "Which of these root-level file paths are legitimate for a frontend project "
        "(e.g. config files, entry HTML, test runner config, package files)? "
        "Any stack is fine: Vite, Angular, Jest, Vitest, Webpack, etc. "
        "Reply with a comma-separated list of ONLY the paths to allow, or 'none' if none.\n\n"
        f"Paths:\n{paths_str}\n\n"
        "Allowed paths:"
    )
    try:
        from strands.models.model import Model as _StrandsModel

        _model = llm_client if (llm_client is not None and isinstance(llm_client, _StrandsModel)) else get_strands_model()
        response = (lambda _r: str(_r))(Agent(model=_model)(prompt, temperature=0.0)).strip()
        text = (response or "").strip()
        if not text or "none" in text.lower():
            return set()
        allowed = set()
        for part in text.replace("\n", ",").split(","):
            p = part.strip().strip(".-")
            if p and p in paths:
                allowed.add(p)
        return allowed
    except Exception:
        return set()


def _validate_file_paths(
    files: Dict[str, str],
    llm_client: Any,
) -> tuple[Dict[str, str], list[str]]:
    """
    Validate and sanitize file paths from LLM output. Requires llm_client (no fallback).

    Returns (validated_files, warnings).
    Rejects files with:
    - Paths not under 'src/' or LLM-validated root paths (root requires LLM)
    - Non-browser file extensions (see _ALLOWED_EXTENSIONS; .env* allowed by basename)
    - Path segments > MAX_PATH_SEGMENT_LENGTH, sentence-like names, etc.
    - Empty content
    """
    validated = {}
    warnings = []
    # Root-level paths (no slash) to be validated by LLM when llm_client is provided
    root_paths_pending: list[tuple[str, str]] = []
    allowed_root_paths: set[str] = set()  # filled by LLM when processing root_paths_pending
    # First pass: accept allowed exts + .env*; collect (path, content, ext) for unknown exts
    unknown_ext_files: list[tuple[str, str, str]] = []

    def _normalize_path(raw: str) -> str:
        """Strip leading slashes and a leading 'frontend/' so paths are relative to frontend root."""
        p = raw.strip()
        if p.startswith("\ufeff"):
            p = p[1:]
        p = p.lstrip("/")
        while p.lower().startswith("frontend/"):
            p = p[9:]
        return p

    for raw_path, content in files.items():
        path = _normalize_path(raw_path)
        if not path:
            warnings.append(f"Path normalizes to empty (invalid): '{raw_path}'")
            continue
        is_src_file = bool(ANGULAR_PATH_PATTERN.match(path))
        is_root_level = "/" not in path
        if not is_src_file:
            if is_root_level:
                # Known-safe root config files are always allowed (no LLM call needed)
                if path in _ALLOWED_ROOT_FILES:
                    if content and content.strip():
                        validated[path] = content
                    continue
                if llm_client is None:
                    warnings.append(
                        f"Path must be under 'src/' (LLM required for root file validation): '{path}'"
                    )
                    continue
                root_paths_pending.append((path, content))
                continue
            else:
                # Path has slash but not under src/ (e.g. "other/dir/file.ts")
                warnings.append(f"Path must be under 'src/' or a known root config file: '{path}'")
                continue

        # Reject non-browser / non-frontend-build file extensions (e.g. .py, .java)
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        basename = path.split("/")[-1] if "/" in path else path
        is_env_file = basename.startswith(".env")  # .env, .env.local, .env.example, etc.
        if ext not in _ALLOWED_EXTENSIONS and not is_env_file:
            if llm_client is None:
                warnings.append(
                    f"File extension '{ext}' is not a browser-compatible frontend file (LLM required): '{path}'"
                )
                continue
            unknown_ext_files.append((path, content, ext))
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
                warnings.append(
                    f"Path segment too long (likely task description as name): '{seg}' in '{path}'"
                )
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(
                    f"Path segment looks like a sentence (4+ hyphenated words): '{seg}' in '{path}'"
                )
                bad_segment = True
                break
            if VERB_PREFIX_PATTERN.match(name_part):
                warnings.append(
                    f"Path segment starts with a verb (task description as name): '{seg}' in '{path}'"
                )
                bad_segment = True
                break
            if FILLER_WORD_PATTERN.search(name_part):
                warnings.append(
                    f"Path segment contains filler words (task description as name): '{seg}' in '{path}'"
                )
                bad_segment = True
                break
        if bad_segment:
            continue

        # Ensure content is non-empty
        if not content or not content.strip():
            warnings.append(f"Empty file content for '{path}' - skipping")
            continue

        validated[path] = content

    # Ask LLM which root-level paths are appropriate (any framework)
    if root_paths_pending and llm_client is not None:
        root_path_list = [p for p, _ in root_paths_pending]
        allowed_root_paths = call_llm_with_retries(
            lambda: _llm_allowed_root_paths(llm_client, root_path_list),
            max_attempts=3,
            backoff_base=2.0,
        )
        for path, content in root_paths_pending:
            if path not in allowed_root_paths:
                warnings.append(f"Root path not allowed: '{path}'")
                continue
            ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
            is_env_file = path.startswith(".env")
            if ext not in _ALLOWED_EXTENSIONS and not is_env_file:
                unknown_ext_files.append((path, content, ext))
                continue
            segments = path.split("/")
            bad_segment = False
            for seg in segments:
                name_part = seg.split(".")[0] if seg else ""
                if not name_part or name_part.lower() in _ALLOWED_DIRS:
                    continue
                if (
                    len(name_part) > MAX_PATH_SEGMENT_LENGTH
                    or BAD_NAME_PATTERN.match(name_part)
                    or VERB_PREFIX_PATTERN.match(name_part)
                    or FILLER_WORD_PATTERN.search(name_part)
                ):
                    bad_segment = True
                    break
            if bad_segment or not content or not content.strip():
                continue
            validated[path] = content
            warnings.append(f"Root path allowed via LLM: '{path}'")

    # LLM decides which unknown extensions to allow (one call per validation run)
    if unknown_ext_files and llm_client is not None:
        unknown_exts = {ext for (_, _, ext) in unknown_ext_files}
        llm_allowed = call_llm_with_retries(
            lambda: _llm_allowed_extensions(llm_client, unknown_exts),
            max_attempts=3,
            backoff_base=2.0,
        )
        for path, content, ext in unknown_ext_files:
            if ext not in llm_allowed:
                warnings.append(
                    f"File extension '{ext}' is not a browser-compatible frontend file: '{path}'"
                )
                continue
            # Same path/segment/empty checks as above (src/ or static list or LLM-allowed root)
            is_src_file = bool(ANGULAR_PATH_PATTERN.match(path))
            is_allowed_root_file = "/" not in path and (
                path in _ALLOWED_ROOT_FILES or path in allowed_root_paths
            )
            if not is_src_file and not is_allowed_root_file:
                continue
            segments = path.split("/")
            bad_segment = False
            for seg in segments:
                name_part = seg.split(".")[0] if seg else ""
                if not name_part or name_part.lower() in _ALLOWED_DIRS:
                    continue
                if (
                    len(name_part) > MAX_PATH_SEGMENT_LENGTH
                    or BAD_NAME_PATTERN.match(name_part)
                    or VERB_PREFIX_PATTERN.match(name_part)
                    or FILLER_WORD_PATTERN.search(name_part)
                ):
                    bad_segment = True
                    break
            if bad_segment or not content or not content.strip():
                continue
            validated[path] = content
            warnings.append(f"File extension '{ext}' allowed via LLM for: '{path}'")

    return validated, warnings


_ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS = (
    "1. Use the issue details in this section (e.g. NG8002, TS errors) to locate the offending components/templates.\n"
    "2. Apply minimal, localized edits. Do not recreate large portions of the app.\n"
    "3. Preserve existing working routes, DI configuration (app.config.ts), and forms.\n"
    "4. Only adjust what is necessary to resolve the errors and issues (e.g. add ReactiveFormsModule, fix bindings).\n"
    "5. Focus on resolving the provided issues before adding new features.\n"
    "6. For \"Argument of type 'string' is not assignable to parameter of type 'X'\" (e.g. in filter controls): type the source so the template gets the literal union. E.g. type the options array as ReadonlyArray<{ value: 'all' | 'active' | 'completed'; label: string }> instead of an untyped array, so option.value is not inferred as string.\n"
    "7. When fixing QA/code review issues, only change what is explicitly reported. Preserve existing property and method names in the component. E.g. if the template uses isLoading, do not rename to loading; if the template calls toggleTaskCompletion, ensure the class has that method and do not replace with a different name unless the issue explicitly asks for it."
)


class FrontendExpertAgent:
    """
    Frontend expert that implements framework-native solutions for React or Angular.
    Validates output to ensure proper naming conventions and project structure.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = llm_client

    def _plan_task(
        self,
        *,
        task: Task,
        existing_code: str,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        api_endpoints: Optional[str] = None,
    ) -> str:
        """Produce an implementation plan for the task. Returns plan markdown or empty string on failure."""
        context_parts: List[str] = [
            f"**Task:** {task.description}",
            f"**Requirements:** {_task_requirements(task)}",
        ]
        if getattr(task, "user_story", None):
            context_parts.append(f"**User Story:** {task.user_story}")
        if spec_content:
            context_parts.extend(
                [
                    "",
                    "**Project Specification:**",
                    _truncate_for_context(spec_content, 15_000),
                ]
            )
        if architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    architecture.overview,
                    *[
                        f"- {c.name} ({c.type})"
                        for c in architecture.components
                        if c.type == "frontend"
                    ],
                ]
            )
        if api_endpoints and api_endpoints != "# No code files found":
            context_parts.extend(
                [
                    "",
                    "**API endpoints (from backend):**",
                    _truncate_for_context(api_endpoints, 8_000),
                ]
            )
        if existing_code and existing_code != "# No code files found":
            context_parts.extend(
                [
                    "",
                    "**Existing codebase:**",
                    _truncate_for_context(existing_code, 25_000),
                ]
            )
        prompt = FRONTEND_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        log_llm_prompt(logger, "Frontend", "planning", (task.description or "")[:80], prompt)
        try:
            data = call_llm_with_retries(
                lambda: json.loads((lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip()),
                max_attempts=3,
                backoff_base=2.0,
            )
            plan = TaskPlan.from_llm_json(data)
            return plan.to_markdown()
        except LLMUnreachableAfterRetriesError:
            raise
        except Exception as e:
            logger.warning("[%s] Planning step failed, proceeding without plan: %s", task.id, e)
            return ""

    def run(self, input_data: FrontendInput) -> FrontendOutput:
        """Implement frontend functionality in the requested framework target."""
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
        qa_count = len(input_data.qa_issues) if input_data.qa_issues else 0
        security_count = len(input_data.security_issues) if input_data.security_issues else 0
        accessibility_count = (
            len(input_data.accessibility_issues) if input_data.accessibility_issues else 0
        )
        code_review_count = (
            len(input_data.code_review_issues) if input_data.code_review_issues else 0
        )
        has_issues = (
            qa_count > 0 or security_count > 0 or accessibility_count > 0 or code_review_count > 0
        )

        framework_target = (input_data.framework_target or "react").lower().strip()
        framework_labels = {
            "react": "Frontend / React",
            "angular": "Frontend / Angular",
            "vue": "Frontend / Vue",
        }
        framework_label = framework_labels.get(
            framework_target, f"Frontend / {framework_target.title()}"
        )

        context_parts: List[str] = [f"**Framework target:** {framework_target}"]
        if has_issues:
            issue_summaries: Dict[str, int] = {}
            if qa_count:
                issue_summaries["QA issues"] = qa_count
            if security_count:
                issue_summaries["security issues"] = security_count
            if accessibility_count:
                issue_summaries["accessibility issues"] = accessibility_count
            if code_review_count:
                issue_summaries["code review issues"] = code_review_count
            desc_lines: List[str] = []
            for i in input_data.qa_issues or []:
                desc_lines.append(
                    f"  - QA: {i.get('description', '')} (location: {i.get('location', '')})"
                )
            for i in input_data.security_issues or []:
                desc_lines.append(
                    f"  - Security [{i.get('category', '')}]: {i.get('description', '')} (location: {i.get('location', '')})"
                )
            for i in input_data.accessibility_issues or []:
                desc_lines.append(
                    f"  - Accessibility: {i.get('description', '')} (location: {i.get('location', '')})"
                )
            for i in input_data.code_review_issues or []:
                desc_lines.append(
                    f"  - Code review [{i.get('category', 'general')}]: {i.get('description', '')} (file: {i.get('file_path', 'unknown')})"
                )
            issue_descriptions = "\n".join(desc_lines) if desc_lines else None
            if getattr(input_data, "convergence_hint", None):
                issue_descriptions = (
                    (issue_descriptions + "\n\n" + input_data.convergence_hint)
                    if issue_descriptions
                    else input_data.convergence_hint
                )
            header = build_problem_solving_header(
                issue_summaries,
                framework_label,
                instructions=_ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS,
                issue_descriptions=issue_descriptions,
            )
            # Strip trailing "---" so we can put issue details inside the problem-solving section
            header_body = header.rstrip()
            if header_body.endswith("---"):
                header_body = header_body[:-3].rstrip()
            problem_block_parts: List[str] = [header_body]
            if input_data.qa_issues:
                qa_lines = []
                for i in input_data.qa_issues:
                    rec = i.get("recommendation", "")
                    desc = i.get("description", "")
                    if "does not exist" in desc and ("Property" in desc or "Method" in desc):
                        hint = " Add the missing property/method on the component class or fix the template to use the existing name."
                        rec = (rec + hint) if rec else hint.strip()
                    qa_lines.append(
                        f"- [{i.get('severity')}] {desc} (location: {i.get('location', '')})\n  Recommendation: {rec}"
                    )
                problem_block_parts.extend(
                    ["", "**QA issues to fix (implement these):**", "\n".join(qa_lines)]
                )
            if input_data.security_issues:
                sec_text = "\n".join(
                    f"- [{i.get('severity')}] {i.get('category')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                    for i in input_data.security_issues
                )
                problem_block_parts.extend(
                    ["", "**Security issues to fix (implement these):**", sec_text]
                )
            if input_data.accessibility_issues:
                a11y_text = "\n".join(
                    f"- [{i.get('severity')}] WCAG {i.get('wcag_criterion', '')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                    for i in input_data.accessibility_issues
                )
                problem_block_parts.extend(
                    ["", "**Accessibility issues to fix (implement these):**", a11y_text]
                )
            if input_data.code_review_issues:
                cr_lines = []
                for i in input_data.code_review_issues:
                    sug = i.get("suggestion", "")
                    desc = i.get("description", "")
                    if "does not exist" in desc and ("Property" in desc or "Method" in desc):
                        hint = " Add the missing property/method on the component class or fix the template to use the existing name."
                        sug = (sug + hint) if sug else hint.strip()
                    cr_lines.append(
                        f"- [{i.get('severity')}] {i.get('category', 'general')}: {desc} "
                        f"(file: {i.get('file_path', 'unknown')})\n  Suggestion: {sug}"
                    )
                problem_block_parts.extend(
                    ["", "**Code review issues to resolve:**", "\n".join(cr_lines)]
                )
            problem_block_parts.extend(["", "---"])
            context_parts.append("\n".join(problem_block_parts))
            logger.info(
                "Frontend problem-solving context: qa_issues=%d, security_issues=%d, "
                "accessibility_issues=%d, code_review_issues=%d",
                qa_count,
                security_count,
                accessibility_count,
                code_review_count,
            )
            logger.info("Frontend problem-solving header for LLM:\n%s", context_parts[0][:800])
        if input_data.task_plan:
            plan_instruction = (
                "**IMPLEMENTATION PLAN (follow this):**\n"
                "Implement the task strictly according to the Implementation plan below. "
                "Your output must realize every item under 'What changes' and 'Tests needed', "
                "and use the algorithms/data structures described. Do not deviate from the plan unless the task description explicitly contradicts it.\n\n"
                "**Implementation plan:**\n" + input_data.task_plan
            )
            context_parts.append(plan_instruction)
        context_parts.extend(
            [
                f"**Task:** {input_data.task_description}",
                f"**Requirements:** {input_data.requirements}",
            ]
        )
        if input_data.user_story:
            context_parts.extend(["", f"**User Story:** {input_data.user_story}"])
        if input_data.spec_content:
            context_parts.extend(
                [
                    "",
                    "**Project Specification (full spec for the application being built):**",
                    "---",
                    input_data.spec_content,
                    "---",
                ]
            )
        if input_data.architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    input_data.architecture.overview,
                    *[
                        f"- {c.name} ({c.type})"
                        for c in input_data.architecture.components
                        if c.type == "frontend"
                    ],
                ]
            )
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_endpoints:
            context_parts.extend(["", "**API endpoints:**", input_data.api_endpoints])
        if input_data.suggested_tests_from_qa:
            tests_block = [
                "",
                "**Suggested tests from QA/testing sub-agent – integrate these into your .spec.ts and e2e files:**",
            ]
            if input_data.suggested_tests_from_qa.get("unit_tests"):
                tests_block.extend(
                    [
                        "",
                        "**Unit tests:**",
                        "```",
                        input_data.suggested_tests_from_qa["unit_tests"],
                        "```",
                    ]
                )
            if input_data.suggested_tests_from_qa.get("integration_tests"):
                tests_block.extend(
                    [
                        "",
                        "**Integration tests:**",
                        "```",
                        input_data.suggested_tests_from_qa["integration_tests"],
                        "```",
                    ]
                )
            context_parts.extend(tests_block)

        prompt = FRONTEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        mode = "problem_solving" if has_issues else "initial"
        task_hint = (input_data.task_description or "")[:80]
        log_llm_prompt(logger, "Frontend", mode, task_hint, prompt)

        empty_retry_prompt = (
            "\n\n**CRITICAL - Your previous response was REJECTED:** "
            "You produced 0 files and 0 code characters. You MUST return a valid JSON object with a 'files' key "
            "containing at least one complete component file (path -> content). Without files, the task cannot be completed. "
            "Try again with concrete, complete file contents under src/."
        )

        data = None
        validated_files = {}
        code = ""
        raw_files = {}
        for attempt in range(2):
            data = call_llm_with_retries(
                lambda: json.loads((lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip()),
                max_attempts=3,
                backoff_base=2.0,
            )

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

            # Validate file paths - reject bad names/empty files (optional LLM fallback for unknown extensions)
            validated_files, validation_warnings = _validate_file_paths(
                raw_files, llm_client=self.llm
            )
            for warn in validation_warnings:
                logger.warning("Frontend output validation: %s", warn)

            # Guard: 0 files and 0 code -> retry once with explicit rejection message
            total_chars = sum(len(c or "") for c in (validated_files or {}).values()) + len(
                code or ""
            )
            if not data.get("needs_clarification", False) and total_chars == 0 and attempt == 0:
                logger.warning(
                    "Frontend: produced no files and no code (failure_class=empty_completion); re-prompting once",
                )
                # If files were rejected by validation, surface those errors so the LLM can fix filenames
                if raw_files and validation_warnings:
                    validation_retry = (
                        "\n\n**CRITICAL - Your file paths were REJECTED by validation:**\n"
                        + "\n".join(f"- {w}" for w in validation_warnings)
                        + "\n\nFix the file paths (e.g. use shorter names, paths under src/). "
                        "Path segments must not start with create-, add-, implement-, etc. Use task-form, task-list, or similar names. "
                        "Return valid JSON with the 'files' key."
                    )
                    prompt = prompt + validation_retry
                else:
                    # Check if this might be unresolved import fix (from qa_issues)
                    has_unresolved = any(
                        "Could not resolve" in str(i.get("description", ""))
                        or "Unresolved" in str(i.get("description", ""))
                        for i in (input_data.qa_issues or [])
                    )
                    if has_unresolved:
                        prompt = prompt + (
                            "\n\n**CRITICAL - Fix the unresolved import:** "
                            "Either (a) add the missing component files under a path that does not start with a verb "
                            "(e.g. task-form), and ensure app.routes.ts uses that path, or (b) change the import "
                            "in app.routes.ts to an existing component. Respond with valid JSON and a 'files' object "
                            "containing the changed files."
                        )
                    else:
                        prompt = prompt + empty_retry_prompt
                continue

            break

        # Fail fast when LLM produces no valid output
        if not validated_files and not data.get("needs_clarification", False):
            from llm_service import LLMPermanentError

            if raw_files:
                raise LLMPermanentError(
                    f"Frontend: LLM returned {len(raw_files)} files but all were rejected by validation. "
                    f"Raw filenames: {list(raw_files.keys())}"
                )
            if code:
                raise LLMPermanentError(
                    "Frontend: LLM returned 'code' but no 'files' dict. "
                    "Model must return structured JSON with a 'files' key."
                )
            raise LLMPermanentError(
                "Frontend: LLM produced no files and no code. Model unavailable or returned invalid output."
            )

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "Frontend: done, code=%s chars, files=%s (validated from %s), summary=%s chars, needs_clarification=%s",
            len(code),
            len(validated_files),
            len(raw_files),
            len(summary),
            needs_clarification,
        )
        npm_packages = data.get("npm_packages_to_install") or []
        if not isinstance(npm_packages, list):
            npm_packages = [str(npm_packages)] if npm_packages else []
        npm_packages = [str(p).strip() for p in npm_packages if str(p).strip()]

        return FrontendOutput(
            framework_used=(data.get("framework_used") or framework_target),
            code=code,
            summary=summary,
            files=validated_files,
            components=data.get("components", []),
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
            gitignore_entries=[
                str(e).strip() for e in (data.get("gitignore_entries") or []) if str(e).strip()
            ],
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
        linting_tool_agent: Any | None = None,
        build_fix_specialist: Any | None = None,
    ) -> FrontendWorkflowResult:
        """
        Execute the full frontend task lifecycle: branch, generate, review, merge, Tech Lead.

        Quality gates: build -> code review -> QA -> accessibility -> security -> DBC -> merge.
        """
        from software_engineering_team.shared.command_runner import run_command_with_nvm
        from software_engineering_team.shared.git_utils import (
            DEVELOPMENT_BRANCH,
            checkout_branch,
            create_feature_branch,
            delete_branch,
            merge_branch,
        )
        from software_engineering_team.shared.repo_writer import (
            NO_FILES_TO_WRITE_MSG,
            write_agent_output,
        )

        task_id = task.id
        branch_name = f"feature/{task_id}"

        try:
            ok, msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, task_id)
        except Exception as e:
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=f"Feature branch failed: {e}",
            )
        if not ok:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=f"Feature branch failed: {msg}",
            )

        from software_engineering_team.shared.command_runner import (
            ensure_frontend_dependencies_installed,
        )

        install_result = ensure_frontend_dependencies_installed(repo_path)
        if not install_result.success:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason="Frontend dependency install failed: "
                + (install_result.error_summary or install_result.stderr or "unknown"),
            )

        qa_issues: List[Dict[str, Any]] = []
        sec_issues: List[Dict[str, Any]] = []
        a11y_issues: List[Dict[str, Any]] = []
        code_review_issues: List[Dict[str, Any]] = []
        suggested_tests_from_qa: Optional[Dict[str, str]] = None
        result: Optional[FrontendOutput] = None
        current_task = task
        last_build_error_sig = ""
        consecutive_same_build_failures = 0
        write_tests_requested = False
        last_code_review_count = 0
        rounds_without_decrease = 0
        convergence_hint: Optional[str] = None

        from software_engineering_team.shared.context_sizing import (
            compute_api_spec_chars,
            compute_existing_code_chars,
            compute_spec_content_chars,
        )

        try:
            for iteration_round in range(MAX_CODE_REVIEW_ITERATIONS):
                max_code = compute_existing_code_chars(self.llm)
                max_api = compute_api_spec_chars(self.llm)
                existing_code = _truncate_for_context(
                    _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"]),
                    max_code,
                )
                api_endpoints = _truncate_for_context(
                    _read_repo_code(backend_dir, [".py"]),
                    max_api,
                )

                plan_text = ""
                if not qa_issues and not sec_issues and not a11y_issues and not code_review_issues:
                    plan_text = self._plan_task(
                        task=current_task,
                        existing_code=existing_code,
                        spec_content=spec_content,
                        architecture=architecture,
                        api_endpoints=api_endpoints
                        if api_endpoints != "# No code files found"
                        else None,
                    )
                    if plan_text:
                        logger.info(
                            "[%s] WORKFLOW   Planning complete, plan length=%d chars",
                            task_id,
                            len(plan_text),
                        )
                        plan_dir = repo_path.parent / "plan"
                        if not plan_dir.exists():
                            plan_dir = repo_path / "plan"
                        if plan_dir.exists() and plan_dir.is_dir():
                            try:
                                plan_file = plan_dir / f"frontend_task_{task_id}.md"
                                plan_file.write_text(
                                    f"# Frontend task plan: {task_id}\n\n{plan_text}",
                                    encoding="utf-8",
                                )
                                logger.info(
                                    "[%s] WORKFLOW   Persisted plan to %s", task_id, plan_file
                                )
                            except Exception as e:
                                logger.warning(
                                    "[%s] Failed to persist plan (non-blocking): %s", task_id, e
                                )

                result = self.run(
                    FrontendInput(
                        framework_target=_resolved_framework_for_implementation(
                            current_task.metadata, spec_content
                        ),
                        task_description=current_task.description,
                        requirements=_task_requirements_with_route_expectations(
                            current_task, repo_path
                        ),
                        user_story=getattr(current_task, "user_story", "") or "",
                        spec_content=_truncate_for_context(
                            spec_content, compute_spec_content_chars(self.llm)
                        ),
                        architecture=architecture,
                        existing_code=existing_code
                        if existing_code != "# No code files found"
                        else None,
                        api_endpoints=api_endpoints
                        if api_endpoints != "# No code files found"
                        else None,
                        qa_issues=qa_issues,
                        security_issues=sec_issues,
                        accessibility_issues=a11y_issues,
                        code_review_issues=code_review_issues,
                        suggested_tests_from_qa=suggested_tests_from_qa,
                        task_plan=plan_text if plan_text else None,
                        convergence_hint=convergence_hint,
                    )
                )

                if result.needs_clarification and result.clarification_requests:
                    if iteration_round < MAX_CLARIFICATION_REFINEMENTS:
                        current_task = tech_lead.refine_task(
                            current_task,
                            result.clarification_requests,
                            spec_content,
                            architecture,
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
                            task_id,
                            result.npm_packages_to_install,
                            install_res.stderr[:500],
                        )

                # ─── Lint verification (before build) ──────────────────────
                if linting_tool_agent is not None:
                    try:
                        from linting_tool_agent.models import LintToolInput as _LintInput

                        lint_result = linting_tool_agent.run(
                            _LintInput(
                                repo_path=str(repo_path),
                                agent_type="frontend",
                                task_id=task_id,
                                task_description=current_task.description,
                            )
                        )
                        if not lint_result.execution_result.success:
                            logger.info(
                                "[%s] WORKFLOW   [%d] Lint found %d issue(s), %d edit(s)",
                                task_id,
                                iteration_round,
                                lint_result.execution_result.issue_count,
                                len(lint_result.edits),
                            )
                            if lint_result.edits:
                                from software_engineering_team.shared.repo_writer import (
                                    write_agent_output as _write_lint,
                                )

                                lint_files: Dict[str, str] = {}
                                repo_root = repo_path.resolve()
                                for e in lint_result.edits:
                                    file_abs = (repo_path / e.file_path).resolve()
                                    try:
                                        rel_path = str(file_abs.relative_to(repo_root))
                                    except ValueError:
                                        continue
                                    if not file_abs.is_file():
                                        continue
                                    current_content = lint_files.get(rel_path)
                                    if current_content is None:
                                        current_content = file_abs.read_text(
                                            encoding="utf-8", errors="replace"
                                        )
                                    if e.old_text not in current_content:
                                        continue
                                    lint_files[rel_path] = current_content.replace(
                                        e.old_text, e.new_text, 1
                                    )
                                if lint_files:
                                    _write_lint(
                                        repo_path,
                                        type(
                                            "_LR",
                                            (),
                                            {"files": lint_files, "summary": lint_result.summary},
                                        )(),
                                        subdir="",
                                    )
                            elif lint_result.linter_issues:
                                code_review_issues = [
                                    {
                                        "severity": li.severity,
                                        "description": f"[{li.rule}] {li.message}",
                                        "file_path": li.file_path,
                                        "suggestion": f"Fix lint violation {li.rule} at line {li.line}",
                                    }
                                    for li in lint_result.linter_issues[:20]
                                ]
                                continue
                    except Exception as lint_err:
                        logger.warning(
                            "[%s] WORKFLOW   Lint step failed (non-blocking): %s",
                            task_id,
                            lint_err,
                        )

                build_ok, build_errors = build_verifier(repo_path, "frontend", task_id)
                if not build_ok:
                    if build_errors.startswith("ENV:"):
                        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                        return FrontendWorkflowResult(
                            task_id=task_id,
                            success=False,
                            failure_reason="Unsupported environment: "
                            + build_errors[4:].strip()[:500],
                        )
                    build_error_sig = (build_errors[:800] or build_errors).strip()
                    if build_error_sig == last_build_error_sig:
                        consecutive_same_build_failures += 1
                    else:
                        last_build_error_sig = build_error_sig
                        consecutive_same_build_failures = 1
                    if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                        return FrontendWorkflowResult(
                            task_id=task_id,
                            success=False,
                            failure_reason=(
                                f"Build failed {MAX_SAME_BUILD_FAILURES} times with the same error. "
                                f"Last error: {build_errors[:500]}"
                            ),
                        )

                    # Try BuildFixSpecialist for minimal targeted fix before QA fallback
                    if consecutive_same_build_failures >= 2 and build_fix_specialist is not None:
                        try:
                            from build_fix_specialist.models import BuildFixInput

                            affected_paths = (
                                _extract_affected_file_paths_from_frontend_build_errors(
                                    build_errors,
                                    repo_path,
                                )
                            )
                            affected_code = _read_frontend_affected_files_code(
                                repo_path, affected_paths
                            )
                            bf_result = build_fix_specialist.run(
                                BuildFixInput(
                                    build_errors=build_errors[:4000],
                                    affected_files_code=affected_code,
                                    task_description=current_task.description,
                                )
                            )
                            if bf_result.edits:
                                ok_apply, msg_apply, files_dict = _apply_frontend_build_fix_edits(
                                    repo_path,
                                    bf_result.edits,
                                )
                                if ok_apply and files_dict:
                                    from software_engineering_team.shared.repo_writer import (
                                        write_agent_output as _write_bf,
                                    )

                                    ok_write, _ = _write_bf(
                                        repo_path,
                                        type(
                                            "_BF",
                                            (),
                                            {"files": files_dict, "summary": bf_result.summary},
                                        )(),
                                        subdir="",
                                    )
                                    if ok_write:
                                        logger.info(
                                            "[%s] WORKFLOW   BuildFixSpecialist applied %d edit(s), re-running build",
                                            task_id,
                                            len(files_dict),
                                        )
                                        continue
                        except Exception as bf_err:
                            logger.warning(
                                "[%s] WORKFLOW   BuildFixSpecialist failed (non-blocking): %s",
                                task_id,
                                bf_err,
                            )

                    # Invoke testing sub-agent to analyze build errors and produce fix recommendations
                    code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                    from qa_agent.models import QAInput as QAI

                    qa_fix_result = qa_agent.run(
                        QAI(
                            code=code_on_branch,
                            language="typescript",
                            task_description=current_task.description,
                            architecture=architecture,
                            build_errors=build_errors[:4000],
                            request_mode="fix_build",
                        )
                    )
                    qa_issues = [
                        b.model_dump() if hasattr(b, "model_dump") else b.dict()
                        for b in (qa_fix_result.bugs_found or [])
                    ]
                    if not qa_issues:
                        # Fallback to generic code_review_issues if QA returns nothing
                        qa_issues = [
                            {
                                "severity": "critical",
                                "description": f"Build failed: {build_errors[:2000]}",
                                "recommendation": "Fix the compilation errors",
                            }
                        ]
                    if consecutive_same_build_failures >= 2:
                        qa_issues.insert(
                            0,
                            {
                                "severity": "critical",
                                "description": (
                                    f"ESCALATION: This build error has occurred {consecutive_same_build_failures} times. "
                                    "Focus ONLY on fixing this specific error. Make minimal, targeted changes."
                                ),
                                "recommendation": "Apply the minimal fix indicated by the error message.",
                            },
                        )
                    code_review_issues = []
                    continue

                consecutive_same_build_failures = 0
                last_build_error_sig = ""

                # After first successful build: have testing sub-agent write unit and integration tests
                if not write_tests_requested:
                    write_tests_requested = True
                    code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                    from qa_agent.models import QAInput as QAI

                    qa_tests_result = qa_agent.run(
                        QAI(
                            code=code_on_branch,
                            language="typescript",
                            task_description=current_task.description,
                            architecture=architecture,
                            request_mode="write_tests",
                        )
                    )
                    tests_dict = {}
                    if qa_tests_result.unit_tests:
                        tests_dict["unit_tests"] = qa_tests_result.unit_tests
                    if qa_tests_result.integration_tests:
                        tests_dict["integration_tests"] = qa_tests_result.integration_tests
                    if tests_dict:
                        suggested_tests_from_qa = tests_dict
                        continue

                suggested_tests_from_qa = (
                    None  # Clear after use so we don't re-pass on code review loop
                )
                code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                from software_engineering_team.shared.context_sizing import (
                    compute_code_review_total_chars,
                    compute_existing_code_chars,
                )

                max_code = compute_existing_code_chars(self.llm)
                max_review = compute_code_review_total_chars(self.llm)
                existing_code_ctx = _truncate_for_context(code_on_branch, max_code)
                code_for_review = _truncate_for_context(code_on_branch, max_review)
                review_result = self._run_code_review(
                    code_review_agent=code_review_agent,
                    code=code_for_review,
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
                    cr_count = len(code_review_issues)
                    if last_code_review_count > 0 and cr_count >= last_code_review_count:
                        rounds_without_decrease += 1
                    else:
                        rounds_without_decrease = 0
                        convergence_hint = None
                    last_code_review_count = cr_count
                    if rounds_without_decrease >= 3:
                        convergence_hint = (
                            "Code review issue count has not decreased; make minimal, targeted fixes "
                            "and avoid refactoring unrelated code."
                        )
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

                        av_result = acceptance_verifier_agent.run(
                            AcceptanceVerifierInput(
                                code=code_for_verify,
                                task_description=current_task.description,
                                acceptance_criteria=current_task.acceptance_criteria,
                                spec_content=spec_content,
                                architecture=architecture,
                                language="typescript",
                            )
                        )
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

                qa_result = qa_agent.run(
                    QAInput(
                        code=code_to_review,
                        language="typescript",
                        task_description=current_task.description,
                        architecture=architecture,
                    )
                )
                from accessibility_agent.models import AccessibilityInput

                a11y_result = accessibility_agent.run(
                    AccessibilityInput(
                        code=code_to_review,
                        language="typescript",
                        task_description=current_task.description,
                        architecture=architecture,
                    )
                )
                from security_agent.models import SecurityInput

                sec_result = security_agent.run(
                    SecurityInput(
                        code=code_to_review,
                        language="typescript",
                        task_description=current_task.description,
                        architecture=architecture,
                    )
                )

                qa_issues = [
                    b.model_dump() if hasattr(b, "model_dump") else b.dict()
                    for b in (qa_result.bugs_found or [])
                ]
                a11y_issues = [
                    i.model_dump() if hasattr(i, "model_dump") else i.dict()
                    for i in (a11y_result.issues or [])
                ]
                sec_issues = [
                    v.model_dump() if hasattr(v, "model_dump") else v.dict()
                    for v in (sec_result.vulnerabilities or [])
                ]

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
                        current_task,
                        qa_result,
                        spec_content,
                        architecture,
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
                            compute_existing_code_chars(self.llm),
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
        except LLMUnreachableAfterRetriesError:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=LLM_UNREACHABLE_AFTER_RETRIES,
                llm_unreachable=True,
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

        return code_review_agent.run(
            CodeReviewInput(
                code=code,
                spec_content=spec_content,
                task_description=task.description,
                task_requirements=_task_requirements(task),
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language="typescript",
                architecture=architecture,
                existing_codebase=existing_code,
            )
        )

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
        from technical_writers.dbc_comments_agent.models import DbcCommentsInput

        from software_engineering_team.shared.git_utils import write_files_and_commit

        try:
            dbc_code = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
            if not dbc_code or dbc_code == "# No code files found":
                return
            dbc_result = dbc_agent.run(
                DbcCommentsInput(
                    code=dbc_code,
                    language="typescript",
                    task_description=task_description,
                    architecture=architecture,
                )
            )
            if not dbc_result.already_compliant and dbc_result.files:
                write_files_and_commit(
                    repo_path,
                    dbc_result.files,
                    dbc_result.suggested_commit_message,
                )
        except Exception as e:
            logger.warning("[%s] DBC review failed (non-blocking): %s", task_id, e)
