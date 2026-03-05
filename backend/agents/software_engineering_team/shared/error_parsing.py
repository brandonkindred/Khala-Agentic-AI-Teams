"""
Structured error parsing for build/test failures.

Categorizes pytest, ng build, and SQLAlchemy errors into structured objects
with error type, file, line, probable cause, and remediation playbooks.
Used by agents to understand failures and apply targeted fixes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class FailureClass(str, Enum):
    """Standard failure classes for observability and agent tuning."""

    EMPTY_COMPLETION = "empty_completion"
    JSON_PARSE_FAILURE = "json_parse_failure"
    IMPORT_ERROR = "import_error"
    SQL_NO_SUCH_TABLE = "sql_no_such_table"
    FRONTEND_UNRESOLVED_IMPORT = "frontend_unresolved_import"
    PYTEST_ASSERTION = "pytest_assertion"
    PYTEST_COLLECTION = "pytest_collection"
    NG_BUILD_ERROR = "ng_build_error"
    ENVIRONMENT = "environment"
    DOCKER_BUILD_ERROR = "docker_build_error"
    DOCKERFILE_SYNTAX = "dockerfile_syntax"
    YAML_PARSE_ERROR = "yaml_parse_error"
    GITHUB_ACTIONS_ERROR = "github_actions_error"
    TERRAFORM_ERROR = "terraform_error"
    UNKNOWN = "unknown"


@dataclass
class ParsedFailure:
    """Structured representation of a build/test failure."""

    failure_class: FailureClass
    file_path: Optional[str] = None
    line: Optional[int] = None
    message: str = ""
    raw_excerpt: str = ""
    suggestion: str = ""
    playbook_hint: str = ""
    # When present, list of "file::test_name" or "file" for assertion failures (all failing tests).
    failing_tests: Optional[List[str]] = None


# Playbook hints for common failure types (fed to agents)
PLAYBOOK_IMPORT_ERROR = (
    "Add the missing export to the module. For 'cannot import name X from Y', "
    "ensure Y defines and exports X (e.g. in __init__.py or the module itself)."
)
PLAYBOOK_SQL_NO_SUCH_TABLE = (
    "Ensure all SQLAlchemy models are imported into the app's metadata and that "
    "Base.metadata.create_all(bind=engine) is called before any queries. "
    "In tests, use a fixture or conftest that creates tables before running."
)
PLAYBOOK_FRONTEND_UNRESOLVED = (
    "Create the missing component file or fix the import path. For Angular routes, "
    "ensure the path in loadComponent/lazy load matches an existing file that exports "
    "the expected component class."
)
PLAYBOOK_PYTEST_ASSERTION = (
    "Fix the failing assertion. Check the test expectations and ensure the implementation "
    "matches (e.g. status codes, response structure, database state)."
)
PLAYBOOK_PYTEST_COLLECTION = (
    "Fix import or syntax errors that prevent pytest from collecting tests. "
    "Often caused by missing exports (e.g. Base from app.database) or circular imports."
)
PLAYBOOK_401_UNAUTHORIZED = (
    "401 Unauthorized means the request was not authenticated. Fix by ensuring the test "
    "client sends the required auth (e.g. Authorization header or token) for protected "
    "endpoints. Do not disable or bypass auth in the application."
)
PLAYBOOK_403_FORBIDDEN = (
    "403 Forbidden means the request was authenticated but not allowed. Check permissions "
    "or scope for the authenticated principal; fix the test or the endpoint as appropriate."
)
PLAYBOOK_404_NOT_FOUND = (
    "404 Not Found means the resource or route was not found. Ensure the URL and method "
    "are correct; fix the test expectations or the route/resource as appropriate."
)
INTERPRETATION_401 = (
    "The test expected a successful response (e.g. 200) but got 401. The endpoint requires "
    "authentication; the test request is missing or has invalid auth. Fix the test to send "
    "the required auth header or use an authenticated test client."
)

# DevOps playbooks
PLAYBOOK_DOCKER_BUILD = (
    "Fix the Docker build: ensure all COPY paths exist (e.g. requirements.txt, package.json), "
    "use valid base image tags (e.g. python:3.11-slim), and fix any RUN command failures. "
    "For 'file not found', verify the file exists in the build context and path is correct."
)
PLAYBOOK_YAML_SYNTAX = (
    "Fix YAML syntax: check indentation (use spaces, not tabs), ensure colons have spaces after "
    "them, and that strings with special characters are quoted. Validate with a YAML linter."
)
PLAYBOOK_GHA_WORKFLOW = (
    "Fix GitHub Actions workflow: use valid action versions (e.g. actions/checkout@v4), ensure "
    "'jobs' has at least one job with 'runs-on' and 'steps'. Check that 'on' trigger is valid."
)


def parse_pytest_failure(stdout: str, stderr: str) -> List[ParsedFailure]:
    """
    Parse pytest output into structured failures.

    Handles:
    - ImportError (cannot import name X from Y)
    - sqlite3.OperationalError (no such table: X)
    - AssertionError / test failures
    """
    text = (stdout or "") + "\n" + (stderr or "")
    failures: List[ParsedFailure] = []

    # ImportError: cannot import name 'Base' from 'app.database'
    import_match = re.search(
        r"ImportError(?:\s+while importing[^:]*)?:\s*cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
        text,
        re.IGNORECASE,
    )
    if import_match:
        symbol, module = import_match.group(1), import_match.group(2)
        file_match = re.search(r"tests/test_[^\s]+\.py", text)
        file_path = file_match.group(0) if file_match else None
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.IMPORT_ERROR,
                file_path=file_path,
                message=f"cannot import name '{symbol}' from '{module}'",
                raw_excerpt=text[import_match.start() : import_match.end() + 200],
                suggestion=f"Ensure {module} exports {symbol}. Add 'from .database import Base' or define Base in app/database.py.",
                playbook_hint=PLAYBOOK_IMPORT_ERROR,
            )
        )
        return failures  # Often the root cause; return early

    # no such table: api_tokens (from sqlite3.OperationalError or similar)
    sql_match = re.search(
        r"no such table:\s*([a-zA-Z_]+)",
        text,
        re.IGNORECASE,
    )
    if sql_match:
        table = sql_match.group(1)
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.SQL_NO_SUCH_TABLE,
                message=f"no such table: {table}",
                raw_excerpt=text[sql_match.start() : sql_match.end() + 300],
                suggestion=f"Create the {table} table: ensure the model is in Base.metadata and call Base.metadata.create_all(bind=engine) before queries.",
                playbook_hint=PLAYBOOK_SQL_NO_SUCH_TABLE,
            )
        )
        return failures

    # Pytest assertion failure - extract all FAILED lines, traceback file, assertion details
    if "= FAILURES =" in text or "assert " in text:
        raw_excerpt = text[-2500:] if len(text) > 2500 else text
        assertion_line: Optional[str] = None
        expected_got: Optional[str] = None
        got_status: Optional[int] = None  # detected response code (e.g. 401) when assertion is status_code

        # Collect ALL FAILED path::test_name (dedupe by (file, test_name))
        failed_matches = re.findall(
            r"FAILED\s+([a-zA-Z0-9_/.-]+test_[a-zA-Z0-9_]+\.py)(?:::(test_[a-zA-Z0-9_]+))?",
            text,
        )
        seen: set = set()
        failing_list: List[Tuple[str, Optional[str]]] = []
        for m in failed_matches:
            if isinstance(m, tuple):
                file_path_part = (m[0] or "").strip()
                test_name_part = (m[1] or "").strip() or None
            else:
                file_path_part = str(m).strip()
                test_name_part = None
            key = (file_path_part, test_name_part or "")
            if key not in seen:
                seen.add(key)
                failing_list.append((file_path_part, test_name_part))
        failing_tests_display: List[str] = [
            f"{f}::{t}" if t else f for (f, t) in failing_list
        ]
        first_file = failing_list[0][0] if failing_list else None
        first_test = failing_list[0][1] if failing_list and failing_list[0][1] else None

        # Traceback file:line (e.g. tests/test_task_endpoints.py:277) in raw excerpt
        traceback_file: Optional[str] = None
        tb_match = re.search(r"(tests/test_[a-zA-Z0-9_]+\.py):\d+", raw_excerpt)
        if tb_match:
            traceback_file = tb_match.group(1)
        # Prefer traceback file when it appears in our failing list
        test_file: Optional[str] = None
        if traceback_file and any(f == traceback_file for (f, _) in failing_list):
            test_file = traceback_file
        else:
            test_file = first_file

        # AssertionError: assert 200 == 401  or  E       AssertionError: assert 200 == 401
        assert_err_match = re.search(
            r"AssertionError:\s*(assert\s+[^\n]+)",
            text,
        )
        if assert_err_match:
            assertion_line = assert_err_match.group(1).strip()[:200]

        # E         +200  /  E         -401  (actual vs expected) -> got 401
        expected_match = re.search(
            r"E\s+[+-]\s*(\d+)\s*\n\s*E\s+[+-]\s*(\d+)",
            text,
        )
        if expected_match:
            v1, v2 = expected_match.group(1), expected_match.group(2)
            # Pytest: E +N is expected, E -N is actual (got). So "expected v1, got v2".
            expected_got = f"expected {v1}, got {v2}" if v1 != v2 else f"values {v1} vs {v2}"
            # When assertion is status_code, the actual response code is "got" (v2)
            if assertion_line and "status_code" in assertion_line:
                try:
                    got_status = int(v2)
                except ValueError:
                    pass

        # Status-code-specific playbook (401, 403, 404)
        playbook_hint = PLAYBOOK_PYTEST_ASSERTION
        if got_status == 401:
            playbook_hint = playbook_hint + " " + PLAYBOOK_401_UNAUTHORIZED
        elif got_status == 403:
            playbook_hint = playbook_hint + " " + PLAYBOOK_403_FORBIDDEN
        elif got_status == 404:
            playbook_hint = playbook_hint + " " + PLAYBOOK_404_NOT_FOUND
        elif assertion_line and "401" in assertion_line and ("status_code" in assertion_line or expected_got):
            playbook_hint = playbook_hint + " " + PLAYBOOK_401_UNAUTHORIZED
        elif assertion_line and "403" in assertion_line:
            playbook_hint = playbook_hint + " " + PLAYBOOK_403_FORBIDDEN
        elif assertion_line and "404" in assertion_line:
            playbook_hint = playbook_hint + " " + PLAYBOOK_404_NOT_FOUND

        # Message: single vs multiple failures
        if len(failing_list) > 1:
            msg_parts = [f"{len(failing_list)} tests failed: {', '.join(failing_tests_display[:10])}"]
            if len(failing_tests_display) > 10:
                msg_parts[0] += f" (+{len(failing_tests_display) - 10} more)"
            if assertion_line:
                msg_parts.append(assertion_line)
            if expected_got:
                msg_parts.append(f"({expected_got})")
            message = " ".join(msg_parts)
        else:
            msg_parts = []
            if first_test:
                msg_parts.append(f"{first_test} failed")
            if assertion_line:
                msg_parts.append(assertion_line)
            if expected_got:
                msg_parts.append(f"({expected_got})")
            message = (
                " ".join(msg_parts)
                if msg_parts
                else "One or more tests failed (assertion or status code mismatch)"
            )

        # Suggestion: single vs multiple
        if len(failing_list) > 1:
            suggestion = (
                f"Fix the following failing tests: {', '.join(failing_tests_display[:15])}. "
                "Ensure each test's requests satisfy the assertion (e.g. if assertion expects 200, "
                "provide valid auth when the endpoint requires it)."
            )
            if len(failing_tests_display) > 15:
                suggestion += f" ({len(failing_tests_display) - 15} more tests failed)"
        else:
            suggestion_parts = []
            if test_file:
                suggestion_parts.append(f"Fix {test_file}")
            if first_test:
                suggestion_parts.append(f"({first_test})")
            if assertion_line:
                suggestion_parts.append(f"to satisfy: {assertion_line}")
            suggestion = (
                ". ".join(suggestion_parts) + "."
                if suggestion_parts
                else "Review the failing test(s) and fix the implementation to match expectations."
            )

        failures.append(
            ParsedFailure(
                failure_class=FailureClass.PYTEST_ASSERTION,
                file_path=test_file,
                message=message,
                raw_excerpt=raw_excerpt,
                suggestion=suggestion,
                playbook_hint=playbook_hint,
                failing_tests=failing_tests_display if failing_tests_display else None,
            )
        )

    # Collection error (not ImportError)
    if "ERROR collecting" in text and FailureClass.IMPORT_ERROR not in [f.failure_class for f in failures]:
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.PYTEST_COLLECTION,
                message="pytest could not collect tests (syntax or import error)",
                raw_excerpt=text[-2000:] if len(text) > 2000 else text,
                suggestion="Fix the error that prevents test collection (imports, syntax).",
                playbook_hint=PLAYBOOK_PYTEST_COLLECTION,
            )
        )

    if not failures:
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.UNKNOWN,
                message="Unrecognized pytest failure",
                raw_excerpt=text[-2000:] if len(text) > 2000 else text,
            )
        )
    return failures


def parse_ng_build_failure(stdout: str, stderr: str) -> List[ParsedFailure]:
    """
    Parse Angular/ng build output into structured failures.

    Handles:
    - Could not resolve "./components/foo/foo.component"
    """
    text = (stdout or "") + "\n" + (stderr or "")
    failures: List[ParsedFailure] = []

    # Could not resolve "./components/task-form/task-form.component"
    resolve_match = re.search(
        r"Could not resolve ['\"]([^'\"]+)['\"]",
        text,
        re.IGNORECASE,
    )
    if resolve_match:
        path = resolve_match.group(1)
        # Try to find file:line from context (e.g. src/app/app.routes.ts:10:53)
        file_match = re.search(r"(src/[^\s:]+\.ts):(\d+):(\d+)", text)
        file_path = file_match.group(1) if file_match else None
        line = int(file_match.group(2)) if file_match and file_match.group(2) else None
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.FRONTEND_UNRESOLVED_IMPORT,
                file_path=file_path,
                line=line,
                message=f"Could not resolve '{path}'",
                raw_excerpt=text[resolve_match.start() : resolve_match.end() + 400],
                suggestion=f"Create the missing file for '{path}' or fix the import path in the route/component.",
                playbook_hint=PLAYBOOK_FRONTEND_UNRESOLVED,
            )
        )
        return failures

    # Generic ng build error
    if "ERROR" in text or "error" in text.lower():
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.NG_BUILD_ERROR,
                message="Angular build failed",
                raw_excerpt=text[-3000:] if len(text) > 3000 else text,
                suggestion="Fix the compilation errors reported above.",
                playbook_hint="Address the specific TypeScript/template errors shown in the output.",
            )
        )

    if not failures:
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.UNKNOWN,
                message="Unrecognized ng build failure",
                raw_excerpt=text[-2000:] if len(text) > 2000 else text,
            )
        )
    return failures


def parse_devops_failure(build_errors: str) -> List[ParsedFailure]:
    """
    Parse DevOps build/verification errors into structured failures.

    Handles:
    - YAML parse errors (pipeline, docker-compose)
    - Docker build errors (COPY failed, invalid base image, RUN failures)
    - GitHub Actions workflow issues
    """
    text = build_errors or ""
    failures: List[ParsedFailure] = []

    # YAML parse error (from orchestrator or yaml.safe_load)
    yaml_match = re.search(
        r"YAML parse error (?:in\s+)?([^\s:]+)?\s*:?\s*(.+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    yamlerror_in_text = "YAMLError" in text or "yaml.YAMLError" in text
    if yaml_match or ("yaml" in text.lower() and "parse" in text.lower()) or yamlerror_in_text:
        file_path = yaml_match.group(1).strip() if yaml_match and yaml_match.group(1) else None
        msg = yaml_match.group(2).strip()[:300] if yaml_match and yaml_match.group(2) else "YAML parse error"
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.YAML_PARSE_ERROR,
                file_path=file_path,
                message=msg,
                raw_excerpt=text[:2000],
                suggestion="Fix YAML syntax: indentation, colons, and quoting.",
                playbook_hint=PLAYBOOK_YAML_SYNTAX,
            )
        )
        return failures

    # Docker build: COPY failed, file not found
    copy_match = re.search(
        r"COPY failed: ([^:]+): ([^\n]+)",
        text,
        re.IGNORECASE,
    )
    if copy_match:
        src, err = copy_match.group(1).strip(), copy_match.group(2).strip()
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.DOCKER_BUILD_ERROR,
                message=f"COPY failed: {src} - {err}",
                raw_excerpt=text[:2000],
                suggestion=f"Ensure '{src}' exists in the build context. Check path and .dockerignore.",
                playbook_hint=PLAYBOOK_DOCKER_BUILD,
            )
        )
        return failures

    # Docker: failed to solve, invalid reference
    solve_match = re.search(
        r"(?:failed to solve|error|invalid reference)[:\s]+(.{1,200})",
        text,
        re.IGNORECASE,
    )
    if solve_match:
        msg = solve_match.group(1).strip()
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.DOCKER_BUILD_ERROR,
                message=f"Docker build failed: {msg}",
                raw_excerpt=text[:2000],
                suggestion="Fix the Dockerfile: valid base image, correct paths, working RUN commands.",
                playbook_hint=PLAYBOOK_DOCKER_BUILD,
            )
        )
        return failures

    # Docker: RUN command failed
    run_match = re.search(
        r"(?:The command|RUN)[^`]*`[^`]+` (?:returned|failed)[^.]*\.?\s*(.+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if run_match:
        detail = run_match.group(1).strip()[:200] if run_match.group(1) else ""
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.DOCKER_BUILD_ERROR,
                message=f"RUN command failed: {detail}",
                raw_excerpt=text[:2000],
                suggestion="Fix the failing RUN step: check package names, install commands, and paths.",
                playbook_hint=PLAYBOOK_DOCKER_BUILD,
            )
        )
        return failures

    # Generic Docker build error
    if "docker" in text.lower() and ("error" in text.lower() or "failed" in text.lower()):
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.DOCKER_BUILD_ERROR,
                message="Docker build failed",
                raw_excerpt=text[:2000],
                suggestion="Fix the Dockerfile and build context. Check COPY paths and RUN commands.",
                playbook_hint=PLAYBOOK_DOCKER_BUILD,
            )
        )
        return failures

    if not failures:
        failures.append(
            ParsedFailure(
                failure_class=FailureClass.UNKNOWN,
                message="Unrecognized DevOps failure",
                raw_excerpt=text[:2000] if text else "",
            )
        )
    return failures


def parse_command_failure(
    command_kind: str,
    stdout: str,
    stderr: str,
) -> List[ParsedFailure]:
    """
    Parse command output based on command kind.

    command_kind: "pytest" | "ng_build" | "py_compile" | "devops"
    """
    if command_kind == "pytest":
        return parse_pytest_failure(stdout, stderr)
    if command_kind in ("ng_build", "ng"):
        return parse_ng_build_failure(stdout, stderr)
    if command_kind == "devops":
        return parse_devops_failure((stdout or "") + "\n" + (stderr or ""))
    return [
        ParsedFailure(
            failure_class=FailureClass.UNKNOWN,
            message="Build/test failure",
            raw_excerpt=(stdout or "") + "\n" + (stderr or ""),
        )
    ]


def build_agent_feedback(failures: List[ParsedFailure], max_chars: int = 2500) -> str:
    """
    Build a concise feedback string for agents from parsed failures.

    Includes the primary failure's suggestion and playbook hint, plus truncated raw excerpt.
    For PYTEST_ASSERTION with failing_tests list, adds "Failing tests:" section.
    When 401 playbook is present, adds "Interpretation:" before Playbook.
    """
    if not failures:
        return ""
    primary = failures[0]
    parts = [
        f"[{primary.failure_class.value}] {primary.message}",
        "",
        "Suggestion:",
        primary.suggestion or "Fix the error.",
    ]
    # Failing tests: list each file::test_name when we have the list (assertion failures)
    if (
        primary.failure_class == FailureClass.PYTEST_ASSERTION
        and primary.failing_tests
    ):
        parts.extend(["", "Failing tests:"])
        for ft in primary.failing_tests[:20]:
            parts.append(f"  - {ft}")
        if len(primary.failing_tests) > 20:
            parts.append(f"  ... and {len(primary.failing_tests) - 20} more")
    # Interpretation: when 401 (or 403/404) playbook is present
    if primary.playbook_hint and PLAYBOOK_401_UNAUTHORIZED in primary.playbook_hint:
        parts.extend(["", "Interpretation:", INTERPRETATION_401])
    elif primary.playbook_hint and PLAYBOOK_403_FORBIDDEN in primary.playbook_hint:
        parts.extend(["", "Interpretation:", PLAYBOOK_403_FORBIDDEN])
    elif primary.playbook_hint and PLAYBOOK_404_NOT_FOUND in primary.playbook_hint:
        parts.extend(["", "Interpretation:", PLAYBOOK_404_NOT_FOUND])
    # Unresolved import: add verb-prefix path fix hint
    if primary.failure_class == FailureClass.FRONTEND_UNRESOLVED_IMPORT:
        parts.extend([
            "",
            "Path fix: If the missing path contains a folder name like create-task or add-task, "
            "create the component under an allowed name (e.g. task-form) and update the import "
            "in the route or module to that path.",
        ])
    if primary.playbook_hint:
        parts.extend(["", "Playbook:", primary.playbook_hint])
    if primary.raw_excerpt:
        excerpt = primary.raw_excerpt
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars] + "\n... [truncated]"
        parts.extend(["", "Raw output:", excerpt])
    return "\n".join(parts)


def get_failure_class_tag(failure_class: FailureClass) -> str:
    """Return a log-friendly tag for observability."""
    return f"failure_class={failure_class.value}"


def log_failure(failure_class: FailureClass, message: str, **kwargs: object) -> None:
    """
    Log a failure with standardized failure_class tag for observability.

    Use this so failure classes can be aggregated and tuned over time.
    """
    logger.warning(
        "%s | %s",
        get_failure_class_tag(failure_class),
        message,
        extra={"failure_class": failure_class.value, **kwargs},
    )
