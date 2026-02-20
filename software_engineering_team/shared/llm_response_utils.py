"""
Utilities for parsing LLM responses when structured JSON parsing fails.

When the LLM returns raw content (e.g. {"content": "..."}), extract file paths
and bodies from markdown code blocks so agents can still produce files.
Also extracts task assignments when Tech Lead / Task Generator return raw content.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def extract_task_assignment_from_content(content: str) -> Optional[Dict[str, Any]]:
    """
    When LLM returns raw content wrapper {"content": "..."}, try to extract
    a task assignment dict (tasks, execution_order, etc.) from the text.
    Returns None if nothing usable found.
    """
    if not content or not content.strip():
        return None
    stripped = content.strip()

    # Strip thinking/reasoning blocks that some models emit
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"<thinking>.*?</thinking>", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"<reasoning>.*?</reasoning>", "", stripped, flags=re.DOTALL)
    stripped = stripped.strip()

    # Extract JSON from XML-style tags if present
    json_tag_match = re.search(r"<json>\s*([\s\S]*?)\s*</json>", stripped)
    if json_tag_match:
        stripped = json_tag_match.group(1).strip()

    # Find first { and match braces to get a complete JSON object
    start = stripped.find("{")
    if start == -1:
        return None

    # Try all top-level {...} objects (greedy match from each {)
    i = start
    while i < len(stripped):
        if stripped[i] != "{":
            i += 1
            continue
        depth = 0
        end = -1
        for j in range(i, len(stripped)):
            if stripped[j] == "{":
                depth += 1
            elif stripped[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            i += 1
            continue
        try:
            parsed = json.loads(stripped[i : end + 1])
            if isinstance(parsed, dict):
                tasks = parsed.get("tasks")
                if isinstance(tasks, list) and len(tasks) > 0:
                    return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        i += 1

    # Try JSON inside markdown code block
    json_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", content, re.IGNORECASE)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1).strip())
            if isinstance(parsed, dict):
                tasks = parsed.get("tasks")
                if isinstance(tasks, list) and len(tasks) > 0:
                    return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    return None


# Extensions we treat as file paths (backend + frontend)
_PATH_EXTENSIONS = (
    ".py", ".ts", ".html", ".scss", ".css", ".json", ".md", ".yaml", ".yml",
    ".js", ".spec.ts",
)


def _looks_like_path(line: str) -> bool:
    """True if the line looks like a file path (has / or known extension)."""
    s = line.strip()
    if not s or len(s) > 200:
        return False
    if "/" in s:
        return True
    return any(s.endswith(ext) for ext in _PATH_EXTENSIONS)


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
    """
    if not content or not content.strip():
        return {}

    files: Dict[str, str] = {}
    stripped = content.strip()

    # Try to parse content as JSON: find first "files"-bearing object (model may wrap JSON in text)
    start = stripped.find("{")
    if start != -1:
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
        if end != -1:
            try:
                parsed = json.loads(stripped[start : end + 1])
                if isinstance(parsed, dict) and parsed.get("files") and isinstance(parsed["files"], dict):
                    for k, v in parsed["files"].items():
                        if isinstance(k, str) and isinstance(v, str) and k and v.strip():
                            files[k] = v
                    if files:
                        return files
            except (json.JSONDecodeError, TypeError):
                pass

    # Try JSON inside markdown code block - model might have wrapped the whole response in a code block
    json_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", content, re.IGNORECASE)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1).strip())
            if isinstance(parsed, dict) and parsed.get("files"):
                f = parsed["files"]
                if isinstance(f, dict):
                    for k, v in f.items():
                        if isinstance(k, str) and isinstance(v, str) and k and v.strip():
                            files[k] = v
                    if files:
                        return files
        except (json.JSONDecodeError, TypeError):
            pass

    # Find all fenced code blocks: ```(lang_or_path)?\n(body)```
    pattern = re.compile(r"```([^\n]*)\n([\s\S]*?)```", re.MULTILINE)
    for match in pattern.finditer(content):
        info = match.group(1).strip()
        body = match.group(2)
        if not body:
            continue

        lines = body.split("\n")
        path: str | None = None
        body_start = 0

        # First line might be the path
        if info and _looks_like_path(info):
            path = info.strip()
            body_start = 0
        elif lines and _looks_like_path(lines[0]):
            path = lines[0].strip()
            body_start = 1
        elif info and any(info.endswith(ext) for ext in _PATH_EXTENSIONS):
            path = info.strip()
            body_start = 0
        elif lines:
            # Check for path comment: // path: src/foo.ts or # path: app/foo.py
            first = lines[0].strip()
            path_prefix = ("path:", "file:", "filepath:")
            for prefix in path_prefix:
                if prefix in first.lower():
                    idx = first.lower().find(prefix)
                    rest = first[idx + len(prefix) :].strip().strip("'\"").strip()
                    if rest and _looks_like_path(rest):
                        path = rest
                        body_start = 1
                        break
                    break

        if path and path not in files:
            content_str = "\n".join(lines[body_start:]).rstrip()
            if content_str:
                files[path] = content_str

    return files


def heuristic_extract_files_from_content(content: str, extensions: tuple = (".py", ".ts", ".html", ".scss")) -> Dict[str, str]:
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
            if _looks_like_path(header_content) and any(header_content.endswith(ext) for ext in path_exts):
                path_candidate = header_content
        elif re.match(r"^(?:File|path|filepath)\s*:\s*\S+", stripped, re.IGNORECASE):
            # "File: app/main.py" or "path: src/foo.ts"
            match = re.search(r":\s*(\S+)", stripped)
            if match:
                path_candidate = match.group(1).strip("'\"").strip()
        elif stripped and "/" in stripped and len(stripped) < 120 and any(stripped.endswith(ext) for ext in path_exts):
            # Standalone path line
            path_candidate = stripped
        if path_candidate and path_candidate not in files and any(path_candidate.endswith(ext) for ext in path_exts):
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
                if next_stripped and "/" in next_stripped and len(next_stripped) < 120 and any(next_stripped.endswith(ext) for ext in path_exts):
                    break
                body_lines.append(next_line)
                i += 1
            content_str = "\n".join(body_lines).rstrip()
            if content_str and len(content_str) > 10:
                files[path_candidate] = content_str
            continue
        i += 1
    return files
