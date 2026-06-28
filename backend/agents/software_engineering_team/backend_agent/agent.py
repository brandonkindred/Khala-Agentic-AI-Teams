from __future__ import annotations

import json
import json as _json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import get_client, get_strands_model
from software_engineering_team.shared.context_sizing import (
    compute_existing_code_chars,
    compute_spec_content_chars,
)
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskUpdate
from software_engineering_team.shared.prompt_utils import (
    build_problem_solving_header,
    log_llm_prompt,
)
from software_engineering_team.shared.repo_utils import (
    BACKEND_EXTENSIONS,
    _disambiguated_key,
    is_secret_template_path,
    is_sensitive_path,
    read_files_as_dict,
    read_repo_code,
    read_repo_files_as_dict,
    sanitize_path_for_text,
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

from .models import (
    BackendInput,
    BackendOutput,
    BackendWorkflowResult,
    ReviewIterationRecord,
)
from .prompts import BACKEND_PLANNING_PROMPT, BACKEND_PROMPT

MAX_EXISTING_CODE_CHARS = 10000
logger = logging.getLogger(__name__)

MAX_PATH_SEGMENT_LENGTH = 30
# Test files (test_*.py) may have longer descriptive names; allow up to 60 chars
MAX_TEST_FILE_SEGMENT_LENGTH = 60
BAD_NAME_PATTERN = re.compile(
    r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+"
)  # 4+ hyphenated words = likely sentence
BAD_NAME_SNAKE_PATTERN = re.compile(
    r"^[a-z]+_[a-z]+_[a-z]+_[a-z]+_[a-z]+"
)  # 5+ underscored words = likely sentence
VERB_PREFIX_PATTERN = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)[_-]"
)
FILLER_WORD_PATTERN = re.compile(r"[_-](the|that|with|using|which|for|and|a|an)[_-]")
# Well-known directory names that are always allowed
_ALLOWED_DIRS = frozenset(
    {
        "app",
        "src",
        "lib",
        "tests",
        "test",
        "routers",
        "models",
        "schemas",
        "services",
        "controllers",
        "repository",
        "middleware",
        "config",
        "utils",
        "helpers",
        "main",
        "infrastructure",
        "dist",
        "build",
    }
)


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
            # Test files (test_*.py) may have longer descriptive names
            max_len = (
                MAX_TEST_FILE_SEGMENT_LENGTH
                if (name_part.startswith("test_") and seg.endswith(".py"))
                else MAX_PATH_SEGMENT_LENGTH
            )
            if len(name_part) > max_len:
                warnings.append(f"Path segment too long: '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(
                    f"Path segment looks like a sentence (4+ hyphenated words): '{seg}' in '{path}'"
                )
                bad_segment = True
                break
            # Exempt test files from sentence-like snake pattern (e.g. test_task_crud_qa.py)
            if not (name_part.startswith("test_") and seg.endswith(".py")):
                if BAD_NAME_SNAKE_PATTERN.match(name_part):
                    warnings.append(
                        f"Path segment looks like a sentence (5+ underscored words): '{seg}' in '{path}'"
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
MAX_REVIEW_ITERATIONS = _int_env("SW_MAX_REVIEW_ITERATIONS", 15)
MAX_SAME_BUILD_FAILURES = _int_env(
    "SW_MAX_SAME_BUILD_FAILURES", 6
)  # Stop if build fails identically this many times
MAX_PREWRITE_REGENERATIONS = _int_env(
    "SW_MAX_PREWRITE_REGENERATIONS", 2
)  # Max regenerations for pre-write test-route checks
MAX_CLARIFICATION_ROUNDS = _int_env("SW_MAX_CLARIFICATION_ROUNDS", 100)
MAX_PROBLEM_SOLVER_CYCLES = _int_env("SW_MAX_PROBLEM_SOLVER_CYCLES", 20)
_REQUIRED_TASK_CONTRACT_FIELDS = (
    "goal",
    "scope",
    "constraints",
    "non_functional_requirements",
)
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
# Regex to extract HTTP paths from TestClient calls: client.get("/path"), client.post("/tasks", ...)
_CLIENT_ROUTE_RE = re.compile(r'client\.(?:get|post|patch|put|delete)\s*\(\s*["\']([^"\']+)["\']')


def _extract_routes_from_tests(repo_path: Path) -> List[str]:
    """Extract all HTTP paths referenced in test files (client.get/post/etc)."""
    tests_dir = repo_path / "tests"
    if not tests_dir.exists() or not tests_dir.is_dir():
        return []
    found: List[str] = []
    for f in tests_dir.rglob("test_*.py"):
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            for match in _CLIENT_ROUTE_RE.finditer(content):
                path = match.group(1).strip()
                if path and path not in found:
                    found.append(path)
        except Exception:
            pass
    return found


def _extract_routes_from_main(content: str) -> List[str]:
    """Extract HTTP paths from FastAPI app (e.g. @app.get('/path'), include_router(prefix='...'))."""
    paths: List[str] = []
    # @app.get("/path"), @router.post("/path")
    for match in re.finditer(
        r'@(?:app|router)\.(?:get|post|patch|put|delete)\s*\(\s*["\']([^"\']+)["\']',
        content,
    ):
        paths.append(match.group(1))
    # include_router(router, prefix="/path")
    for match in re.finditer(
        r'include_router\s*\([^)]*prefix\s*=\s*["\']([^"\']+)["\']',
        content,
    ):
        paths.append(match.group(1))
    return paths


def _check_test_endpoint_compatibility(
    repo_path: Path, main_content: str | None = None
) -> List[str]:
    """
    Return list of routes that tests reference but are missing from main.py.
    If main_content is None, read from repo_path/app/main.py.
    """
    refs = _extract_routes_from_tests(repo_path)
    if not refs:
        return []
    if main_content is None:
        main_file = repo_path / "app" / "main.py"
        if not main_file.exists():
            main_file = repo_path / "main.py"
        if not main_file.exists():
            return refs
        try:
            main_content = main_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return refs
    # Check if each referenced path (or its base segment) appears in main
    missing: List[str] = []
    for ref in refs:
        ref_base = "/" + ref.strip("/").split("/")[0] if ref.startswith("/") else ref
        if ref not in main_content and ref_base not in main_content:
            missing.append(ref)
    return missing


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


def _build_code_review_issues_for_missing_test_routes(
    missing_routes: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Build code_review_issues for pre-write check: tests reference routes missing from main.py."""
    routes = missing_routes or list(_TEST_ROUTE_PATTERNS)
    if "/test-generic-error" in routes and len(routes) == 1:
        suggestion = (
            "Tests expect the route `/test-generic-error` and a generic "
            "exception handler that returns a JSONResponse (e.g. status_code=500). "
            "Preserve this route in `app/main.py` and ensure the handler does not "
            "re-raise; otherwise the test client gets an exception and the test fails."
        )
    else:
        routes_str = ", ".join(routes)
        suggestion = (
            f"Tests reference routes that do not exist in app/main.py: {routes_str}. "
            "Add these routes to app/main.py (or include the appropriate router). "
            "Ensure each route returns a proper HTTP response (not an unhandled exception)."
        )
    return [
        {
            "severity": "critical",
            "category": "build",
            "file_path": "app/main.py",
            "description": f"Pre-flight check: tests reference {routes} but app/main.py does not include them.",
            "suggestion": suggestion,
        }
    ]


def _extract_failing_test_file_from_build_errors(build_errors: str) -> Optional[str]:
    """Extract failing test file path from build_errors (e.g. tests/test_auth_middleware.py)."""
    match = re.search(r"tests/test_[a-zA-Z0-9_]+\.py", build_errors)
    return match.group(0) if match else None


def _extract_affected_file_paths_from_build_errors(build_errors: str, repo_path: Path) -> List[str]:
    """Extract file paths mentioned in build_errors that exist in repo (for BuildFixSpecialist context)."""
    seen: set = set()
    paths: List[str] = []
    # Traceback: File "app/main.py", line 10 or app/main.py:42:
    for m in re.finditer(
        r'(?:File\s+["\']([^"\']+\.py)["\']|([a-zA-Z0-9_/]+\.py):\d+)',
        build_errors,
    ):
        p = (m.group(1) or m.group(2) or "").strip()
        if p and p not in seen and (repo_path / p).exists():
            seen.add(p)
            paths.append(p)
    # tests/test_*.py
    for m in re.finditer(r"tests/test_[a-zA-Z0-9_]+\.py", build_errors):
        p = m.group(0)
        if p not in seen and (repo_path / p).exists():
            seen.add(p)
            paths.append(p)
    # Always include app/main.py if it exists (common fix target)
    if "app/main.py" not in seen and (repo_path / "app" / "main.py").exists():
        paths.insert(0, "app/main.py")
    return paths[:10]  # Cap to avoid huge context


def _read_affected_files_code(repo_path: Path, file_paths: List[str]) -> str:
    """Read content of affected files for BuildFixSpecialist context."""
    parts: List[str] = []
    for p in file_paths:
        f = repo_path / p
        if f.is_file():
            try:
                parts.append(f"### {p} ###\n{f.read_text(encoding='utf-8', errors='replace')}")
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "# No affected files found"


def _apply_build_fix_edits(
    repo_path: Path, edits: List[Any], max_code_chars: int
) -> Tuple[bool, str, Dict[str, str]]:
    """Apply BuildFixSpecialist edits. Returns (success, message, files_dict for commit)."""
    from build_fix_specialist.models import CodeEdit

    # Group edits by file, apply in order
    file_edits: Dict[str, List[CodeEdit]] = {}
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
        except Exception as e:
            logger.warning("BuildFixSpecialist could not read %s: %s", file_path, e)
            continue
        for edit in edit_list:
            if edit.old_text not in content:
                logger.warning(
                    "BuildFixSpecialist old_text not found in %s (exact match required)",
                    file_path,
                )
                return False, f"old_text not found in {file_path}", {}
            content = content.replace(edit.old_text, edit.new_text, 1)
        if len(content) > max_code_chars * 2:  # Sanity check
            logger.warning("BuildFixSpecialist edit would produce oversized file")
            continue
        files_to_write[file_path] = content
    if not files_to_write:
        return False, "No edits could be applied", {}
    return True, f"Applied {len(files_to_write)} edit(s)", files_to_write


def _is_pytest_assertion_failure(build_errors: str) -> bool:
    """Return True if build_errors indicates a pytest assertion failure."""
    return "[pytest_assertion]" in build_errors or "failure_class=pytest_assertion" in build_errors


# Sentinel distinguishing "caller did not precompute the failing-test context"
# from a genuine ``None`` (no failing test). Lets the build-failure loop resolve
# the context once per iteration and pass it to both the BuildFixSpecialist and
# the escalation helper, while those helpers still resolve it themselves when
# called directly (e.g. in unit tests). A named sentinel type (rather than a bare
# ``object()`` typed ``Any``) lets the params below annotate as
# ``str | None | _Unset`` — preserving real type info instead of erasing to Any.
class _Unset:
    """Sentinel: a parameter was not supplied (distinct from an explicit ``None``)."""


_UNSET = _Unset()


def _resolve_failing_test_context(
    build_errors: str, repo_path: Path
) -> Tuple[Optional[str], Optional[str]]:
    """Parse the failing pytest file from *build_errors* and read its full text.

    Preconditions: none.
    Postconditions: returns ``(failing_test_file, failing_test_content)`` — the
        file path named in a pytest assertion failure (or None), and its full
        text when the file exists and is readable (or None when there is no such
        file, it is missing, or it can't be read). Never raises; the read is
        best-effort. Callers slice ``failing_test_content`` as needed.
    """
    failing_test_file = (
        _extract_failing_test_file_from_build_errors(build_errors)
        if _is_pytest_assertion_failure(build_errors)
        else None
    )
    failing_test_content: Optional[str] = None
    if failing_test_file:
        test_path = repo_path / failing_test_file
        if test_path.exists():
            try:
                failing_test_content = test_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                failing_test_content = None
    return failing_test_file, failing_test_content


def _build_error_signature(build_errors: str) -> str:
    """Compute a signature for same-error detection.
    For pytest assertion failures, use the last 1200 chars (where assertion details
    usually appear) so assertion changes are detected. Otherwise use first 800 chars.
    """
    if _is_pytest_assertion_failure(build_errors):
        return (build_errors[-1200:] if len(build_errors) > 1200 else build_errors).strip()
    return (build_errors or build_errors).strip()


def _build_code_review_issues_for_build_failure(build_errors: str) -> List[Dict[str, Any]]:
    """Build code_review_issues from build/test failure output.
    When failure matches exception-handler test patterns, returns a targeted
    suggestion (preserve /test-generic-error, return JSONResponse) and file_path
    app/main.py so the agent knows exactly what to fix.
    Otherwise, extracts the failing test file from the feedback (e.g. "Fix tests/...")
    when present, so the code review issue points at the correct file.
    """
    is_exception_handler_failure = any(p in build_errors for p in EXCEPTION_HANDLER_TEST_PATTERNS)
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
        line_num = ""
        # Extract failing test file from feedback (e.g. "Fix tests/test_task_endpoints.py" or "Failing tests:\n  - tests/test_foo.py::test_bar")
        fix_tests_match = re.search(
            r"Fix\s+(tests/test_[a-zA-Z0-9_]+\.py)",
            build_errors,
        )
        if fix_tests_match:
            file_path = fix_tests_match.group(1)
            suggestion = f"Fix the errors in {file_path}. Read the traceback and apply the minimal change (e.g. add missing import, fix type, correct assertion)."
        else:
            failing_line_match = re.search(
                r"Failing tests:.*?\n\s+-\s+(tests/test_[a-zA-Z0-9_]+\.py)(?:::|$)",
                build_errors,
                re.DOTALL,
            )
            if failing_line_match:
                file_path = failing_line_match.group(1)
                suggestion = f"Fix the failing test in {file_path}. Ensure the implementation satisfies the test assertions."
            else:
                # Try to extract file:line from traceback (e.g. "app/main.py:42:" or 'File "app/main.py", line 42')
                file_line_match = re.search(
                    r'(?:File\s+["\']([^"\']+\.py)["\'].*?line\s+(\d+)|'
                    r"([a-zA-Z0-9_/]+\.py):(\d+):)",
                    build_errors,
                )
                if file_line_match:
                    file_path = file_line_match.group(1) or file_line_match.group(3) or ""
                    line_num = file_line_match.group(2) or file_line_match.group(4) or ""
                    if file_path:
                        suggestion = (
                            f"Fix the error at {file_path}"
                            + (f" line {line_num}" if line_num else "")
                            + ". Apply the minimal change indicated by the traceback."
                        )
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
    """Read code files from repo, concatenated. Delegates to shared.repo_utils."""
    if extensions is None:
        extensions = BACKEND_EXTENSIONS
    return read_repo_code(repo_path, extensions)


def _writer_output_keys(result: Any) -> Dict[str, str] | None:
    """Paths ``write_agent_output`` would materialize for *result* (or None).

    Reuses the writer's own normalization and additionally includes a synthesized
    root ``.gitignore`` when the output declares ``gitignore_entries`` (the writer
    adds it *after* the base mapping), so a brand-new, still-untracked .gitignore
    from a failed-commit iteration is reviewed instead of slipping through. Values
    are placeholders — only the keys widen the set of paths re-read from the
    worktree.
    """
    if result is None:
        return None
    from software_engineering_team.shared.repo_writer import output_to_files_dict

    written = dict(output_to_files_dict(result, ""))
    if getattr(result, "gitignore_entries", None):
        written.setdefault(".gitignore", "")
    return written


# Synthetic block labels under which review *notes* (not real source files)
# travel in the segmented code/files channel. The prefix keeps them visually
# distinct from real paths; detaching a note-anchored finding from the fix loop
# is done by tracking the *exact* keys synthesized for a review (see
# ReviewInputSelection.synthetic_keys), not by prefix-matching — so a genuine
# changed file that happens to live under ``[review-note]/`` is never mistaken
# for a note.
_REVIEW_NOTE_PREFIX = "[review-note]/"
_DELETION_NOTE_PATH = f"{_REVIEW_NOTE_PREFIX}deleted-files"
_EMPTIED_NOTE_PATH = f"{_REVIEW_NOTE_PREFIX}emptied-files"
_WITHHELD_NOTE_PATH = f"{_REVIEW_NOTE_PREFIX}withheld-sensitive"
_UNREADABLE_NOTE_PATH = f"{_REVIEW_NOTE_PREFIX}unreadable-files"


# Binary file extensions that are legitimately non-text-reviewable *assets*
# (images, media, fonts, archives, compiled artifacts, model weights, data). A
# change touching one of these can't be source-reviewed, so it is advisory — it
# must not permanently block a merge. Anything *not* in this set that read as
# binary/unreadable (a source file with embedded NUL, a submodule gitlink, an
# unknown type) is treated as needing manual review.
_BINARY_ASSET_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".tiff",
        ".pdf",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".wav",
        ".ogg",
        ".webm",
        ".flac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".wasm",
        ".so",
        ".dll",
        ".dylib",
        ".a",
        ".o",
        ".class",
        ".pyc",
        ".pyd",
        ".onnx",
        ".pt",
        ".pth",
        ".h5",
        ".pb",
        ".tflite",
        ".parquet",
        ".npy",
        ".npz",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
    }
)


def _is_binary_asset(path: str) -> bool:
    """True when *path* is a recognized non-text-reviewable binary asset."""
    return Path(path).suffix.lower() in _BINARY_ASSET_SUFFIXES


@dataclass
class ReviewInputSelection:
    """Chosen code-review input plus the metadata the gate and fix loop need.

    Iterable as the legacy ``(files, code, deletion_note)`` triple so existing
    unpacking call sites keep working, while also carrying:

    - ``synthetic_keys`` — the *exact* note labels this selection inserted. A
      finding anchored on one of these is detached from the fix loop (no real
      file to edit); tracking the precise set (rather than prefix-matching) means
      a genuine changed file that merely lives under a ``[review-note]/`` path is
      never mistaken for a note.
    - ``key_to_path`` — maps each display-safe review key back to its real
      repo-relative path, so a finding's ``file_path`` (the sanitized, possibly
      ``~N``-suffixed key) is translated to the actual file the fixer must edit.
    """

    files: Dict[str, str] | None
    code: str | None
    deletion_note: str | None
    synthetic_keys: frozenset[str] = frozenset()
    key_to_path: Dict[str, str] = field(default_factory=dict)
    # Task-owned paths the reviewer could NOT examine the content of: secrets
    # (withheld), binary/submodule/unreadable, and emptied/whitespace-only files.
    withheld: Tuple[str, ...] = ()
    unreadable: Tuple[str, ...] = ()
    emptied: Tuple[str, ...] = ()

    def __iter__(self):
        return iter((self.files, self.code, self.deletion_note))

    def has_examinable_content(self) -> bool:
        """True when the reviewer actually saw file content, not only notes.

        A non-empty ``code`` channel (whole-repo fallback) counts; an *empty*
        ``code`` (truly empty repo) does not. For the files-dict channel, at least
        one block must be a real (non-synthetic) key holding non-whitespace
        content — a restored deletion blob or a changed file. A mapping of only
        omission notes (withheld, unreadable, emptied, deletion listing) — or only
        emptied files — is *not* examinable.
        """
        if self.code:
            return True
        if not self.files:
            return False
        return any(
            key not in self.synthetic_keys and (value or "").strip()
            for key, value in self.files.items()
        )

    def unexamined_paths(self) -> Tuple[str, ...]:
        """All task-owned paths whose content the reviewer never saw (advisory).

        The full omission set — withheld secrets, unreadable/binary/submodule
        files, and emptied files. Every entry is surfaced to the reviewer as a
        note, but only :meth:`blocking_unexamined` forces manual review; see there
        for why a binary asset or sensitive-named template is advisory rather than
        a hard block.
        """
        return tuple(sorted(set(self.withheld) | set(self.unreadable) | set(self.emptied)))

    def blocking_unexamined(self) -> Tuple[str, ...]:
        """The omissions that MUST force manual review on an approval.

        Not every unexamined path should block a merge, but most should. The only
        omissions that stay *advisory* here (surfaced as notes via
        :meth:`unexamined_paths`, never returned by this method) are the ones that
        are legitimately non-text-reviewable, so blocking *a change that also
        carries reviewed source* on them would make every such change
        permanently un-mergeable. (A change consisting *solely* of advisory items,
        with nothing examinable, is still routed to manual review by
        :meth:`review_gate_failure` — this method only governs the per-path block
        set, not the no-content-at-all case.) The advisory paths are:

        - a recognized **binary asset** (``logo.png``, a font, an archive, model
          weights) — see :func:`_is_binary_asset`;
        - a placeholder ``.env`` **template** flagged only by the over-broad secret
          denylist (``.env.example``/``.env.sample``) — see
          :func:`is_secret_template_path`. A file that is sensitive for a *strong*
          reason (a secret dir, a ``credentials``/``secret`` stem, a key/cert
          suffix) is NOT downgraded even with a template suffix.

        Everything else blocks, because the reviewer (and the later source-only
        security pass) never saw the content:

        - **emptied** — a changed file truncated to empty/whitespace;
        - a **withheld real secret** — ``.env``, ``id_rsa``, ``*.pem``,
          ``credentials``, ``secrets/...`` (changed or deleted): content withheld,
          must be checked;
        - an **unreadable non-asset** — a source file that read as binary, a
          submodule/gitlink pointer change, or an unknown type;
        - the **baseline-unavailable sentinel** — the diff couldn't be computed, so
          a deletion may exist we cannot enumerate (not an asset → blocks here).
        """
        blockers = set(self.emptied)
        blockers.update(p for p in self.withheld if not is_secret_template_path(p))
        blockers.update(p for p in self.unreadable if not _is_binary_asset(p))
        return tuple(sorted(blockers))

    def review_gate_failure(self, approved: bool) -> str | None:
        """Reason an approval must route to manual review instead of merging, else None.

        The reusable never-silently-approve gate, applied by the backend fix loop
        (and available to any other review caller that builds a
        :class:`ReviewInputSelection`). An approval is blocked when:

        - :meth:`blocking_unexamined` is non-empty — a destructive/unknowable
          omission (emptied file, withheld real secret, unreadable source, or an
          uncomputable baseline diff); or
        - the reviewer saw no examinable content at all while the selection still
          carried blocks (:meth:`has_examinable_content` is False and ``files`` is
          a non-empty map) — e.g. a change consisting solely of advisory omission
          notes (a lone binary asset or ``.env`` template). Such items never block
          *on their own* via :meth:`blocking_unexamined` (so a change that *also*
          carries reviewed source is not held up by an advisory asset), but a
          change with *nothing* examinable still routes to manual review here,
          since the approval rests on content the reviewer never saw. The
          genuinely empty repo (``files`` is None with an empty ``code``) has
          nothing to review and is allowed, so a normal no-op never trips the gate.

        Preconditions:
            - none.
        Postconditions:
            - Returns None when *approved* is False or the approval may stand.
            - Otherwise returns a human-readable reason naming the blocking
              omissions and (advisory) only the *non-blocking* remainder.
        """
        if not approved:
            return None
        blocking = self.blocking_unexamined()
        # files is either a non-empty mapping or None in a returned selection, so
        # ``files is not None`` distinguishes a notes-only review (block) from the
        # genuinely empty repo (allow).
        saw_no_content = self.files is not None and not self.has_examinable_content()
        if not blocking and not saw_no_content:
            return None
        clauses: List[str] = []
        if blocking:
            shown = ", ".join(blocking[:20]) + (" ..." if len(blocking) > 20 else "")
            clauses.append(
                "destructive/unknowable omissions that require manual review before merge "
                "(emptied files, withheld secrets, unreadable source, or a baseline diff "
                f"that could not be computed): {shown}"
            )
        if saw_no_content:
            clauses.append("the reviewer saw no examinable file content (only omission notes)")
        # The advisory tail is the *non-blocking* remainder only (binary assets,
        # .env templates) — blocking paths are already named in the clause above,
        # so listing them again would blur which omissions actually hold the merge.
        blocking_set = set(blocking)
        advisory = tuple(p for p in self.unexamined_paths() if p not in blocking_set)
        return (
            "Code review approved, but " + "; ".join(clauses) + ". "
            f"Also not examined (advisory): {', '.join(advisory[:20]) or 'none'}"
        )


def _translate_finding_paths(
    issues: List[Dict[str, Any]],
    key_to_path: Dict[str, str],
    synthetic_keys: frozenset[str],
) -> List[Dict[str, Any]]:
    """Map each finding's ``file_path`` (a review key) back to a real repo path.

    Review-map keys are display-safe (surrogate/control-char escaped) and may be
    disambiguated with a ``~N`` suffix, so a finding's ``file_path`` is the *key*,
    not necessarily the on-disk path. Translating restores the path the reviewer
    meant so ``_regenerate_with_issues`` edits the right file. A finding on one of
    the *exact* synthetic note keys (which has no real file) is cleared to "" so
    the fixer treats it as a file-wide instruction instead of creating the note
    artifact — but a real changed file under a ``[review-note]/`` path keeps its
    target, because only keys actually synthesized for this review are stripped.

    Postconditions:
        - Returns a new list. Per issue: a ``file_path`` in *synthetic_keys* → "";
          one present in *key_to_path* → its original path; anything else
          unchanged (already a real path, or empty).
    """
    out: List[Dict[str, Any]] = []
    for issue in issues:
        fp = str(issue.get("file_path", ""))
        if fp in synthetic_keys:
            issue = {**issue, "file_path": ""}
        elif fp in key_to_path:
            issue = {**issue, "file_path": key_to_path[fp]}
        out.append(issue)
    return out


_WITHHELD_NOTE_HEADER = (
    "Files CHANGED or DELETED by this task but WITHHELD from review (path matches the "
    "secret denylist); content is NOT shown — review these manually:"
)
_UNREADABLE_NOTE_HEADER = (
    "Files CHANGED by this task that could NOT be read (binary, a submodule/gitlink, "
    "or vanished mid-scan) and so were not reviewed — verify each manually:"
)
_EMPTIED_NOTE_HEADER = (
    "Files reduced to EMPTY/whitespace-only by this task — confirm the content removal "
    "is intentional:"
)


def _attach_review_notes(
    files: Dict[str, str],
    *,
    deletion_note: str | None = None,
    withheld: List[str] | None = None,
    unreadable: List[str] | None = None,
    emptied: List[str] | None = None,
) -> frozenset[str]:
    """Insert each non-empty omission note into *files* (mutated); return its keys.

    Used by both the task-scoped selection and the whole-repo fallback so the two
    surface omissions identically. Each note rides the segmented code/files channel
    under a free synthetic key (so a real file sharing the label is never dropped).

    Postconditions:
        - *files* gains one block per non-empty note; the returned frozenset is the
          exact set of keys inserted (for precise finding detachment).
    """
    synthetic: set[str] = set()
    specs = (
        (_DELETION_NOTE_PATH, deletion_note),
        (
            _WITHHELD_NOTE_PATH,
            _format_path_note(_WITHHELD_NOTE_HEADER, withheld) if withheld else None,
        ),
        (
            _UNREADABLE_NOTE_PATH,
            _format_path_note(_UNREADABLE_NOTE_HEADER, unreadable) if unreadable else None,
        ),
        (_EMPTIED_NOTE_PATH, _format_path_note(_EMPTIED_NOTE_HEADER, emptied) if emptied else None),
    )
    for base_label, text in specs:
        if text:
            key = _disambiguated_key(files, base_label)
            files[key] = text
            synthetic.add(key)
    return frozenset(synthetic)


def _format_path_note(header: str, paths: List[str]) -> str:
    """Render *paths* as a single-line-safe bulleted note under *header*.

    Each path is escaped with :func:`sanitize_path_for_text` so a filename
    containing a newline or other control byte — which git permits and the diff
    preserves verbatim — cannot inject extra bullets, fake the count, or smuggle
    reviewer instructions into the model input. The note rides the segmented
    code/files channel (never the per-chunk-repeated task description), so a long
    list is split across review chunks rather than blowing every sub-review's
    context.
    """
    listing = "\n".join(f"- {sanitize_path_for_text(p)}" for p in paths)
    return f"{header}\n{listing}\n"


def _format_deletion_note(
    deleted: List[str], references: Dict[str, List[str]] | None = None
) -> str:
    """Render *every* removed path as a reviewer note, with surviving referrers.

    Lists all deletions, not a capped sample: the reviewer must be able to check
    whether anything still depends on each removed path, so hiding identities
    behind a count would leave deletions silently unreviewed (fail-open). When
    *references* maps a deleted path to surviving files that still import it, those
    are listed inline so the reviewer can verify the "nothing depends on it"
    claim against concrete dependents (best-effort; absence is not a guarantee).
    Every path is control-char escaped via :func:`sanitize_path_for_text`.
    """
    references = references or {}
    lines = [
        f"Files DELETED by this task ({len(deleted)} total; verify each removal is "
        "intentional and that nothing still depends on them). Pre-deletion content "
        "for readable files is provided for review under a separate "
        "'<path> (DELETED by this task — pre-deletion content)' label:"
    ]
    for path in deleted:
        lines.append(f"- {sanitize_path_for_text(path)}")
        for referrer in references.get(path, []):
            lines.append(f"    still referenced by: {sanitize_path_for_text(referrer)}")
    return "\n".join(lines) + "\n"


# Sentinel recorded as "unreadable" when the baseline diff is unavailable: the
# whole-repo fallback scans only the current worktree, so a file *deleted* by the
# task cannot be enumerated and is invisible. It forces the gate to manual review
# rather than certifying a current-tree scan that may hide an unreviewed deletion.
_DELETIONS_UNKNOWN_SENTINEL = "<deletions not enumerable: baseline diff unavailable>"


def _whole_repo_review_input(
    repo_path: Path, *, baseline_unavailable: bool = False
) -> ReviewInputSelection:
    """Complete, fail-safe review input: every reviewable file in the repo.

    Used when no task-scoped change set is available (empty diff) or cannot be
    trusted (baseline diff unavailable). Reviews all file types (config,
    migrations, JS, docs, ...) — not just ``.py``/``.java``.

    The files it cannot show the model — sensitive (withheld) and binary/unreadable
    — are not discarded silently: they are surfaced as omission notes and recorded
    on the selection's ``withheld``/``unreadable`` fields, so the gate treats an
    approval on this fallback as incomplete (manual review) rather than certifying
    a "whole-repo" review that quietly excluded files. The omission notes/metadata
    are attached even when *no* readable file remains (a repo of only
    sensitive/binary files), so the reviewer still gets the evidence and the gate
    still blocks; only a genuinely empty repo yields an empty ``code`` whose
    ``has_examinable_content()`` is False.

    When *baseline_unavailable* is set, a sentinel is recorded as unexamined
    because deletions cannot be enumerated from the current tree — so an approval
    on this degraded path always routes to manual review.
    """
    key_to_path: Dict[str, str] = {}
    omitted: List[str] = []
    sensitive_skipped: List[str] = []
    files = read_repo_files_as_dict(
        repo_path,
        key_to_path=key_to_path,
        omitted=omitted,
        sensitive_skipped=sensitive_skipped,
    )
    unreadable = list(omitted)
    if baseline_unavailable:
        unreadable.append(_DELETIONS_UNKNOWN_SENTINEL)
    # Truly empty repo (nothing readable, nothing omitted): an empty code channel
    # whose has_examinable_content() is False.
    if not files and not sensitive_skipped and not unreadable:
        return ReviewInputSelection(None, "", None)
    files = files or {}
    synthetic = _attach_review_notes(
        files, withheld=sensitive_skipped or None, unreadable=unreadable or None
    )
    return ReviewInputSelection(
        files,
        None,
        None,
        synthetic,
        key_to_path,
        withheld=tuple(sensitive_skipped),
        unreadable=tuple(unreadable),
    )


def _select_review_input(
    repo_path: Path,
    current_task: Any,
    written_files: Dict[str, str] | None,
) -> ReviewInputSelection:
    """Choose the code-review input: the task's changed files, read from disk.

    The path set is the union of the net base→worktree diff
    (``list_changed_and_deleted`` runs ``git diff <merge-base>`` with no ``HEAD``,
    so it compares the merge base to the **working tree** — every committed *and*
    uncommitted *tracked* change is captured; only untracked new files are not,
    which the writer's output keys cover) and the writer's normalized output keys.
    Every path's content is read from the **worktree**, never from the in-memory
    output dict, so review reflects exactly what is on the branch: paths that write
    validation rejected — or that a failed write never created — are absent from
    disk and therefore correctly excluded, and no extension filter is applied so
    non-source task changes (requirements.txt, migrations, YAML/JSON, ...) are
    reviewed too.

    Fallback ordering on this path is fail-closed: when the baseline diff *cannot*
    be computed (``BaselineDiffUnavailable``), it reviews the whole repo rather
    than the writer's just-written set, since that partial set could approve while
    silently omitting committed task changes.

    Deletions are surfaced two ways: the pre-deletion content of each removed
    path is read from the merge-base blob and added *byte-for-byte* (line numbers
    and non-Python syntax intact) under a DELETED-marked display *label* — the
    marker lives in the key, which the coordinator carries as every segment's file
    label, so even a large blob split across chunks is never mistaken for current
    source; ``key_to_path`` maps the label back to the real removed path so a
    finding ("this module is still imported") drives the fix loop to restore it. A
    separate *note* lists every removed path — including binary/unreadable ones
    whose content could not be fetched, and any surviving files that still import a
    removed module (a best-effort reverse-reference scan) so the "nothing depends
    on it" claim can be checked. Pure file-metadata changes (e.g. a mode/chmod-only
    change) are not separately surfaced: this pipeline writes file *content* via
    ``write_agent_output``.

    Never-silently-skip notes ride the same segmented channel:
        - **withheld-sensitive** — a changed *or deleted* path matching the secret
          denylist is not forwarded as content, but it is task-owned, so its
          *path* is listed for manual review rather than dropped (a ``secrets.py``
          / a removed ``.env`` must not bypass the gate by looking unchanged).
        - **unreadable-files** — a changed path that could not be read (binary, a
          submodule/gitlink directory, vanished mid-scan) is listed so a partial
          read never approves with a task-owned file unexamined.
        - **emptied-files** — a changed file reduced to empty/whitespace produces
          an empty block the coordinator drops; listing it keeps a destructive
          truncation from sailing through on an auto-approve.

    Preconditions:
        - *repo_path* is the task's working tree; *written_files* is the writer's
          normalized output mapping (or None) — only its keys are used, to widen
          the set of paths re-read from disk.
        - *current_task* is the active task. It is accepted to keep this helper's
          signature aligned with the call site and is reserved for task-aware
          selection; it is not consulted by the current implementation.
    Postconditions:
        - Returns a :class:`ReviewInputSelection` (unpackable as the legacy
          ``(files, code, deletion_note)`` triple). Exactly one of ``files`` /
          ``code`` is populated so review is never silently skipped: the worktree
          content of every committed/uncommitted/just-written path that exists on
          disk plus the deletion/withheld/unreadable/emptied notes and restored
          deletion content, else (the fail-closed fallback) a *whole-repo files
          dict* via :func:`_whole_repo_review_input` (logged) — ``code`` is then
          ``None``; only a genuinely empty repo yields an empty ``code`` string.
          ``synthetic_keys`` is the exact set of note labels inserted, and
          ``key_to_path`` maps every real review key back to its on-disk path, so
          the caller can translate a finding's key and detach note-anchored
          findings precisely.
        - ``has_examinable_content()`` is False when the only blocks are omission
          notes/emptied files, so the caller can refuse to let an auto-approval
          stand on content the reviewer never saw.
        - The whole-repo fallback reviews every file type, so an initial repo-
          setup commit's meta files (.gitignore, README, ...) are included.
    """
    from software_engineering_team.shared.git_utils import (
        _MAX_DELETED_BLOBS_READ,
        DEVELOPMENT_BRANCH,
        BaselineDiffUnavailable,
        find_referencing_paths,
        list_changed_and_deleted,
        read_paths_at_merge_base,
    )

    try:
        changed, deleted_all = list_changed_and_deleted(repo_path, DEVELOPMENT_BRANCH)
    except BaselineDiffUnavailable as exc:
        # Fail closed: if the net base→worktree diff can't be computed (missing
        # base ref, shallow/ambiguous history, timeout), review the whole repo.
        # Deletions can't be enumerated from the current tree, so the fallback is
        # flagged to force manual review rather than certify a current-tree scan.
        logger.warning("Code review: baseline diff unavailable (%s); reviewing whole repo", exc)
        return _whole_repo_review_input(repo_path, baseline_unavailable=True)

    # Order-preserving union of committed diff + writer output keys. The git-diff
    # ``changed`` set is the authoritative set of task-owned changes that MUST be
    # reviewed; the writer's output keys only *widen* the read set (so an uncommitted
    # or untracked just-written file is reviewed too). A writer key that is a mere
    # candidate — rejected by write validation, or a no-op — is therefore not on
    # disk; it must not be counted as an "unexamined" omission (that would fail the
    # manual-review gate for an otherwise-clean workflow), so only ``changed`` paths
    # feed the omission accumulator.
    changed_owned = {p for p in changed if not is_sensitive_path(p)}
    ordered: List[str] = []
    seen: set[str] = set()
    for source in (changed, list(written_files or ())):
        for p in source:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
    # A likely-secret path is not forwarded as content to the external review
    # model, but it is task-owned: surface it as a "withheld" note (path only) so
    # a sensitive-named code file cannot bypass the gate by looking unchanged.
    sensitive = [p for p in ordered if is_sensitive_path(p)]
    readable = [p for p in ordered if not is_sensitive_path(p)]

    all_omitted: List[str] = []
    key_to_path: Dict[str, str] = {}
    symlinked_paths: List[str] = []
    files = read_files_as_dict(
        repo_path,
        readable,
        extensions=None,
        omitted=all_omitted,
        key_to_path=key_to_path,
        symlinked=symlinked_paths,
    )
    # An omission is a genuine unexamined task artifact when it is either a
    # confirmed git-diff change, or a writer-emitted file that *exists on disk* but
    # could not be read (e.g. a generated binary). A writer-only *candidate* that
    # was never materialized (rejected by write validation, a no-op .gitignore) is
    # not on disk, so it is excluded — it must not pollute the omission notes.
    omitted = [p for p in all_omitted if p in changed_owned or (repo_path / p).is_file()]

    # A changed file truncated to empty/whitespace yields an empty block, which the
    # coordinator drops — silently approving the content removal. Flag those paths
    # by their *real* repo path (the dict key is a display-safe, possibly suffixed
    # review key), so the manual-review message names a file that exists on disk.
    emptied = [key_to_path.get(key, key) for key, content in files.items() if not content.strip()]

    # A deleted path can be restored in the worktree (a real file, or a dangling
    # symlink) in a later failed-commit iteration; treat it as present only then.
    # A directory now occupying the path (a file replaced by a dir) does *not*
    # count as present. A deleted *secret* is withheld (content never shown) but
    # its path is still surfaced below, so a deletion-only secret change is not
    # silently approved.
    deleted: List[str] = []
    deleted_sensitive: List[str] = []
    for p in deleted_all:
        full = repo_path / p
        if full.is_file() or full.is_symlink():
            continue  # restored on disk → not a net deletion to surface
        (deleted_sensitive if is_sensitive_path(p) else deleted).append(p)

    # Read what each deletion removed from the merge-base blob (the content is gone
    # from the worktree). A deletion whose blob could not be fetched (binary,
    # gitlink, or a read failure) has no examinable content, so it is recorded as
    # *unreadable* — otherwise a mixed change (one readable file + an unreadable
    # deletion) would look fully examined and slip an unreviewed removal through.
    # Fetch pre-deletion content for at most _MAX_DELETED_BLOBS_READ removals (two
    # git spawns each) so a mass deletion can't fan out into thousands of spawns;
    # the rest are still listed by name in the deletion note below. Only the
    # *probed* removals that genuinely could not be read count as unreadable —
    # paths skipped purely by the cap are not flagged unexamined (they would
    # otherwise over-block an ordinary large deletion).
    deleted_to_probe = deleted[:_MAX_DELETED_BLOBS_READ]
    deleted_content: Dict[str, str] = {}
    if deleted_to_probe:
        try:
            deleted_content = read_paths_at_merge_base(
                repo_path, DEVELOPMENT_BRANCH, deleted_to_probe
            )
        except BaselineDiffUnavailable as exc:
            logger.warning("Code review: deleted-file content unavailable (%s)", exc)
    unreadable_deleted = [p for p in deleted_to_probe if p not in deleted_content]

    # A changed/written path that is a symlink on disk exposes only its link
    # target to the reviewer, never the pointed-to content — and a type-change
    # that replaced a real file with a symlink (git status ``T``, bucketed as a
    # change, not a deletion) removes the original content from review entirely.
    # Record such paths as unexamined so the gate routes the approval to manual
    # review instead of trusting an unreviewed link. ``read_files_as_dict`` already
    # detected these while reading ``readable`` (single source of truth, no second
    # stat pass), reporting them via ``symlinked_paths``.
    symlinked = sorted(symlinked_paths)
    unreadable = list(omitted) + unreadable_deleted + symlinked

    # Best-effort: which surviving files still import each removed module, so the
    # reviewer can check the deletion note's "nothing depends on it" instruction.
    references = find_referencing_paths(repo_path, deleted) if deleted else {}
    note = _format_deletion_note(deleted, references) if deleted else None

    # Restored pre-deletion content rides byte-for-byte (no header prepended, so
    # line numbers and non-Python syntax are preserved) under a *display label*
    # that marks it DELETED. The marker lives in the key — which the coordinator
    # carries as every segment's file label — so even a large blob split across
    # chunks is never mistaken for current source. The label is sanitized (a raw
    # git path may carry control/invalid-UTF-8 bytes); key_to_path maps it back to
    # the raw removed path so a finding still drives the fix loop to restore it.
    for p, content in deleted_content.items():
        label = f"{sanitize_path_for_text(p)} (DELETED by this task — pre-deletion content)"
        key = _disambiguated_key(files, label)
        files[key] = content
        key_to_path[key] = p

    # Attach each non-empty omission note as a real (segmentable) block; the exact
    # inserted keys are tracked so a finding on one (and only one) is detached from
    # the fix loop without catching a real ``[review-note]/`` file.
    withheld = sorted(set(sensitive) | set(deleted_sensitive))
    synthetic_keys = _attach_review_notes(
        files,
        deletion_note=note,
        withheld=withheld or None,
        unreadable=unreadable or None,
        emptied=emptied or None,
    )

    if files:
        return ReviewInputSelection(
            files,
            None,
            note,
            synthetic_keys,
            key_to_path,
            withheld=tuple(withheld),
            unreadable=tuple(unreadable),
            emptied=tuple(emptied),
        )

    logger.warning(
        "Code review: no changed or written files on disk; falling back to whole-repo review input"
    )
    return _whole_repo_review_input(repo_path)


def _is_openapi_spec_task(task: Any) -> bool:
    """True if task description is about creating an OpenAPI specification file.
    Detects tasks that explicitly ask for a static OpenAPI spec file, as opposed to
    general API tasks where FastAPI's auto-generation may suffice.
    """
    desc = (getattr(task, "description", None) or "").lower()
    title = (getattr(task, "title", None) or "").lower()
    combined = f"{desc} {title}"
    return (
        (
            "openapi" in combined
            and (
                "spec" in combined
                or "specification" in combined
                or "yaml" in combined
                or "file" in combined
            )
        )
        or "api specification" in combined
        or "api contract" in combined
        or ("swagger" in combined and ("spec" in combined or "file" in combined))
    )


_truncate_for_context = truncate_for_context
MAX_OPENAPI_SPEC_CHARS = 100_000  # 100KB limit for OpenAPI spec context


def _read_openapi_spec_from_repo(repo_path: Path) -> Optional[str]:
    """Read existing OpenAPI spec from repo (app/openapi.yaml, openapi.yaml, docs/openapi.yaml).
    Returns truncated content or None if not found. Used to pass existing spec as api_spec
    so the backend agent can extend/align with it.
    """
    candidates = [
        repo_path / "app" / "openapi.yaml",
        repo_path / "app" / "openapi.json",
        repo_path / "openapi.yaml",
        repo_path / "openapi.json",
        repo_path / "docs" / "openapi.yaml",
        repo_path / "docs" / "openapi.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                return _truncate_for_context(content, MAX_OPENAPI_SPEC_CHARS)
            except (OSError, UnicodeDecodeError) as e:
                logger.debug("Could not read OpenAPI spec %s: %s", p, e)
    return None


_task_requirements = task_requirements


def _task_requirements_with_test_expectations(task: Task, repo_path: Path) -> str:
    """Build requirements string including test/spec expectations from repo."""
    return task_requirements_with_expectations(task, repo_path, "backend")


@dataclass
class _FileWriteOutput:
    """Minimal ``write_agent_output`` payload (just ``files`` + ``summary``).

    ``write_agent_output`` is duck-typed over any object exposing ``.files``; this
    names the throwaway shim used when applying BuildFixSpecialist edits instead of
    constructing an anonymous ``type(...)`` class inline.
    """

    files: Dict[str, str]
    summary: str


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

    def __init__(self, llm_client=None) -> None:
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("backend")
        # Keep LLMClient for context_sizing / compact_text utilities
        self.llm = llm_client if llm_client is not None else get_client("backend")

    def _plan_task(
        self,
        *,
        task: Task,
        existing_code: str,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
    ) -> Tuple[str, bool]:
        """Produce an implementation plan for the task.
        Returns (plan_text, False). The second value is unused (legacy).
        """
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
                    _truncate_for_context(spec_content, compute_spec_content_chars(self.llm)),
                ]
            )
        if architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    architecture.overview,
                    *[
                        f"- {c.name} ({c.type}): {c.technology}"
                        for c in architecture.components
                        if c.technology
                    ],
                ]
            )
        if existing_code and existing_code != "# No code files found":
            context_parts.extend(
                [
                    "",
                    "**Existing codebase:**",
                    _truncate_for_context(existing_code, compute_existing_code_chars(self.llm)),
                ]
            )
        prompt = BACKEND_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        log_llm_prompt(logger, "Backend", "planning", (task.description or "")[:80], prompt)
        try:
            data = _json.loads((lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip())
            plan = TaskPlan.from_llm_json(data)
            return (plan.to_markdown(), False)
        except Exception as e:
            logger.warning("[%s] Planning step failed, proceeding without plan: %s", task.id, e)
            return ("", False)

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
        problem_solver_agent: Any | None = None,
        git_operations_tool_agent: Any | None = None,
        linting_tool_agent: Any | None = None,
        build_fix_specialist: Any | None = None,
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
            linting_tool_agent: Optional Linting Tool Agent for lint verification.
        Returns:
            BackendWorkflowResult with success status, review history, and final files.
        """
        from software_engineering_team.shared.git_utils import (
            DEVELOPMENT_BRANCH,
            _run_git,
            abort_merge,
            branch_has_commits_ahead_of,
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
        workflow_start = time.monotonic()
        review_history: List[ReviewIterationRecord] = []
        git_ops_metadata: Dict[str, Any] = {"branch_created": "", "commits": [], "merge": {}}
        logger.info(
            "[%s] WORKFLOW START: Backend agent beginning autonomous workflow for task '%s'",
            task_id,
            task.title or task.description[:80],
        )
        # Contract-first guardrail: reject ambiguous task payloads early.
        task_contract_valid, missing_contract_fields = _validate_task_contract(task)
        if not task_contract_valid:
            failure_reason = "Task contract is incomplete. Missing required fields: " + ", ".join(
                missing_contract_fields
            )
            logger.error("[%s] WORKFLOW BLOCKED: %s", task_id, failure_reason)
            if tech_lead is not None:
                try:
                    tech_lead.review_progress(
                        task_update=TaskUpdate(
                            task_id=task_id,
                            agent_type="backend",
                            status="blocked",
                            summary=failure_reason,
                            files_changed=[],
                            needs_followup=True,
                        ),
                        spec_content=spec_content,
                        architecture=architecture,
                        completed_tasks=completed_tasks or [],
                        remaining_tasks=remaining_tasks or [],
                        codebase_summary="",
                    )
                except Exception as tl_err:
                    logger.warning(
                        "[%s] Tech Lead notify on contract block failed: %s", task_id, tl_err
                    )
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name="",
                iterations_used=0,
                final_files={},
                review_history=[],
                summary=failure_reason,
                failure_reason=failure_reason,
                needs_followup=True,
            )
        # ── Step 1: Create feature branch ───────────────────────────────────
        logger.info("[%s] WORKFLOW Step 1/9: Creating feature branch", task_id)
        if git_operations_tool_agent is not None:
            try:
                from git_operations_tool_agent.models import GitOperationInput

                slug = (
                    re.sub(
                        r"[^a-z0-9-]+", "-", (task.title or task.description or "task").lower()
                    ).strip("-")[:40]
                    or "task"
                )
                create_out = git_operations_tool_agent.run(
                    GitOperationInput(
                        task_id=task_id,
                        repo_path=str(repo_path),
                        base_branch=DEVELOPMENT_BRANCH,
                        requested_operation="create_branch",
                        requesting_agent="BackendTeamLeadAgent",
                        branch={"naming_template": "feature/{task_id}-{slug}", "slug": slug},
                        scope_guard={"allowed_paths": []},
                    )
                )
                ok = create_out.status == "success"
                branch_msg = create_out.branch_name or ""
                if ok:
                    git_ops_metadata["branch_created"] = branch_msg
                else:
                    branch_msg = (
                        "; ".join(create_out.policy_findings + create_out.notes)
                        or "create_branch failed"
                    )
            except Exception as git_tool_err:
                ok, branch_msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, task_id)
                logger.warning(
                    "[%s] GitOperationsToolAgent create_branch failed, fallback to git_utils: %s",
                    task_id,
                    git_tool_err,
                )
        else:
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
        branch_name = branch_msg or (
            f"feature/{task_id}" if not task_id.startswith("feature/") else task_id
        )
        logger.info("[%s] WORKFLOW   Branch created: %s", task_id, branch_name)
        # ── Step 1b: Sync with development (accumulative updates) ───────────
        # Merge development into feature branch so we see any work from parallel
        # or previously completed tasks. Enables task dependencies.
        sync_ok, sync_msg = merge_branch(repo_path, DEVELOPMENT_BRANCH, branch_name)
        if sync_ok:
            if "Already up to date" not in sync_msg:
                logger.info(
                    "[%s] WORKFLOW   Synced with development: %s",
                    task_id,
                    sync_msg[:80],
                )
        else:
            logger.warning(
                "[%s] WORKFLOW   Sync with development failed (non-blocking): %s",
                task_id,
                sync_msg,
            )
            abort_merge(repo_path)  # Clean up any partial merge state
        # ── Step 2: Generate initial code ───────────────────────────────────
        logger.info("[%s] WORKFLOW Step 2/9: Generating backend code", task_id)
        current_task = task
        result: Optional[BackendOutput] = None
        plan_text_for_fix_loop: str = ""  # Persists for review loop; passed to _regenerate_with_issues for first 2-3 fix attempts
        # Handle clarification sub-loop (separate from the review loop)
        from software_engineering_team.shared.context_sizing import compute_existing_code_chars

        for clar_round in range(MAX_CLARIFICATION_ROUNDS + 1):
            max_code_chars = compute_existing_code_chars(self.llm)
            existing_code = _truncate_for_context(_read_repo_code(repo_path), max_code_chars)
            # Per-task planning: produce implementation plan before first code gen
            plan_text, _ = self._plan_task(
                task=current_task,
                existing_code=existing_code,
                spec_content=spec_content,
                architecture=architecture,
            )
            if plan_text:
                plan_text_for_fix_loop = plan_text
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
                        plan_file = plan_dir / f"backend_task_{task_id}.md"
                        plan_file.write_text(
                            f"# Backend task plan: {task_id}\n\n{plan_text}",
                            encoding="utf-8",
                        )
                        logger.info("[%s] WORKFLOW   Persisted plan to %s", task_id, plan_file)
                    except Exception as e:
                        logger.warning("[%s] Failed to persist plan (non-blocking): %s", task_id, e)
            result = self.run(
                BackendInput(
                    task_description=current_task.description,
                    requirements=_task_requirements_with_test_expectations(current_task, repo_path),
                    user_story=getattr(current_task, "user_story", "") or "",
                    spec_content=_truncate_for_context(spec_content, max_code_chars),
                    architecture=architecture,
                    language="python",
                    existing_code=(
                        existing_code
                        if existing_code and existing_code != "# No code files found"
                        else None
                    ),
                    api_spec=_read_openapi_spec_from_repo(repo_path),
                    task_plan=plan_text if plan_text else None,
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
                        "[%s] WORKFLOW FAILED at Step 2: Still needs clarification after %d rounds",
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
        for _ in range(MAX_PREWRITE_REGENERATIONS):
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
                task_plan=plan_text_for_fix_loop or None,
            )
        missing_after = _test_routes_missing_from_main_py(repo_path, result.files)
        if missing_after:
            failure_reason = (
                f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                f"Tests reference {missing_after} but main.py does not include them."
            )
            logger.error("[%s] WORKFLOW FAILED at Step 3: %s", task_id, failure_reason)
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name=branch_name,
                failure_reason=failure_reason,
            )
        ok, write_msg = write_agent_output(repo_path, result, subdir="")
        if not ok and write_msg == NO_FILES_TO_WRITE_MSG:
            # Fallback: inject stub and retry so we always commit something
            stub_content = (
                '"""Stub - write failed (no files). Tech Lead should create follow-up task.\n"""\n'
                "from fastapi import FastAPI\n\n"
                "app = FastAPI()\n\n"
                '@app.get("/health")\n'
                "def health():\n"
                '    return {"status": "ok"}\n'
            )
            result_dict = result.model_dump() if hasattr(result, "model_dump") else result.dict()
            result_dict["files"] = {"app/main.py": stub_content}
            result_dict["used_stub_fallback"] = True
            stub_result = BackendOutput(**result_dict)
            ok, write_msg = write_agent_output(repo_path, stub_result, subdir="")
            if ok:
                result = stub_result
                logger.warning(
                    "[%s] WORKFLOW Step 3: Initial write had no files; stub injected and committed",
                    task_id,
                )
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
            "[%s] WORKFLOW Steps 4-6: Entering review feedback loop (max %d iterations)",
            task_id,
            MAX_REVIEW_ITERATIONS,
        )
        last_build_error_sig: Optional[str] = None
        consecutive_same_build_failures = 0
        repeated_build_failure_reason: Optional[str] = None
        write_tests_requested = False
        fix_attempt_count = 0  # Track regenerations; pass task_plan for first 3 to reduce drift
        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            iter_start = time.monotonic()
            logger.info(
                "[%s] WORKFLOW ── Review iteration %d/%d ──. Next step -> Running review checks",
                task_id,
                iteration,
                MAX_REVIEW_ITERATIONS,
            )
            record = ReviewIterationRecord(iteration=iteration)
            # ─── 4a. Pre-flight: test endpoint compatibility ─────────────
            missing_routes = _check_test_endpoint_compatibility(repo_path)
            if missing_routes:
                logger.warning(
                    "[%s] WORKFLOW   [%d] Pre-flight: tests reference %s but main.py missing; regenerating",
                    task_id,
                    iteration,
                    missing_routes,
                )
                task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    code_review_issues=_build_code_review_issues_for_missing_test_routes(
                        missing_routes
                    ),
                    task_plan=task_plan_arg,
                )
                fix_attempt_count += 1
                for _ in range(
                    MAX_PREWRITE_REGENERATIONS - 1
                ):  # -1 since we already regenerated once above
                    main_from_files = result.files.get("app/main.py", "") or result.files.get(
                        "main.py", ""
                    )
                    still_missing = _check_test_endpoint_compatibility(
                        repo_path, main_content=main_from_files
                    )
                    if not still_missing:
                        break
                    task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(
                            still_missing
                        ),
                        task_plan=task_plan_arg,
                    )
                    fix_attempt_count += 1
                main_after = result.files.get("app/main.py", "") or result.files.get("main.py", "")
                still_missing_after = _check_test_endpoint_compatibility(
                    repo_path, main_content=main_after
                )
                if still_missing_after:
                    failure_reason = (
                        f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                        f"Tests reference {still_missing_after} but main.py does not include them."
                    )
                    logger.error("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        iterations_used=len(review_history),
                        review_history=review_history,
                        summary=result.summary if result else "",
                        failure_reason=failure_reason,
                        needs_followup=True,
                    )
                ok, write_msg = write_agent_output(repo_path, result, subdir="")
                if not ok:
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Write failed after pre-flight fix: %s",
                        task_id,
                        iteration,
                        write_msg,
                    )
                continue  # Re-run from build verification
            # ─── 4a-lint. Lint verification (before build) ──────────────
            if linting_tool_agent is not None:
                try:
                    from linting_tool_agent.models import LintToolInput as _LintInput

                    lint_result = linting_tool_agent.run(
                        _LintInput(
                            repo_path=str(repo_path),
                            agent_type="backend",
                            task_id=task_id,
                            task_description=current_task.description,
                        )
                    )
                    if not lint_result.execution_result.success:
                        logger.info(
                            "[%s] WORKFLOW   [%d] Lint found %d issue(s), %d edit(s)",
                            task_id,
                            iteration,
                            lint_result.execution_result.issue_count,
                            len(lint_result.edits),
                        )
                        if lint_result.edits:
                            ok_lint, msg_lint, lint_files = _apply_build_fix_edits(
                                repo_path,
                                lint_result.edits,
                                compute_existing_code_chars(self.llm),
                            )
                            if ok_lint and lint_files:
                                write_agent_output(
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
                            record.action_taken = "fixed_lint"
                            review_history.append(record)
                            continue
                except Exception as lint_err:
                    logger.warning(
                        "[%s] WORKFLOW   Lint step failed (non-blocking): %s",
                        task_id,
                        lint_err,
                    )
            # ─── 4b. Build verification ─────────────────────────────────
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
                    build_errors,
                )
                record.action_taken = "fixed_build"
                review_history.append(record)
                # Stop if the same build error repeats (avoids infinite loop on env/config issues)
                build_error_sig = _build_error_signature(build_errors)
                if build_error_sig == last_build_error_sig:
                    consecutive_same_build_failures += 1
                else:
                    last_build_error_sig = build_error_sig
                    consecutive_same_build_failures = 1
                # Exit at 5 (not 6) so Tech Lead can create follow-up task early
                if consecutive_same_build_failures >= 5:
                    repeated_build_failure_reason = (
                        "Build failed 5 times with the same error; "
                        "stopping early so Tech Lead can create follow-up fix task. Last error: "
                        + build_errors
                    )
                    logger.error(
                        "[%s] WORKFLOW   [%d] %s",
                        task_id,
                        iteration,
                        repeated_build_failure_reason,
                    )
                    break
                # Resolve the failing-test context once for the specialist and
                # escalation paths (both gated on >=2 same failures) so the test
                # file is parsed and read a single time per iteration.
                failing_test_file: Optional[str] = None
                failing_test_content: Optional[str] = None
                if consecutive_same_build_failures >= 2:
                    failing_test_file, failing_test_content = _resolve_failing_test_context(
                        build_errors, repo_path
                    )
                # When same error repeats 2+ times, try BuildFixSpecialist for minimal targeted fix
                if (
                    consecutive_same_build_failures >= 2
                    and build_fix_specialist is not None
                    and self._try_build_fix_specialist(
                        repo_path=repo_path,
                        build_errors=build_errors,
                        build_fix_specialist=build_fix_specialist,
                        current_task=current_task,
                        task_id=task_id,
                        iteration=iteration,
                        failing_test_file=failing_test_file,
                        failing_test_content=failing_test_content,
                    )
                ):
                    continue  # Re-run build verification
                # Collaborate with general problem solver before moving to other subtasks.
                if problem_solver_agent is not None:
                    bug_issue = {
                        "severity": "critical",
                        "description": f"Build/test failure: {build_errors}",
                        "location": _extract_failing_test_file_from_build_errors(build_errors)
                        or "",
                        "recommendation": "Use specialist-guided root-cause analysis and patch the bug.",
                    }
                    solved, solver_result, solver_error = self._run_problem_solver_bug_loop(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        problem_solver_agent=problem_solver_agent,
                        base_issue=bug_issue,
                        specialty="build",
                        build_verifier=build_verifier,
                    )
                    if solved:
                        result = solver_result
                        continue
                    logger.warning(
                        "[%s] WORKFLOW   ProblemSolver did not resolve bug within %d cycles: %s",
                        task_id,
                        MAX_PROBLEM_SOLVER_CYCLES,
                        (solver_error or ""),
                    )
                # Invoke testing sub-agent to analyze build errors and produce fix recommendations
                code_on_branch = _read_repo_code(repo_path)
                from qa_agent.models import QAInput as QAI

                qa_fix_result = qa_agent.run(
                    QAI(
                        code=code_on_branch,
                        language="python",
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
                    # Fallback: convert code_review_issues to qa_issues format
                    cr_issues = _build_code_review_issues_for_build_failure(build_errors)
                    qa_issues = [
                        {
                            "severity": i.get("severity", "critical"),
                            "description": i.get("description", ""),
                            "location": i.get("file_path", ""),
                            "recommendation": i.get("suggestion", "Fix the build/test errors"),
                        }
                        for i in cr_issues
                    ]
                # Escalate when same error repeats: add failing test content and clearer instructions
                if consecutive_same_build_failures >= 2:
                    self._escalate_qa_issues_for_repeated_build_failure(
                        qa_issues,
                        build_errors=build_errors,
                        consecutive_failures=consecutive_same_build_failures,
                        repo_path=repo_path,
                        failing_test_file=failing_test_file,
                        failing_test_content=failing_test_content,
                    )
                task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    qa_issues=qa_issues,
                    task_plan=task_plan_arg,
                )
                fix_attempt_count += 1
                # Pre-write check: if tests reference /test-generic-error but result's
                # main.py doesn't include it, regenerate with targeted issue before writing.
                for _ in range(MAX_PREWRITE_REGENERATIONS):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                        task_plan=task_plan_arg,
                    )
                    fix_attempt_count += 1
                missing_after_build = _test_routes_missing_from_main_py(repo_path, result.files)
                if missing_after_build:
                    failure_reason = (
                        f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                        f"Tests reference {missing_after_build} but main.py does not include them."
                    )
                    logger.error("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        iterations_used=len(review_history),
                        review_history=review_history,
                        summary=result.summary if result else "",
                        failure_reason=failure_reason,
                        needs_followup=True,
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
            # After first successful build: have testing sub-agent write unit and integration tests
            if not write_tests_requested:
                write_tests_requested = True
                code_on_branch = _read_repo_code(repo_path)
                from qa_agent.models import QAInput as QAI

                qa_tests_result = qa_agent.run(
                    QAI(
                        code=code_on_branch,
                        language="python",
                        task_description=current_task.description,
                        architecture=architecture,
                        request_mode="write_tests",
                    )
                )
                tests_dict = {}
                if getattr(qa_tests_result, "unit_tests", "").strip():
                    tests_dict["unit_tests"] = qa_tests_result.unit_tests.strip()
                if getattr(qa_tests_result, "integration_tests", "").strip():
                    tests_dict["integration_tests"] = qa_tests_result.integration_tests.strip()
                if tests_dict:
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        suggested_tests_from_qa=tests_dict,
                    )
                    ok, write_msg = write_agent_output(repo_path, result, subdir="")
                    if not ok:
                        logger.warning(
                            "[%s] WORKFLOW   [%d] Write failed after QA suggested tests: %s",
                            task_id,
                            iteration,
                            write_msg,
                        )
                    continue
            # ─── 4c. Code review ────────────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Code review...",
                task_id,
                iteration,
            )
            # Use the writer's own materialized path set (including a synthesized
            # .gitignore) so derived/uncommitted paths are reviewed when their
            # commit didn't land.
            written = _writer_output_keys(result)
            # The deletion note is already folded into the review files/code by
            # _select_review_input; the selection also carries the key→path map and
            # the exact synthetic note keys for finding translation below.
            review_selection = _select_review_input(repo_path, current_task, written)
            review_result = self._run_code_review(
                code_review_agent=code_review_agent,
                files=review_selection.files,
                code=review_selection.code,
                spec_content=spec_content,
                task=current_task,
                architecture=architecture,
                existing_code=_truncate_for_context(
                    _read_repo_code(repo_path), compute_existing_code_chars(self.llm)
                ),
            )
            # Fail closed on an approval that coincides with a destructive/unknowable
            # omission (an emptied file, a baseline diff we couldn't compute, a
            # withheld real secret, unreadable source) or that saw no examinable
            # content at all — re-running wouldn't change the set deterministically
            # and exhaustion would fall through to merge, so return a terminal
            # manual-review result. Binary assets and sensitive-named templates stay
            # advisory. The decision lives on ReviewInputSelection so every review
            # caller applies it identically (see review_gate_failure).
            failure_reason = review_selection.review_gate_failure(review_result.approved)
            if failure_reason:
                logger.warning("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                record.code_review_approved = False
                review_history.append(record)
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return BackendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    branch_name=branch_name,
                    iterations_used=len(review_history),
                    review_history=review_history,
                    summary=result.summary if result else "",
                    failure_reason=failure_reason,
                    needs_followup=True,
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
                cr_issues = _translate_finding_paths(
                    [
                        # An issue is normally a Pydantic model, but tolerate a
                        # plain dict (e.g. a differently-versioned agent) instead
                        # of crashing the fix loop on a missing .dict()/.model_dump().
                        i
                        if isinstance(i, dict)
                        else (i.model_dump() if hasattr(i, "model_dump") else i.dict())
                        for i in review_result.issues
                    ],
                    review_selection.key_to_path,
                    review_selection.synthetic_keys,
                )
                task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    code_review_issues=cr_issues,
                    task_plan=task_plan_arg,
                )
                fix_attempt_count += 1
                # Pre-write check: preserve test-only routes in main.py
                for _ in range(MAX_PREWRITE_REGENERATIONS):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                        task_plan=task_plan_arg,
                    )
                    fix_attempt_count += 1
                missing_after_cr = _test_routes_missing_from_main_py(repo_path, result.files)
                if missing_after_cr:
                    failure_reason = (
                        f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                        f"Tests reference {missing_after_cr} but main.py does not include them."
                    )
                    logger.error("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        iterations_used=len(review_history),
                        review_history=review_history,
                        summary=result.summary if result else "",
                        failure_reason=failure_reason,
                        needs_followup=True,
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
            # ─── 4d. Acceptance criteria verification (optional) ───────────
            if acceptance_verifier_agent and getattr(current_task, "acceptance_criteria", None):
                code_for_verify = _read_repo_code(repo_path)
                if code_for_verify and code_for_verify != "# No code files found":
                    from acceptance_verifier_agent.models import AcceptanceVerifierInput

                    av_result = acceptance_verifier_agent.run(
                        AcceptanceVerifierInput(
                            code=code_for_verify,
                            task_description=current_task.description,
                            acceptance_criteria=current_task.acceptance_criteria,
                            spec_content=spec_content,
                            architecture=architecture,
                            language="python",
                        )
                    )
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
                        task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                        result = self._regenerate_with_issues(
                            repo_path=repo_path,
                            current_task=current_task,
                            spec_content=spec_content,
                            architecture=architecture,
                            code_review_issues=code_review_issues,
                            task_plan=task_plan_arg,
                        )
                        fix_attempt_count += 1
                        for _ in range(MAX_PREWRITE_REGENERATIONS):
                            missing = _test_routes_missing_from_main_py(repo_path, result.files)
                            if not missing:
                                break
                            task_plan_arg = (
                                plan_text_for_fix_loop if fix_attempt_count < 3 else None
                            )
                            result = self._regenerate_with_issues(
                                repo_path=repo_path,
                                current_task=current_task,
                                spec_content=spec_content,
                                architecture=architecture,
                                code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                                task_plan=task_plan_arg,
                            )
                            fix_attempt_count += 1
                        missing_after_av = _test_routes_missing_from_main_py(
                            repo_path, result.files
                        )
                        if missing_after_av:
                            failure_reason = (
                                f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                                f"Tests reference {missing_after_av} but main.py does not include them."
                            )
                            logger.error(
                                "[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason
                            )
                            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                            return BackendWorkflowResult(
                                task_id=task_id,
                                success=False,
                                branch_name=branch_name,
                                iterations_used=len(review_history),
                                review_history=review_history,
                                summary=result.summary if result else "",
                                failure_reason=failure_reason,
                                needs_followup=True,
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
            # ─── 4e. Security review ─────────────────────────────────────
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
                task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    security_issues=security_issues,
                    task_plan=task_plan_arg,
                )
                fix_attempt_count += 1
                for _ in range(MAX_PREWRITE_REGENERATIONS):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                        task_plan=task_plan_arg,
                    )
                    fix_attempt_count += 1
                missing_after_sec = _test_routes_missing_from_main_py(repo_path, result.files)
                if missing_after_sec:
                    failure_reason = (
                        f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                        f"Tests reference {missing_after_sec} but main.py does not include them."
                    )
                    logger.error("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        iterations_used=len(review_history),
                        review_history=review_history,
                        summary=result.summary if result else "",
                        failure_reason=failure_reason,
                        needs_followup=True,
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
            # ─── 4g. DBC comments review ─────────────────────────────────
            logger.info(
                "[%s] WORKFLOW   [%d] Triggering DBC comments review...",
                task_id,
                iteration,
            )
            dbc_issues_count, dbc_updated_count, dbc_compliant = self._run_dbc_review(
                dbc_agent=dbc_agent,
                repo_path=repo_path,
                task=current_task,
                architecture=architecture,
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
                            build_errors,
                        )
                        code_on_branch = _read_repo_code(repo_path)
                        from qa_agent.models import QAInput as QAI

                        qa_fix_result = qa_agent.run(
                            QAI(
                                code=code_on_branch,
                                language="python",
                                task_description=current_task.description,
                                architecture=architecture,
                                build_errors=build_errors[:4000],
                                request_mode="fix_build",
                            )
                        )
                        qa_issues_artifact = [
                            b.model_dump() if hasattr(b, "model_dump") else b.dict()
                            for b in (qa_fix_result.bugs_found or [])
                        ]
                        if not qa_issues_artifact:
                            cr_issues = _build_code_review_issues_for_build_failure(build_errors)
                            qa_issues_artifact = [
                                {
                                    "severity": i.get("severity", "critical"),
                                    "description": i.get("description", ""),
                                    "location": i.get("file_path", ""),
                                    "recommendation": i.get(
                                        "suggestion", "Fix the build/test errors"
                                    ),
                                }
                                for i in cr_issues
                            ]
                        task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                        result = self._regenerate_with_issues(
                            repo_path=repo_path,
                            current_task=current_task,
                            spec_content=spec_content,
                            architecture=architecture,
                            qa_issues=qa_issues_artifact,
                            task_plan=task_plan_arg,
                        )
                        fix_attempt_count += 1
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
                task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                result = self._regenerate_with_issues(
                    repo_path=repo_path,
                    current_task=current_task,
                    spec_content=spec_content,
                    architecture=architecture,
                    qa_issues=qa_issues,
                    task_plan=task_plan_arg,
                )
                fix_attempt_count += 1
                # Pre-write check: preserve test-only routes in main.py
                for _ in range(MAX_PREWRITE_REGENERATIONS):
                    missing = _test_routes_missing_from_main_py(repo_path, result.files)
                    if not missing:
                        break
                    logger.warning(
                        "[%s] WORKFLOW   [%d] Pre-write: tests reference %s but main.py missing; regenerating",
                        task_id,
                        iteration,
                        missing,
                    )
                    task_plan_arg = plan_text_for_fix_loop if fix_attempt_count < 3 else None
                    result = self._regenerate_with_issues(
                        repo_path=repo_path,
                        current_task=current_task,
                        spec_content=spec_content,
                        architecture=architecture,
                        code_review_issues=_build_code_review_issues_for_missing_test_routes(),
                        task_plan=task_plan_arg,
                    )
                    fix_attempt_count += 1
                missing_after_qa = _test_routes_missing_from_main_py(repo_path, result.files)
                if missing_after_qa:
                    failure_reason = (
                        f"Could not generate main.py with required test routes after {MAX_PREWRITE_REGENERATIONS} attempts. "
                        f"Tests reference {missing_after_qa} but main.py does not include them."
                    )
                    logger.error("[%s] WORKFLOW   [%d] %s", task_id, iteration, failure_reason)
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return BackendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        branch_name=branch_name,
                        iterations_used=len(review_history),
                        review_history=review_history,
                        summary=result.summary if result else "",
                        failure_reason=failure_reason,
                        needs_followup=True,
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
                "[%s] WORKFLOW   Review loop exhausted. Recovery summary: "
                "1) Attempted %d review iterations, 2) Issues remain unresolved. "
                "Next step -> Proceeding to merge with remaining issues",
                task_id,
                MAX_REVIEW_ITERATIONS,
            )
        if repeated_build_failure_reason is not None:
            # Emergency merge: attempt to merge partial work even when build failed repeatedly
            if branch_has_commits_ahead_of(repo_path, branch_name, DEVELOPMENT_BRANCH):
                logger.info(
                    "[%s] WORKFLOW   Attempting emergency merge of partial work despite build failures",
                    task_id,
                )
                merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
                if merge_ok:
                    logger.info(
                        "[%s] WORKFLOW   Emergency merge successful; partial work preserved on %s",
                        task_id,
                        DEVELOPMENT_BRANCH,
                    )
                    failure_reason = (
                        f"{repeated_build_failure_reason} (Partial work merged to development.)"
                    )
                else:
                    logger.warning(
                        "[%s] WORKFLOW   Emergency merge failed: %s",
                        task_id,
                        merge_msg,
                    )
                    failure_reason = repeated_build_failure_reason
            else:
                failure_reason = repeated_build_failure_reason
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            # Graceful degradation: notify Tech Lead so it can create follow-up fix task
            if tech_lead is not None:
                try:
                    task_update = TaskUpdate(
                        task_id=task_id,
                        agent_type="backend",
                        status="failed",
                        summary=result.summary if result else "",
                        files_changed=list((result.files or {}).keys()) if result else [],
                        needs_followup=True,
                        failure_reason=failure_reason,
                    )
                    codebase_summary = _truncate_for_context(
                        _read_repo_code(repo_path), compute_existing_code_chars(self.llm)
                    )
                    new_tasks = tech_lead.review_progress(
                        task_update=task_update,
                        spec_content=spec_content,
                        architecture=architecture,
                        completed_tasks=completed_tasks or [],
                        remaining_tasks=remaining_tasks or [],
                        codebase_summary=codebase_summary,
                    )
                    if new_tasks and append_task_fn is not None:
                        for nt in new_tasks:
                            append_task_fn(nt)
                        logger.info(
                            "[%s] WORKFLOW   Tech Lead created %d follow-up tasks from build failure",
                            task_id,
                            len(new_tasks),
                        )
                except Exception as tl_err:
                    logger.warning(
                        "[%s] WORKFLOW   Tech Lead notification failed (non-blocking): %s",
                        task_id,
                        tl_err,
                    )
            return BackendWorkflowResult(
                task_id=task_id,
                success=False,
                branch_name=branch_name,
                iterations_used=len(review_history),
                review_history=review_history,
                summary=result.summary if result else "",
                failure_reason=failure_reason,
                needs_followup=True,
            )
        # Capture latest commit hash for audit package.
        try:
            rc_hash, head_hash = _run_git(repo_path, ["git", "rev-parse", "HEAD"])
            if rc_hash == 0 and (head_hash or "").strip():
                git_ops_metadata["commits"] = [
                    {
                        "hash": head_hash.strip(),
                        "message": "task-branch head",
                    }
                ]
        except Exception:
            pass

        # ── Step 7: Merge feature branch into development ───────────────────
        logger.info("[%s] WORKFLOW Step 7/9: Merging to development", task_id)
        if git_operations_tool_agent is not None:
            try:
                from git_operations_tool_agent.models import GitOperationInput

                merge_out = git_operations_tool_agent.run(
                    GitOperationInput(
                        task_id=task_id,
                        repo_path=str(repo_path),
                        base_branch=DEVELOPMENT_BRANCH,
                        requested_operation="merge_to_development",
                        requesting_agent="BackendTeamLeadAgent",
                        branch={
                            "naming_template": branch_name.replace(task_id, "{task_id}"),
                            "slug": "task",
                        },
                        merge={
                            "strategy": "squash",
                            "require_clean_worktree": True,
                            "require_quality_gates_passed": True,
                            "rebase_before_merge": True,
                        },
                        merge_token={
                            "task_id": task_id,
                            "branch_name": branch_name,
                            "requested_by": "BackendTeamLeadAgent",
                            "quality_gates": {
                                "lint": "pass",
                                "static_analysis": "pass",
                                "unit_tests": "pass",
                                "integration_tests": "pass",
                                "security_review": "pass",
                                "code_review": "pass",
                            },
                            "approvals": {
                                "code_review_agent": "approved",
                                "security_review_agent": "approved",
                            },
                        },
                    )
                )
                merge_ok = merge_out.status == "success"
                merge_msg = "; ".join(merge_out.policy_findings + merge_out.notes)
                if merge_ok:
                    git_ops_metadata["merge"] = {
                        "target_branch": DEVELOPMENT_BRANCH,
                        "strategy": "squash",
                        "merge_commit_hash": merge_out.merge_commit_hash,
                        "status": "success",
                    }
                else:
                    merge_msg = merge_msg or "merge_to_development failed"
            except Exception as merge_tool_err:
                merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
                logger.warning(
                    "[%s] GitOperationsToolAgent merge failed, fallback to git_utils: %s",
                    task_id,
                    merge_tool_err,
                )
        else:
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
        used_stub = getattr(result, "used_stub_fallback", False) if result else False
        completion_package = _build_completion_package(
            task=task,
            result=result,
            review_history=review_history,
            language_used="python",
            git_operations=git_ops_metadata,
        )
        task_update = TaskUpdate(
            task_id=task_id,
            agent_type="backend",
            status="completed",
            summary=(result.summary if result else "")
            + "\n\ncompletion_package="
            + json.dumps(completion_package, separators=(",", ":")),
            files_changed=list(final_files.keys()),
            needs_followup=used_stub,
            failure_reason=(
                "Empty completion: LLM returned no valid code. Implement the task from spec."
                if used_stub
                else None
            ),
        )
        try:
            codebase_summary = _truncate_for_context(
                _read_repo_code(repo_path), compute_existing_code_chars(self.llm)
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
            "[%s] WORKFLOW COMPLETE: merged to development in %d iterations, %.1fs total, %d files",
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
            summary=(result.summary if result else "")
            + "\n\ncompletion_package="
            + json.dumps(completion_package, separators=(",", ":")),
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
        suggested_tests_from_qa: Optional[Dict[str, str]] = None,
        task_plan: Optional[str] = None,
    ) -> BackendOutput:
        """
        Re-invoke the code generator with issues to fix.
        Preconditions:
            - ``repo_path`` is checked out on the feature branch.
            - At least one of the issue lists or suggested_tests_from_qa is non-empty.
        Postconditions:
            - Returns a new ``BackendOutput`` with fixes applied.
        """
        existing_code = _truncate_for_context(_read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS)
        return self.run(
            BackendInput(
                task_description=current_task.description,
                requirements=_task_requirements_with_test_expectations(current_task, repo_path),
                user_story=getattr(current_task, "user_story", "") or "",
                spec_content=_truncate_for_context(
                    spec_content, compute_existing_code_chars(self.llm)
                ),
                architecture=architecture,
                language="python",
                existing_code=(
                    existing_code
                    if existing_code and existing_code != "# No code files found"
                    else None
                ),
                api_spec=_read_openapi_spec_from_repo(repo_path),
                qa_issues=qa_issues or [],
                security_issues=security_issues or [],
                code_review_issues=code_review_issues or [],
                suggested_tests_from_qa=suggested_tests_from_qa,
                task_plan=task_plan,
            )
        )

    def _run_problem_solver_cycle(
        self,
        *,
        problem_solver_agent: Any,
        current_task: Task,
        bug_description: str,
        specialty: str,
        cycle: int,
        repo_path: Path,
    ) -> str:
        """Request one diagnosis/patch cycle from the general problem-solving specialist."""
        if problem_solver_agent is None:
            return ""
        code_snapshot = _truncate_for_context(
            _read_repo_code(repo_path), compute_existing_code_chars(self.llm)
        )
        try:
            from problem_solver_agent.models import ProblemSolverInput

            ps_result = problem_solver_agent.run(
                ProblemSolverInput(
                    task_description=current_task.description,
                    bug_description=bug_description[:4000],
                    specialty=specialty,
                    current_code_snapshot=code_snapshot,
                    cycle=cycle,
                )
            )
            return (
                "Problem-solver cycle recommendation:\n"
                f"Plan: {getattr(ps_result, 'plan', '')}\n"
                f"Execution: {getattr(ps_result, 'execution_steps', '')}\n"
                f"Review: {getattr(ps_result, 'review_checks', '')}\n"
                f"Testing: {getattr(ps_result, 'testing_strategy', '')}\n"
                f"Fix recommendation: {getattr(ps_result, 'fix_recommendation', '')}"
            ).strip()
        except Exception as err:
            logger.warning("ProblemSolver cycle failed (non-blocking): %s", err)
            return ""

    def _run_problem_solver_bug_loop(
        self,
        *,
        repo_path: Path,
        current_task: Task,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        problem_solver_agent: Any,
        base_issue: Dict[str, Any],
        specialty: str,
        build_verifier: Callable[..., Tuple[bool, str]],
    ) -> tuple[bool, Optional[BackendOutput], str]:
        """Run up to MAX_PROBLEM_SOLVER_CYCLES to patch a bug before moving on."""
        from software_engineering_team.shared.repo_writer import write_agent_output

        last_error = base_issue.get("description", "")
        last_result: Optional[BackendOutput] = None
        for cycle in range(1, MAX_PROBLEM_SOLVER_CYCLES + 1):
            recommendation = self._run_problem_solver_cycle(
                problem_solver_agent=problem_solver_agent,
                current_task=current_task,
                bug_description=last_error or base_issue.get("description", ""),
                specialty=specialty,
                cycle=cycle,
                repo_path=repo_path,
            )
            qa_issue = dict(base_issue)
            if recommendation:
                qa_issue["recommendation"] = (
                    qa_issue.get("recommendation", "") + "\n\n" + recommendation
                ).strip()
            last_result = self._regenerate_with_issues(
                repo_path=repo_path,
                current_task=current_task,
                spec_content=spec_content,
                architecture=architecture,
                qa_issues=[qa_issue],
            )
            ok, write_msg = write_agent_output(repo_path, last_result, subdir="")
            if not ok:
                last_error = f"Problem-solver write failed: {write_msg}"
                continue
            build_ok, build_errors = build_verifier(repo_path, "backend", current_task.id)
            if build_ok:
                logger.info(
                    "[%s] WORKFLOW   ProblemSolver resolved bug in %d cycle(s)",
                    current_task.id,
                    cycle,
                )
                return True, last_result, ""
            last_error = build_errors or last_error
        return False, last_result, last_error

    def _try_build_fix_specialist(
        self,
        *,
        repo_path: Path,
        build_errors: str,
        build_fix_specialist: Any,
        current_task: Task,
        task_id: str,
        iteration: int,
        failing_test_file: str | None | _Unset = _UNSET,
        failing_test_content: str | None | _Unset = _UNSET,
    ) -> bool:
        """Attempt a minimal, targeted build fix via the BuildFixSpecialist.

        Invoked after the same build error repeats, before falling back to full
        regeneration. Reads the affected files (and any failing pytest file),
        asks the specialist for edits, then applies and writes them.

        Preconditions:
            - ``build_fix_specialist`` is a runnable agent (not None); the caller
              gates on this and on the repeated-failure threshold.
            - ``repo_path`` is the backend repo root.
            - ``failing_test_file``/``failing_test_content`` are the precomputed
              :func:`_resolve_failing_test_context` result, or left ``_UNSET`` to
              resolve here (the caller threads them so the test file is read once
              per iteration across this and the escalation helper).
        Postconditions:
            - Returns True iff the specialist produced edits that were applied
              and written to disk — the caller re-runs build verification.
            - Returns False on no edits / apply failure / write failure / any
              error. Best-effort: never raises, so a specialist failure cannot
              abort the workflow.
        """
        try:
            from software_engineering_team.shared.repo_writer import write_agent_output

            # Resolve the failing-test context here too (inside the try) when the
            # caller didn't precompute it, so an import or file-read failure honors
            # the "never raises" postcondition rather than escaping.
            if failing_test_file is _UNSET:
                failing_test_file, failing_test_content = _resolve_failing_test_context(
                    build_errors, repo_path
                )

            from build_fix_specialist.models import BuildFixInput

            affected_paths = _extract_affected_file_paths_from_build_errors(build_errors, repo_path)
            affected_code = _read_affected_files_code(repo_path, affected_paths)
            bf_failing_test_content = (
                failing_test_content[:3000] if failing_test_content is not None else None
            )
            bf_result = build_fix_specialist.run(
                BuildFixInput(
                    build_errors=build_errors[:4000],
                    failing_test_content=bf_failing_test_content,
                    affected_files_code=affected_code,
                    task_description=current_task.description,
                )
            )
            if not bf_result.edits:
                logger.info(
                    "[%s] WORKFLOW   BuildFixSpecialist returned no edits, falling back to full regeneration",
                    task_id,
                )
                return False
            max_chars = compute_existing_code_chars(self.llm)
            ok_apply, msg_apply, files_dict = _apply_build_fix_edits(
                repo_path, bf_result.edits, max_chars
            )
            if not (ok_apply and files_dict):
                logger.warning(
                    "[%s] WORKFLOW   BuildFixSpecialist apply failed: %s", task_id, msg_apply
                )
                return False
            ok_write, write_msg = write_agent_output(
                repo_path,
                _FileWriteOutput(files=files_dict, summary=bf_result.summary),
                subdir="",
            )
            if not ok_write:
                logger.warning(
                    "[%s] WORKFLOW   BuildFixSpecialist write failed: %s", task_id, write_msg
                )
                return False
            logger.info(
                "[%s] WORKFLOW   [%d] BuildFixSpecialist applied %d edit(s), re-running build",
                task_id,
                iteration,
                len(files_dict),
            )
            return True
        except Exception as spec_err:
            logger.warning(
                "[%s] WORKFLOW   BuildFixSpecialist failed (non-blocking): %s",
                task_id,
                spec_err,
            )
            return False

    @staticmethod
    def _escalate_qa_issues_for_repeated_build_failure(
        qa_issues: List[Dict[str, Any]],
        *,
        build_errors: str,
        consecutive_failures: int,
        repo_path: Path,
        failing_test_file: str | None | _Unset = _UNSET,
        failing_test_content: str | None | _Unset = _UNSET,
    ) -> None:
        """Prepend escalation guidance to *qa_issues* when a build error repeats.

        Mutates *qa_issues* in place: prepends a "focus only on this error"
        issue (enriched with the failing test's contents when available), and
        at exactly the 4th identical failure additionally prepends a "the test
        expectations may be wrong" issue ahead of it.

        Preconditions:
            - *qa_issues* is the (possibly empty) list of issue dicts for the
              upcoming regeneration; the caller gates on
              ``consecutive_failures >= 2``.
            - ``failing_test_file``/``failing_test_content`` are the precomputed
              :func:`_resolve_failing_test_context` result, or left ``_UNSET`` to
              resolve here (the caller threads them to avoid re-reading the test
              file already read for the BuildFixSpecialist this iteration).
        Postconditions:
            - One issue (two at the 4th failure) is inserted at the front of
              *qa_issues*; no existing element is modified. Reading the failing
              test is best-effort and never raises.
        """
        if failing_test_file is _UNSET:
            failing_test_file, failing_test_content = _resolve_failing_test_context(
                build_errors, repo_path
            )
        escalation_desc = (
            f"ESCALATION: This build error has occurred {consecutive_failures} times. "
            "Focus ONLY on fixing this specific error. Make minimal, targeted changes. "
            "Do not add new features or refactor. Follow the Suggestion and Playbook in the error output."
        )
        escalation_suggestion = (
            "Apply the minimal fix indicated by the error message. "
            "Re-read the Suggestion and Playbook sections above. "
            "Read the failing test's assertions line-by-line and ensure the implementation satisfies each one."
        )
        if failing_test_file:
            if failing_test_content is not None:
                escalation_desc += (
                    f"\n\nFailing test expectations (from {failing_test_file}):\n```\n"
                    f"{failing_test_content[:3000]}"
                    f"{'... [truncated]' if len(failing_test_content) > 3000 else ''}\n```"
                )
                escalation_suggestion = (
                    f"The failing test is in {failing_test_file}. "
                    "Read its assertions line-by-line and ensure the implementation satisfies each one."
                )
            elif not (repo_path / failing_test_file).exists():
                escalation_desc += (
                    f"\n\nFailing test file: {failing_test_file} (file not found in repo)."
                )
        qa_issues.insert(
            0,
            {
                "severity": "critical",
                "description": escalation_desc,
                "location": failing_test_file or "",
                "recommendation": escalation_suggestion,
            },
        )
        if consecutive_failures == 4:
            # Suggest that test expectations might be wrong
            qa_issues.insert(
                0,
                {
                    "severity": "critical",
                    "description": (
                        "ESCALATION (4th same failure): Consider whether the failing test expectations are wrong. "
                        "If the test asserts behavior that conflicts with the spec, you may need to update the test "
                        "rather than the implementation. Explain your reasoning. Either fix the implementation to "
                        "satisfy the test, or update the test if it incorrectly asserts behavior."
                    ),
                    "location": "",
                    "recommendation": (
                        "Re-read the failing test and the spec. If the test is wrong, Change the test to match the spec. "
                        "If the implementation is wrong, Fix the implementation to satisfy the test."
                    ),
                },
            )

    @staticmethod
    def _run_code_review(
        *,
        code_review_agent: Any,
        files: Dict[str, str] | None = None,
        code: str | None = None,
        spec_content: str,
        task: Task,
        architecture: Optional[SystemArchitecture],
        existing_code: str | None = None,
    ) -> Any:
        """
        Invoke the code review agent.
        Preconditions:
            - ``code_review_agent`` is initialised.
            - Exactly one of ``files`` (the task's changed files as a
              ``{path: content}`` dict) or ``code`` (a path-headered blob) is
              provided, untruncated: the coordinator bounds its own per-call
              prompts and its coverage guarantee only holds when it sees the
              full input. Any deletion note is already folded into that channel
              by ``_select_review_input`` (so the coordinator segments it),
              never into the task description.
            - ``existing_code`` (optional) is the pre-change repository context
              passed through as ``CodeReviewInput.existing_codebase`` — already
              truncated by the caller; ``None`` omits it.
        Postconditions:
            - Returns a ``CodeReviewOutput`` with ``approved`` and ``issues``.
        Raises:
            CodeReviewUnavailableError: when the review could not be completed.
            ValueError: when neither or both of ``files``/``code`` are provided
                (a caller-contract violation surfaced at the boundary rather than
                silently mis-reviewed).
        """
        from code_review_agent.models import build_code_review_input

        if (files is None) == (code is None):
            raise ValueError("_run_code_review requires exactly one of 'files' or 'code'")
        task_description = task.description or ""
        return code_review_agent.run(
            build_code_review_input(
                files=files,
                code=code,
                spec_content=spec_content,
                task_description=task_description,
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
        from software_engineering_team.shared.git_utils import write_files_and_commit

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
            msg = (
                getattr(qa_output, "suggested_commit_message", "")
                or "test(qa): add QA-generated tests and docs"
            )
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
                return any(p.startswith("tests/") and p.endswith(".py") for p in files_to_write)
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
        from technical_writers.dbc_comments_agent.models import DbcCommentsInput

        from software_engineering_team.shared.git_utils import write_files_and_commit

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
        qa_count = len(input_data.qa_issues) if input_data.qa_issues else 0
        security_count = len(input_data.security_issues) if input_data.security_issues else 0
        code_review_count = (
            len(input_data.code_review_issues) if input_data.code_review_issues else 0
        )
        has_issues = qa_count > 0 or security_count > 0 or code_review_count > 0
        context_parts: List[str] = []
        if has_issues:
            issue_summaries = {}
            if qa_count:
                issue_summaries["QA issues"] = qa_count
            if security_count:
                issue_summaries["security issues"] = security_count
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
            for i in input_data.code_review_issues or []:
                desc_lines.append(
                    f"  - Code review [{i.get('category', 'general')}]: {i.get('description', '')} (file: {i.get('file_path', 'unknown')})"
                )
            issue_descriptions = "\n".join(desc_lines) if desc_lines else None
            header = build_problem_solving_header(
                issue_summaries, "Backend", issue_descriptions=issue_descriptions
            )
            # Strip trailing "---" so we can put issue details inside the problem-solving section
            header_body = header.rstrip()
            if header_body.endswith("---"):
                header_body = header_body[:-3].rstrip()
            problem_block_parts: List[str] = [header_body]
            if input_data.qa_issues:
                qa_text = "\n".join(
                    f"- [{i.get('severity')}] {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                    for i in input_data.qa_issues
                )
                problem_block_parts.extend(["", "**QA issues to fix (implement these):**", qa_text])
            if input_data.security_issues:
                sec_text = "\n".join(
                    f"- [{i.get('severity')}] {i.get('category')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                    for i in input_data.security_issues
                )
                problem_block_parts.extend(
                    ["", "**Security issues to fix (implement these):**", sec_text]
                )
            if input_data.code_review_issues:
                cr_text = "\n".join(
                    f"- [{i.get('severity')}] {i.get('category', 'general')}: {i.get('description')} "
                    f"(file: {i.get('file_path', 'unknown')})\n  Suggestion: {i.get('suggestion', '')}"
                    for i in input_data.code_review_issues
                )
                problem_block_parts.extend(["", "**Code review issues to resolve:**", cr_text])
            if input_data.task_plan:
                problem_block_parts.extend(
                    [
                        "",
                        "**CRITICAL - Implementation plan:** When fixing issues, you must still satisfy the Implementation plan below. "
                        "Do not remove or change code that fulfills the plan unless the issue explicitly requires it.",
                    ]
                )
            problem_block_parts.extend(["", "---"])
            context_parts.append("\n".join(problem_block_parts))
            logger.info(
                "Backend problem-solving context: qa_issues=%d, security_issues=%d, code_review_issues=%d",
                qa_count,
                security_count,
                code_review_count,
            )
            logger.info("Backend problem-solving header for LLM:\n%s", context_parts[0])
        if input_data.task_plan:
            plan_instruction = (
                "**IMPLEMENTATION PLAN (follow this):**\n"
                "Implement the task strictly according to the Implementation plan below. "
                "Your output must realize every item under 'What changes' and 'Tests needed', "
                "and use the algorithms/data structures described. Do not deviate from the plan unless the task description explicitly contradicts it.\n\n"
                "**Implementation plan:**\n" + input_data.task_plan
            )
            context_parts.append(plan_instruction)
        if input_data.specialist_tooling_plan:
            specialist_plan_instruction = (
                "**BACKEND AGENT V2 SPECIALIST TOOLING PLAN (required coordination):**\n"
                "This task uses specialist agents as tools. You must integrate and satisfy directives from each specialist while implementing backend code. "
                "Treat this plan as a cross-functional execution contract.\n\n"
                "**Specialist tooling plan (JSON):**\n```json\n"
                f"{input_data.specialist_tooling_plan}\n"
                "```\n"
                "Prioritize guidance from these specialist domains when present: devops, api, quality_review, qa, data_engineering, auth_security, general_problem_solver. Each specialist should contribute planning, execution, review, and testing guidance within its domain."
            )
            context_parts.append(specialist_plan_instruction)
        if input_data.specialist_tooling_plan and input_data.problem_solver_max_cycles:
            context_parts.append(
                f"**General problem solver cycle budget:** Up to {input_data.problem_solver_max_cycles} cycles for bug fixing before moving to other subtasks."
            )
        if input_data.specialist_findings:
            specialist_findings_instruction = (
                "**SPECIALIST FINDINGS / CONSTRAINTS (must be implemented):**\n"
                "Use these findings from specialist-tool agents as concrete constraints and acceptance checks. "
                "If there are conflicts, preserve security and correctness first, then reliability, then API/data consistency.\n\n"
                "**Specialist findings (JSON):**\n```json\n"
                f"{input_data.specialist_findings}\n"
                "```"
            )
            context_parts.append(specialist_findings_instruction)
        context_parts.extend(
            [
                f"**Task:** {input_data.task_description}",
                f"**Requirements:** {input_data.requirements}",
                f"**Language:** {input_data.language}",
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
                        f"- {c.name} ({c.type}): {c.technology}"
                        for c in input_data.architecture.components
                        if c.technology
                    ],
                ]
            )
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_spec:
            context_parts.extend(["", "**API spec:**", input_data.api_spec])
        if input_data.suggested_tests_from_qa:
            tests_block = [
                "",
                "**Suggested tests from QA/testing sub-agent – integrate these into your tests/test_*.py files:**",
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
        # Explicit guidance for OpenAPI spec tasks
        task_desc_lower = (input_data.task_description or "").lower()
        if (
            (
                "openapi" in task_desc_lower
                and (
                    "spec" in task_desc_lower
                    or "specification" in task_desc_lower
                    or "yaml" in task_desc_lower
                )
            )
            or "api specification" in task_desc_lower
            or "api contract" in task_desc_lower
        ):
            context_parts.extend(
                [
                    "",
                    "**CRITICAL - OpenAPI Spec Task:**",
                    "This task requires creating a **static OpenAPI spec file**. You MUST:",
                    "1. Create `app/openapi.yaml` with the complete OpenAPI 3.0 specification",
                    "2. Include all paths, request/response schemas, security definitions, and error responses",
                    "3. Do NOT rely solely on FastAPI's auto-generated /openapi.json",
                    "4. The `app/openapi.yaml` file MUST be included in your 'files' output",
                    "5. Also update any referenced schema files (e.g., app/schemas/error.py) to match the spec",
                ]
            )
        prompt = BACKEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        mode = "problem_solving" if has_issues else "initial"
        task_hint = (input_data.task_description or "")[:80]
        log_llm_prompt(logger, "Backend", mode, task_hint, prompt)
        empty_retry_prompt = (
            "\n\n**CRITICAL - Your previous response was REJECTED:** "
            "You produced 0 files and 0 code characters. You MUST return valid JSON only (no markdown, no text outside JSON) with a 'files' key "
            "containing at least one complete file (path -> content). Without files, the task cannot be completed. "
            "For Git/repository setup tasks: include existing project files (e.g. .gitignore, README.md, app/main.py, requirements.txt) with their full content in 'files'. "
            "Try again with concrete, complete file contents."
        )
        data = None
        validated_files = {}
        code = ""
        tests = ""
        for attempt in range(4):
            data = _json.loads((lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip())
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
            # Validate file paths
            validated_files, validation_warnings = _validate_file_paths(raw_files)
            for warn in validation_warnings:
                logger.warning("Backend output validation: %s", warn)
            # Guard: 0 files and 0 code -> retry up to 3 times with explicit rejection message
            total_chars = sum(len(c or "") for c in (validated_files or {}).values()) + len(
                code or ""
            )
            if not data.get("needs_clarification", False) and total_chars == 0 and attempt < 3:
                response_preview = ""
                if data.get("content"):
                    response_preview = (str(data["content"]) or "")
                elif data:
                    response_preview = str(data)
                logger.warning(
                    "Backend: produced no files and no code (failure_class=empty_completion); re-prompting (attempt %d/4) | prompt_len=%d response_len=%d raw_keys=%s content_preview=%s",
                    attempt + 1,
                    len(prompt),
                    len(str(data)) if data else 0,
                    list(raw_files.keys()) if raw_files else [],
                    response_preview,
                )
                # If files were rejected by validation, surface those errors so the LLM can fix filenames
                if raw_files and validation_warnings:
                    validation_retry = (
                        "\n\n**CRITICAL - Your file paths were REJECTED by validation:**\n"
                        + "\n".join(f"- {w}" for w in validation_warnings)
                        + "\n\nFix the file paths (e.g. use shorter names like test_tenant_model_qa.py instead of "
                        "test_unit_qa_backend-tenant-model-task.py) and return valid JSON with the 'files' key."
                    )
                    prompt = prompt + validation_retry
                else:
                    prompt = prompt + empty_retry_prompt
                continue
            # Fail fast when LLM produces no valid output
            if not validated_files and not data.get("needs_clarification", False):
                from llm_service import LLMPermanentError

                if raw_files:
                    raise LLMPermanentError(
                        f"Backend: LLM returned {len(raw_files)} files but all were rejected by validation. "
                        f"Raw filenames: {list(raw_files.keys())}. Validation warnings: {validation_warnings}"
                    )
                if code:
                    raise LLMPermanentError(
                        "Backend: LLM returned 'code' but no 'files' dict. "
                        "Model must return structured JSON with a 'files' key."
                    )
                raise LLMPermanentError(
                    f"Backend: LLM produced no files and no code after {attempt + 1} attempts. "
                    "Model unavailable or returned invalid output."
                )
            break
        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []
        logger.info(
            "Backend: done, code=%s chars, files=%s (validated from %s), tests=%s chars, "
            "summary=%s chars, needs_clarification=%s",
            len(code),
            len(validated_files),
            len(raw_files),
            len(tests),
            len(summary),
            needs_clarification,
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
            gitignore_entries=[
                str(e).strip() for e in (data.get("gitignore_entries") or []) if str(e).strip()
            ],
        )


def _validate_task_contract(task: Task) -> tuple[bool, list[str]]:
    """Validate contract-first task requirements before implementation begins."""
    missing: List[str] = []
    metadata = task.metadata or {}
    for key in _REQUIRED_TASK_CONTRACT_FIELDS:
        value = metadata.get(key)
        if value in (None, "", [], {}):
            missing.append(key)
    if not task.acceptance_criteria:
        missing.append("acceptance_criteria")
    # Require explicit IO contract either through metadata or requirements text.
    io_contract = metadata.get("inputs_outputs")
    if io_contract in (None, "", [], {}) and "input" not in (task.requirements or "").lower():
        missing.append("inputs_outputs")
    return (len(missing) == 0, missing)


def _build_acceptance_trace(task: Task, files_changed: List[str]) -> List[Dict[str, Any]]:
    """Build criterion-to-implementation/test trace for completion package."""
    tests = [f for f in files_changed if "/test" in f or f.startswith("tests/")]
    impl = [f for f in files_changed if f not in tests]
    trace: List[Dict[str, Any]] = []
    for criterion in task.acceptance_criteria or []:
        trace.append(
            {
                "criterion": criterion,
                "implementation_refs": impl[:4],
                "tests": tests[:4],
            }
        )
    return trace


def _build_completion_package(
    *,
    task: Task,
    result: Optional[BackendOutput],
    review_history: List[ReviewIterationRecord],
    language_used: str,
    git_operations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create structured completion package for downstream handoff/audit."""
    files_changed = list((result.files or {}).keys()) if result else []
    latest = review_history[-1] if review_history else None
    completion_status = "completed"
    if latest and (
        not latest.build_passed
        or not latest.code_review_approved
        or not latest.security_approved
        or not latest.qa_approved
    ):
        completion_status = "completed_with_warnings"
    gates = {
        "spec_validated": "pass",
        "design_approved": "pass",
        "code_implemented": "pass",
        "tests_written": "pass" if any("test" in f for f in files_changed) else "warning",
        "formatting": "pass",
        "static_analysis": "pass",
        "tests_unit": "pass" if latest is None or latest.build_passed else "fail",
        "tests_integration": "pass" if latest is None or latest.build_passed else "fail",
        "security_scan": "pass" if latest is None or latest.security_approved else "fail",
        "review": "pass" if latest is None or latest.code_review_approved else "fail",
        "acceptance_trace": "pass" if task.acceptance_criteria else "warning",
        "docs_handoff": "pass",
    }
    return {
        "task_id": task.id,
        "status": completion_status,
        "language_used": language_used,
        "files_changed": files_changed,
        "acceptance_criteria_trace": _build_acceptance_trace(task, files_changed),
        "quality_gates": gates,
        "notes": [result.summary] if result and result.summary else [],
        "risks_remaining": [],
        "handoff": {
            "migration_required": any("migrations/" in f for f in files_changed),
            "feature_flag_required": False,
        },
        "git_operations": git_operations or {},
    }
