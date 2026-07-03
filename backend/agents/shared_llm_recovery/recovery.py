"""
Utilities for parsing LLM responses when structured JSON parsing fails.

When the LLM returns raw content (e.g. {"content": "..."}), extract file paths
and bodies from markdown code blocks so agents can still produce files.
Also extracts task assignments when Tech Lead / Task Generator return raw content.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# Bound on how many candidate objects one salvage call will attempt to parse.
# Typical responses contain a handful; the cap keeps adversarial output (e.g.
# thousands of balanced "{}" pairs) from turning salvage into a CPU sink.
_MAX_PARSE_ATTEMPTS = 64


def _strip_wrappers(content: str) -> str:
    """Drop think/reasoning blocks and unwrap ``<json>...</json>`` tags.

    Preconditions: ``content`` is a str.
    Postconditions: returns the stripped text (may be empty); never raises.
    """
    stripped = content.strip()
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"<thinking>.*?</thinking>", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"<reasoning>.*?</reasoning>", "", stripped, flags=re.DOTALL)
    stripped = stripped.strip()
    json_tag_match = re.search(r"<json>\s*([\s\S]*?)\s*</json>", stripped)
    if json_tag_match:
        stripped = json_tag_match.group(1).strip()
    return stripped


def _balanced_object_spans(text: str) -> Tuple[List[Tuple[int, int]], int]:
    """Locate top-level balanced ``{...}`` spans in one linear, string-aware pass.

    Braces inside JSON string literals must not affect nesting depth — task
    descriptions and review reasons routinely carry code snippets with unbalanced
    ``{``/``}`` — and an escaped quote must not close its string. Quotes are only
    treated as string delimiters while inside an open object, so prose quotation
    marks before any ``{`` cannot derail the scan.

    Preconditions: ``text`` is a str.
    Postconditions: returns ``(spans, first_unclosed)`` where ``spans`` lists
    ``(start, end_exclusive)`` for every balanced object not nested inside
    another balanced object, in document order, and ``first_unclosed`` is the
    index of the first ``{`` that never closed (-1 when all opens closed).
    Single pass — O(len(text)); never raises.
    """
    spans: List[Tuple[int, int]] = []
    stack: List[int] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            if stack:
                in_string = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, i + 1))
    first_unclosed = stack[0] if stack else -1
    return spans, first_unclosed


def _parse_candidate(fragment: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Parse one candidate fragment strictly, then via tolerant repair.

    Repair (the ``json-repair`` library — the same dependency the LLM clients
    use) fixes common model slips like trailing commas and max-tokens
    truncation. A repaired result is only trusted when the fragment at least
    resembles JSON (contains a quote and a colon) and the result is a non-empty
    dict — repairing prose braces like ``{not json}`` must not fabricate a
    payload.

    Preconditions: ``fragment`` is a str.
    Postconditions: returns ``(parsed, strict)`` where ``strict`` is True iff
    plain ``json.loads`` accepted the fragment, or ``(None, False)`` when no
    dict can be produced. Never raises — ``RecursionError`` from pathologically
    deep nesting and any repair-library failure are contained here.
    """
    try:
        parsed = json.loads(fragment)
        if isinstance(parsed, dict):
            return parsed, True
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass
    if '"' in fragment and ":" in fragment:
        try:
            import json_repair

            parsed = json_repair.loads(fragment)
            if isinstance(parsed, dict) and parsed:
                return parsed, False
        except Exception:  # noqa: BLE001 - salvage must never raise
            pass
    return None, False


def _salvage_object(
    content: str, accept: Callable[[Dict[str, Any]], bool]
) -> Optional[Dict[str, Any]]:
    """Salvage the authoritative JSON object matching ``accept`` from raw output.

    The single engine behind ``extract_json_object`` and
    ``extract_task_assignment_from_content``: strip think-blocks/``<json>``
    wrappers, scan for balanced objects (string-aware, linear time), parse each
    candidate strictly-then-repaired, and select by rank.

    Selection rule: strict-parsed outranks repaired, non-empty outranks empty,
    and ties break toward the LAST candidate in document order — models
    routinely echo a format example ("I will answer in the form {...}") before
    the final object, so the trailing object is the authoritative one.

    Preconditions: ``content`` is a str (may be empty); ``accept`` is a pure
    predicate over a parsed dict.
    Postconditions: returns an accepted dict, or ``None`` when nothing
    salvageable matches. Never raises on malformed input.
    """
    if not content or not content.strip():
        return None
    stripped = _strip_wrappers(content)
    spans, first_unclosed = _balanced_object_spans(stripped)

    best: Optional[Dict[str, Any]] = None
    best_rank = (-1, -1)
    attempts = 0
    # Reverse order + strictly-greater rank replacement keeps the LAST candidate
    # for equal ranks, per the selection rule above.
    for start, end in reversed(spans):
        if attempts >= _MAX_PARSE_ATTEMPTS:
            break
        attempts += 1
        parsed, strict = _parse_candidate(stripped[start:end])
        if parsed is None or not accept(parsed):
            continue
        rank = (1 if strict else 0, 1 if parsed else 0)
        if rank == (1, 1):
            return parsed
        if rank > best_rank:
            best, best_rank = parsed, rank
    if best is not None:
        return best

    # Whole payload inside a markdown fence (may itself need repair).
    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", content, re.IGNORECASE)
    if fence:
        parsed, _strict = _parse_candidate(fence.group(1).strip())
        if parsed is not None and accept(parsed):
            return parsed

    # Max-tokens truncation: the object never closed. Repair can complete it.
    if first_unclosed != -1:
        parsed, _strict = _parse_candidate(stripped[first_unclosed:])
        if parsed is not None and accept(parsed):
            return parsed

    return None


def _has_tasks(parsed: Dict[str, Any]) -> bool:
    """Accept predicate: a task-assignment dict with a non-empty ``tasks`` list."""
    tasks = parsed.get("tasks")
    return isinstance(tasks, list) and len(tasks) > 0


def extract_task_assignment_from_content(content: str) -> Optional[Dict[str, Any]]:
    """
    When LLM returns raw content wrapper {"content": "..."}, try to extract
    a task assignment dict (tasks, execution_order, etc.) from the text.

    Thin wrapper over the shared salvage engine with the task-assignment accept
    predicate; candidates without a non-empty ``tasks`` list are skipped.

    Preconditions: ``content`` is a str (may be empty).
    Postconditions: returns the salvaged assignment dict, or ``None`` if nothing
    usable is found. Never raises on malformed input.
    """
    return _salvage_object(content, _has_tasks)


def extract_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Recover the authoritative JSON *object* from raw LLM content.

    Thin wrapper over the shared salvage engine accepting any dict: strips
    ``<think>``/``<thinking>``/``<reasoning>`` blocks, unwraps ``<json>``,
    scans for balanced objects with a string-aware linear pass (braces inside
    JSON string values do not corrupt the scan), parses each candidate strictly
    then via ``json-repair`` (trailing commas, truncated output), and prefers
    strict, non-empty, later candidates — so a format example echoed before the
    real payload is not mistaken for it.

    Preconditions:
        - ``content`` is a ``str`` (may be empty).
    Postconditions:
        - Returns a ``dict`` on success, or ``None`` when nothing parses. Never
          raises on malformed input.
    """
    return _salvage_object(content, lambda parsed: True)


# Extensions we treat as file paths (backend + frontend)
_PATH_EXTENSIONS = (
    ".py",
    ".ts",
    ".html",
    ".scss",
    ".css",
    ".json",
    ".md",
    ".yaml",
    ".yml",
    ".js",
    ".spec.ts",
)


def _looks_like_path(line: str) -> bool:
    """True if the line looks like a file path (has / or known extension)."""
    s = line.strip()
    if not s or len(s) > 200:
        return False
    if "/" in s:
        return True
    return any(s.endswith(ext) for ext in _PATH_EXTENSIONS)


def _clean_files_dict(parsed: object) -> Dict[str, str]:
    """Extract a ``{path: content}`` dict from a parsed object's ``files`` key.

    Postconditions: returns only entries where both key and value are non-empty
    strings (value non-blank); empty dict when ``parsed`` has no usable ``files``.
    """
    files: Dict[str, str] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("files"), dict):
        for k, v in parsed["files"].items():
            if isinstance(k, str) and isinstance(v, str) and k and v.strip():
                files[k] = v
    return files


def _files_from_json_object(stripped: str) -> Dict[str, str]:
    """Strategy 1: the first balanced ``{...}`` carrying a ``files`` dict (model may wrap JSON in text)."""
    start = stripped.find("{")
    if start == -1:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(stripped)):
        if stripped[i] == "{":
            depth += 1
        elif stripped[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return {}
    try:
        return _clean_files_dict(json.loads(stripped[start : end + 1]))
    except (json.JSONDecodeError, TypeError):
        return {}


def _files_from_json_codeblock(content: str) -> Dict[str, str]:
    """Strategy 2: a ```` ```json ```` fence wrapping the whole ``{... "files": {...}}`` response."""
    json_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", content, re.IGNORECASE)
    if not json_match:
        return {}
    try:
        return _clean_files_dict(json.loads(json_match.group(1).strip()))
    except (json.JSONDecodeError, TypeError):
        return {}


def _infer_block_path(info: str, lines: list[str]) -> tuple[str | None, int]:
    """Infer ``(path, body_start_index)`` for one fenced block's info string + body lines.

    Postconditions: ``path`` is ``None`` when no path can be inferred; otherwise
    ``body_start_index`` is the first body line that is actual file content.
    """
    if info and _looks_like_path(info):
        return info.strip(), 0
    if lines and _looks_like_path(lines[0]):
        return lines[0].strip(), 1
    if info and any(info.endswith(ext) for ext in _PATH_EXTENSIONS):
        return info.strip(), 0
    if lines:
        # Check for a path comment: // path: src/foo.ts or # path: app/foo.py
        first = lines[0].strip()
        for prefix in ("path:", "file:", "filepath:"):
            if prefix in first.lower():
                idx = first.lower().find(prefix)
                rest = first[idx + len(prefix) :].strip().strip("'\"").strip()
                if rest and _looks_like_path(rest):
                    return rest, 1
                break
    return None, 0


def _files_from_fenced_blocks(content: str) -> Dict[str, str]:
    """Strategy 3: each ```` ```(lang_or_path)\\n body ``` ```` block → one inferred file."""
    files: Dict[str, str] = {}
    pattern = re.compile(r"```([^\n]*)\n([\s\S]*?)```", re.MULTILINE)
    for match in pattern.finditer(content):
        info = match.group(1).strip()
        body = match.group(2)
        if not body:
            continue
        lines = body.split("\n")
        path, body_start = _infer_block_path(info, lines)
        if path and path not in files:
            content_str = "\n".join(lines[body_start:]).rstrip()
            if content_str:
                files[path] = content_str
    return files


def extract_files_from_content(content: str) -> Dict[str, str]:
    """
    Parse markdown code blocks from raw LLM content and build a files dict.

    Supports:
    - Raw or wrapped JSON: content starting with { or containing a single {...} with "files"
    - ```path/to/file.ext\\n<content>
    - ```\\npath/to/file.ext\\n<content>  (first line is path)
    - ```json\\n{...}  (try to parse as JSON with "files" key)
    - ```lang\\n<content>  (single block: infer path from extension)

    Returns a dict of path -> content. May be empty if nothing could be parsed.
    The JSON strategies short-circuit (first non-empty wins); fenced-block
    parsing is the fallback.
    """
    if not content or not content.strip():
        return {}
    stripped = content.strip()
    # JSON strategies short-circuit: the codeblock regex only runs if the
    # balanced-object scan found nothing.
    files = _files_from_json_object(stripped)
    if files:
        return files
    files = _files_from_json_codeblock(content)
    if files:
        return files
    return _files_from_fenced_blocks(content)


def heuristic_extract_files_from_content(
    content: str, extensions: tuple = (".py", ".ts", ".html", ".scss")
) -> Dict[str, str]:
    """
    When extract_files_from_content returns nothing, try to recover files by splitting on path-like
    lines or "File:" / "path:" headers. Used so backend/frontend have something to write instead of
    failing with zero files.

    Also parses markdown headers like ## app/main.py or ### path/to/file.ext as file delimiters.
    """
    if not content or not content.strip():
        return {}
    files: Dict[str, str] = {}
    lines = content.split("\n")
    path_exts = set(extensions)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        path_candidate: str | None = None
        # Markdown file headers: ## app/main.py or ### path/to/file.ext
        md_header_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if md_header_match:
            header_content = md_header_match.group(1).strip()
            if _looks_like_path(header_content) and any(
                header_content.endswith(ext) for ext in path_exts
            ):
                path_candidate = header_content
        elif re.match(r"^(?:File|path|filepath)\s*:\s*\S+", stripped, re.IGNORECASE):
            # "File: app/main.py" or "path: src/foo.ts"
            match = re.search(r":\s*(\S+)", stripped)
            if match:
                path_candidate = match.group(1).strip("'\"").strip()
        elif (
            stripped
            and "/" in stripped
            and len(stripped) < 120
            and any(stripped.endswith(ext) for ext in path_exts)
        ):
            # Standalone path line
            path_candidate = stripped
        if (
            path_candidate
            and path_candidate not in files
            and any(path_candidate.endswith(ext) for ext in path_exts)
        ):
            # Collect content until next path-like line or blank separator
            body_lines: list = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                if not next_stripped:
                    i += 1
                    continue
                if re.match(r"^(?:File|path|filepath)\s*:\s*\S+", next_stripped, re.IGNORECASE):
                    break
                if (
                    next_stripped
                    and "/" in next_stripped
                    and len(next_stripped) < 120
                    and any(next_stripped.endswith(ext) for ext in path_exts)
                ):
                    break
                body_lines.append(next_line)
                i += 1
            content_str = "\n".join(body_lines).rstrip()
            if content_str and len(content_str) > 10:
                files[path_candidate] = content_str
            continue
        i += 1
    return files


def extract_single_python_block(content: str) -> Optional[str]:
    """
    Last-resort extraction: find a single ```python or ```py block and return its body.
    Used when extract_files_from_content and heuristic_extract return nothing.
    Returns None if no Python block found.
    """
    if not content or not content.strip():
        return None
    match = re.search(r"```(?:python|py)\s*\n([\s\S]*?)```", content, re.IGNORECASE)
    if match:
        body = match.group(1).strip()
        if body and len(body) > 20:
            return body
    return None
