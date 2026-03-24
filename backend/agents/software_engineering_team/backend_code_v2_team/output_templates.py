"""
Template-based output parsing for backend_code_v2_team.

Avoids reliance on JSON so that model output can be parsed reliably across
different providers and models. Uses section-delimited text that can be
parsed with simple string/regex extraction; partial or truncated output
can still yield useful results.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Execution / tool agents: files + summary
# ---------------------------------------------------------------------------

# Format: ## FILE path/to/file.ext ##\n<content>\n## FILE next.ext ## or ## SUMMARY ##
MARKER_FILE = "## FILE "
MARKER_SUMMARY = "## SUMMARY ##"
MARKER_END_SUMMARY = "## END SUMMARY ##"

# Regex: ## FILE <path> ## (path is non-greedy, then optional whitespace/newline)
_RE_FILE_HEADER = re.compile(r"## FILE (.+?) ##\s*", re.DOTALL)
# Match start of any section line (content ends before next "## XXX ##")
_RE_NEXT_SECTION = re.compile(r"\n## [A-Z_]+ ##", re.DOTALL)


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """Extract section between start_marker and end_marker (or end of text)."""
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _normalize_file_path(path: str) -> str:
    """Strip redundant backend/ prefix from path.

    The backend team operates within the backend directory, so LLM output
    paths like 'backend/src/...' should be normalized to 'src/...'.
    """
    prefixes_to_strip = ("backend/", "./backend/")
    for prefix in prefixes_to_strip:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def parse_files_and_summary_template(text: str) -> Dict[str, Any]:
    """
    Parse template output that contains file blocks and an optional summary.

    File blocks: each starts with "## FILE <path> ##" and content runs until
    the next "## FILE " or "## SUMMARY ##". Summary is taken from
    "## SUMMARY ##" ... "## END SUMMARY ##" or to end of text.

    Returns dict with keys: "files" (path -> content), "summary" (str).
    """
    files: Dict[str, str] = {}
    summary = ""

    # Find all ## FILE path ## positions; content runs until next section (any ## XXX ##)
    for m in _RE_FILE_HEADER.finditer(text):
        path = m.group(1).strip()
        content_start = m.end()
        next_section = _RE_NEXT_SECTION.search(text, content_start)
        content_end = next_section.start() if next_section else len(text)
        content = text[content_start:content_end].rstrip()
        if path:
            files[_normalize_file_path(path)] = content

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    if summary_section:
        summary = summary_section.split("\n")[0].strip()[:2000]
    elif MARKER_SUMMARY in text:
        idx = text.find(MARKER_SUMMARY) + len(MARKER_SUMMARY)
        rest = text[idx:].strip()
        if MARKER_END_SUMMARY in rest:
            summary = rest.split(MARKER_END_SUMMARY)[0].strip().split("\n")[0].strip()[:2000]
        else:
            summary = rest.split("\n")[0].strip()[:2000]

    return {"files": files, "summary": summary}


def parse_files_with_validation(text: str) -> Tuple[Dict[str, Any], List[str], Dict[str, str]]:
    """Parse files from template output.

    Note: Truncation detection is now handled at the LLM client level via
    finish_reason checks. This function simply parses the output without
    additional validation.

    Returns:
        Tuple of:
        - parsed: Dict with "files" and "summary" keys
        - truncated_paths: Always empty list (deprecated)
        - validation_errors: Always empty dict (deprecated)
    """
    parsed = parse_files_and_summary_template(text)
    return parsed, [], {}


# ---------------------------------------------------------------------------
# Planning: microtasks + language + summary
# ---------------------------------------------------------------------------

MARKER_MICROTASKS = "## MICROTASKS ##"
MARKER_END_MICROTASKS = "## END MICROTASKS ##"
MARKER_LANGUAGE = "## LANGUAGE ##"
MARKER_END_LANGUAGE = "## END LANGUAGE ##"
MARKER_PLAN_SUMMARY = "## SUMMARY ##"
MARKER_END_PLAN_SUMMARY = "## END SUMMARY ##"
BLOCK_SEP = "---"


def _parse_microtask_block(block: str) -> Dict[str, Any] | None:
    """Parse a single microtask block (key: value lines)."""
    out: Dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key == "depends_on" and value:
            out[key] = [v.strip() for v in value.split("|") if v.strip()]
        elif value:
            out[key] = value
    if out.get("id"):
        if "depends_on" not in out:
            out["depends_on"] = []
        return out
    return None


def parse_planning_template(text: str) -> Dict[str, Any]:
    """
    Parse planning-phase template into microtasks, language, and summary.

    Format:
      ## MICROTASKS ##
      ---
      id: mt-xxx
      title: ...
      description: ...
      tool_agent: data_engineering
      depends_on: mt-yyy|mt-zzz
      ---
      ## END MICROTASKS ##
      ## LANGUAGE ##
      python
      ## END LANGUAGE ##
      ## SUMMARY ##
      ...
      ## END SUMMARY ##

    Returns dict with keys: "microtasks" (list of dicts), "language" (str), "summary" (str).
    """
    microtasks: List[Dict[str, Any]] = []
    language = "python"
    summary = ""

    mt_section = _section(text, MARKER_MICROTASKS, MARKER_END_MICROTASKS)
    if not mt_section and MARKER_MICROTASKS in text:
        idx = text.find(MARKER_MICROTASKS) + len(MARKER_MICROTASKS)
        mt_section = text[idx:].strip()
        if MARKER_LANGUAGE in mt_section:
            mt_section = mt_section.split(MARKER_LANGUAGE)[0].strip()
    for part in mt_section.split(BLOCK_SEP):
        part = part.strip()
        if not part:
            continue
        obj = _parse_microtask_block(part)
        if obj:
            microtasks.append(obj)

    lang_section = _section(text, MARKER_LANGUAGE, MARKER_END_LANGUAGE)
    if lang_section:
        language = lang_section.strip().split("\n")[0].strip().lower() or "python"
        if language not in ("python", "java"):
            language = "python"

    summary_section = _section(text, MARKER_PLAN_SUMMARY, MARKER_END_PLAN_SUMMARY)
    if summary_section:
        summary = summary_section.strip().split("\n")[0].strip()[:1000]
    elif MARKER_PLAN_SUMMARY in text:
        idx = text.find(MARKER_PLAN_SUMMARY) + len(MARKER_PLAN_SUMMARY)
        summary = text[idx:].strip().split("\n")[0].strip()[:1000]

    return {"microtasks": microtasks, "language": language, "summary": summary}


# ---------------------------------------------------------------------------
# Review: passed + issues + summary
# ---------------------------------------------------------------------------

MARKER_PASSED = "## PASSED ##"
MARKER_END_PASSED = "## END PASSED ##"
MARKER_ISSUES = "## ISSUES ##"
MARKER_END_ISSUES = "## END ISSUES ##"
MARKER_REVIEW_SUMMARY = "## SUMMARY ##"
MARKER_END_REVIEW_SUMMARY = "## END SUMMARY ##"


def _parse_issue_block(block: str) -> Dict[str, Any] | None:
    """Parse a single issue block."""
    out: Dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        out[key] = value.strip()
    if out.get("description") or out.get("source"):
        out.setdefault("source", "code_review")
        out.setdefault("severity", "medium")
        out.setdefault("file_path", "")
        out.setdefault("recommendation", "")
        return out
    return None


def parse_review_template(text: str) -> Dict[str, Any]:
    """
    Parse review-phase template into passed, issues, and summary.

    Returns dict with keys: "passed" (bool), "issues" (list of dicts), "summary" (str).
    """
    passed = True
    issues: List[Dict[str, Any]] = []
    summary = ""

    passed_section = _section(text, MARKER_PASSED, MARKER_END_PASSED)
    if passed_section:
        first = passed_section.strip().split("\n")[0].strip().lower()
        passed = first in ("true", "yes", "1", "pass")

    issues_section = _section(text, MARKER_ISSUES, MARKER_END_ISSUES)
    if not issues_section and MARKER_ISSUES in text:
        idx = text.find(MARKER_ISSUES) + len(MARKER_ISSUES)
        issues_section = text[idx:].strip()
        if MARKER_REVIEW_SUMMARY in issues_section:
            issues_section = issues_section.split(MARKER_REVIEW_SUMMARY)[0].strip()
    for part in issues_section.split(BLOCK_SEP):
        part = part.strip()
        if not part:
            continue
        obj = _parse_issue_block(part)
        if obj:
            issues.append(obj)

    summary_section = _section(text, MARKER_REVIEW_SUMMARY, MARKER_END_REVIEW_SUMMARY)
    if summary_section:
        summary = summary_section.strip().split("\n")[0].strip()[:1000]
    elif MARKER_REVIEW_SUMMARY in text:
        idx = text.find(MARKER_REVIEW_SUMMARY) + len(MARKER_REVIEW_SUMMARY)
        summary = text[idx:].strip().split("\n")[0].strip()[:1000]

    return {"passed": passed, "issues": issues, "summary": summary}


# ---------------------------------------------------------------------------
# Problem-solving: files + fixes_applied + resolved + summary
# ---------------------------------------------------------------------------

MARKER_FIXES = "## FIXES_APPLIED ##"
MARKER_END_FIXES = "## END FIXES_APPLIED ##"
MARKER_RESOLVED = "## RESOLVED ##"
MARKER_END_RESOLVED = "## END RESOLVED ##"
MARKER_PS_SUMMARY = "## SUMMARY ##"
MARKER_END_PS_SUMMARY = "## END SUMMARY ##"


def _parse_fix_block(block: str) -> Dict[str, Any] | None:
    """Parse a single fix block (issue, fix)."""
    out: Dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        out[key] = value.strip()
    if out.get("issue") or out.get("fix"):
        return out
    return None


def parse_problem_solving_template(text: str) -> Dict[str, Any]:
    """
    Parse problem-solving template: files (same format as execution) plus
    fixes_applied, resolved, and summary.

    Returns dict with keys: "files", "fixes_applied", "resolved" (bool), "summary".
    """
    base = parse_files_and_summary_template(text)
    files = base["files"]
    summary = base["summary"]

    fixes_applied: List[Dict[str, Any]] = []
    fixes_section = _section(text, MARKER_FIXES, MARKER_END_FIXES)
    if not fixes_section and MARKER_FIXES in text:
        idx = text.find(MARKER_FIXES) + len(MARKER_FIXES)
        fixes_section = text[idx:].strip()
        if MARKER_RESOLVED in fixes_section:
            fixes_section = fixes_section.split(MARKER_RESOLVED)[0].strip()
    for part in fixes_section.split(BLOCK_SEP):
        part = part.strip()
        if not part:
            continue
        obj = _parse_fix_block(part)
        if obj:
            fixes_applied.append(obj)

    resolved = True
    resolved_section = _section(text, MARKER_RESOLVED, MARKER_END_RESOLVED)
    if resolved_section:
        first = resolved_section.strip().split("\n")[0].strip().lower()
        resolved = first in ("true", "yes", "1")

    summary_sec = _section(text, MARKER_PS_SUMMARY, MARKER_END_PS_SUMMARY)
    if summary_sec:
        summary = summary_sec.strip().split("\n")[0].strip()[:1000]

    return {
        "files": files,
        "fixes_applied": fixes_applied,
        "summary": summary,
        "resolved": resolved,
    }


def parse_problem_solving_single_issue_template(text: str) -> Dict[str, Any]:
    """
    Parse single-issue problem-solving output: ROOT_CAUSE, FILE blocks, RESOLVED, SUMMARY.

    Returns dict with keys: "files", "root_cause", "resolved" (bool), "summary".
    """
    base = parse_files_and_summary_template(text)
    files = base["files"]
    summary = base["summary"]

    root_cause = _section(text, "## ROOT_CAUSE ##", "## END ROOT_CAUSE ##")
    if root_cause:
        root_cause = root_cause.strip().split("\n")[0].strip()[:500]

    resolved = True
    resolved_section = _section(text, MARKER_RESOLVED, MARKER_END_RESOLVED)
    if resolved_section:
        first = resolved_section.strip().split("\n")[0].strip().lower()
        resolved = first in ("true", "yes", "1")

    summary_sec = _section(text, MARKER_PS_SUMMARY, MARKER_END_PS_SUMMARY)
    if summary_sec:
        summary = summary_sec.strip().split("\n")[0].strip()[:1000]

    return {
        "files": files,
        "root_cause": root_cause or "",
        "resolved": resolved,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Batch fix parsing: files + issues_addressed + summary
# ---------------------------------------------------------------------------

MARKER_ISSUES_ADDRESSED = "## ISSUES_ADDRESSED ##"
MARKER_END_ISSUES_ADDRESSED = "## END ISSUES_ADDRESSED ##"


def _parse_issue_addressed_block(block: str) -> Dict[str, Any] | None:
    """Parse a single issue_addressed block (issue_index, description)."""
    out: Dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        out[key] = value.strip()
    if out.get("issue_index") or out.get("description"):
        return out
    return None


def parse_batch_fix_template(text: str) -> Dict[str, Any]:
    """
    Parse batch fix output: FILE blocks, ISSUES_ADDRESSED, and SUMMARY.

    Returns dict with keys: "files", "issues_addressed" (list), "summary".
    """
    base = parse_files_and_summary_template(text)
    files = base["files"]
    summary = base["summary"]

    issues_addressed: List[Dict[str, Any]] = []
    issues_section = _section(text, MARKER_ISSUES_ADDRESSED, MARKER_END_ISSUES_ADDRESSED)
    if not issues_section and MARKER_ISSUES_ADDRESSED in text:
        idx = text.find(MARKER_ISSUES_ADDRESSED) + len(MARKER_ISSUES_ADDRESSED)
        issues_section = text[idx:].strip()
        if MARKER_SUMMARY in issues_section:
            issues_section = issues_section.split(MARKER_SUMMARY)[0].strip()
    for part in issues_section.split(BLOCK_SEP):
        part = part.strip()
        if not part:
            continue
        obj = _parse_issue_addressed_block(part)
        if obj:
            issues_addressed.append(obj)

    summary_sec = _section(text, MARKER_PS_SUMMARY, MARKER_END_PS_SUMMARY)
    if summary_sec:
        summary = summary_sec.strip()[:2000]

    return {
        "files": files,
        "issues_addressed": issues_addressed,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Documentation self-review parsing: quality_score, improvements, files, summary
# ---------------------------------------------------------------------------

MARKER_QUALITY_SCORE = "## QUALITY_SCORE ##"
MARKER_END_QUALITY_SCORE = "## END QUALITY_SCORE ##"
MARKER_IMPROVEMENTS = "## IMPROVEMENTS ##"
MARKER_END_IMPROVEMENTS = "## END IMPROVEMENTS ##"


def parse_documentation_self_review_template(text: str) -> Dict[str, Any]:
    """
    Parse documentation self-review output: QUALITY_SCORE, IMPROVEMENTS, FILE blocks, SUMMARY.

    Returns dict with keys: "quality_score" (float), "improvements" (list), "files", "summary".
    """
    base = parse_files_and_summary_template(text)
    files = base["files"]
    summary = base["summary"]

    quality_score = 0.5
    score_section = _section(text, MARKER_QUALITY_SCORE, MARKER_END_QUALITY_SCORE)
    if score_section:
        try:
            quality_score = float(score_section.strip().split("\n")[0].strip())
            quality_score = max(0.0, min(1.0, quality_score))
        except ValueError:
            pass

    improvements: List[str] = []
    imp_section = _section(text, MARKER_IMPROVEMENTS, MARKER_END_IMPROVEMENTS)
    if imp_section:
        for line in imp_section.strip().splitlines():
            line = line.strip()
            if line.startswith("- "):
                improvements.append(line[2:].strip())
            elif line:
                improvements.append(line)

    return {
        "quality_score": quality_score,
        "improvements": improvements,
        "files": files,
        "summary": summary,
    }
