"""
Utilities for parsing LLM responses when structured JSON parsing fails.

When the LLM returns raw content (e.g. {"content": "..."}), extract file paths
and bodies from markdown code blocks so agents can still produce files.
Also extracts task assignments when Tech Lead / Task Generator return raw content.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Collection, Dict, Iterator, List, Optional, Tuple

# Bounds on salvage work. Strict ``raw_decode`` probes are O(1) each (they fail
# or succeed at the current brace without rescanning), so their bound is
# generous; tolerant repair can be superlinear on large fragments, so it gets a
# tighter cap. Both keep adversarial output (e.g. thousands of "{" or "{}"
# pairs) from turning salvage into a CPU sink.
_MAX_STRICT_PROBES = 256
_MAX_REPAIR_ATTEMPTS = 32


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


def _looks_like_json_object(fragment: str) -> bool:
    """True when *fragment* is shaped like a JSON object literal.

    Either an empty object (``{}``), or the first non-space character after the
    opening ``{`` is a quote (a quoted key) AND a ``:`` appears later (the
    key/value separator). This gates tolerant repair so prose braces are never
    fabricated into a dict — both ``{see the "spec" above}`` (no leading quote)
    and ``{"name" property}`` (quoted word but no colon) are rejected — while
    genuine model slips still pass: ``{"a": 1,}`` (trailing comma) and ``{"a": ``
    (max-tokens truncation) are a quoted key followed by a colon.

    Preconditions: ``fragment`` is a str.
    Postconditions: bool; never raises.
    """
    inner = fragment.lstrip()
    if not inner.startswith("{"):
        return False
    rest = inner[1:].lstrip()
    if rest.startswith("}"):
        return True
    # A real object literal: a quoted key, then eventually its ``:`` separator.
    # The bare colon check is deliberately permissive (a ``:`` inside a string
    # value counts) — its only job is to reject prose braces that carry a quoted
    # word but no key/value structure, which json-repair would otherwise coerce.
    return rest.startswith('"') and ":" in rest


def _repair_object(fragment: str) -> Optional[Dict[str, Any]]:
    """Repair *fragment* into a non-empty dict via ``json-repair``, or ``None``.

    The caller must have already confirmed the fragment is object-shaped (see
    ``_looks_like_json_object``) — repair fabricates a value for a dangling key,
    so it is only ever run on fragments whose shape already commits them to
    being an object literal.

    Preconditions: ``fragment`` is an object-shaped str.
    Postconditions: a non-empty ``dict`` or ``None``; never raises. A missing
    ``json-repair`` wheel or any library failure yields ``None`` (repair
    silently disabled — salvage degrades rather than crashing).
    """
    try:
        import json_repair

        parsed = json_repair.loads(fragment)
    except Exception:  # noqa: BLE001 - salvage must never raise
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _parse_or_repair(fragment: str) -> Optional[Dict[str, Any]]:
    """Strict-parse *fragment*, then object-shaped repair. For fence/tail paths.

    Preconditions: ``fragment`` is a str.
    Postconditions: a ``dict`` (possibly empty, when strict JSON) or ``None``;
    never raises.
    """
    try:
        parsed = json.loads(fragment)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass
    if _looks_like_json_object(fragment):
        return _repair_object(fragment)
    return None


def _iter_strict_objects(text: str) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield ``(start, obj)`` for each strict-JSON object embedded in *text*.

    Uses ``json.JSONDecoder.raw_decode`` from each ``{`` so prose braces and
    prose quotation marks cannot corrupt the scan the way a hand-rolled
    brace-matcher can — ``raw_decode`` simply fails on ``{ not json }`` and the
    probe advances to the next ``{``. This is the recall fallback: it finds a
    real object buried under an unclosed prose brace (``the set {1,2 ...
    {"tasks": [...]}``) that the top-level span scan, which sees the whole thing
    as one never-closed object, misses.

    Preconditions: ``text`` is a str.
    Postconditions: yields dicts in document order using at most
    ``_MAX_STRICT_PROBES`` probes; never raises.
    """
    decoder = json.JSONDecoder()
    i = 0
    probes = 0
    while probes < _MAX_STRICT_PROBES:
        j = text.find("{", i)
        if j == -1:
            return
        probes += 1
        try:
            obj, end = decoder.raw_decode(text, j)
        except (ValueError, RecursionError):
            # ValueError: not JSON at this brace. RecursionError: pathologically
            # deep nesting — skip this probe rather than let salvage raise.
            i = j + 1
            continue
        if isinstance(obj, dict):
            yield j, obj
            i = max(end, j + 1)
        else:
            i = j + 1


def _select_object(
    candidates: List[Tuple[int, Dict[str, Any]]],
    accept: Callable[[Dict[str, Any]], bool],
) -> Optional[Dict[str, Any]]:
    """Pick the authoritative object: the last non-empty *accepted* candidate.

    Ranking key ``(non_empty, start)``: a non-empty object always outranks an
    empty ``{}``, and among equally-non-empty accepted candidates the LAST in
    document order wins — models echo a format example ("I will answer as
    {...}") before the real payload, so the trailing object is authoritative.
    Strict-vs-repaired is deliberately NOT in the rank: a repaired real payload
    that appears after a strict format echo must still win, so position is the
    only authority signal once a candidate is accepted.

    Preconditions: ``candidates`` is a list of ``(start, dict)``; ``accept`` is
    a pure predicate. Postconditions: an accepted dict or ``None``; never raises.
    """
    best: Optional[Dict[str, Any]] = None
    best_key: Tuple[int, int] = (-1, -1)
    for start, obj in candidates:
        if not accept(obj):
            continue
        key = (1 if obj else 0, start)
        if key > best_key:
            best, best_key = obj, key
    return best


def _descend_envelope(
    candidates: List[Tuple[int, Dict[str, Any]]],
    accept: Callable[[Dict[str, Any]], bool],
) -> Optional[Dict[str, Any]]:
    """Recover a payload wrapped one level deep in a rejected envelope object.

    e.g. ``{"result": {"tasks": [...]}}`` when the predicate wants a top-level
    ``tasks``. Only fires when no top-level candidate was accepted, so it never
    overrides a direct match; later parents are tried first.

    Preconditions/Postconditions: as ``_select_object``.
    """
    for _start, obj in reversed(candidates):
        # Preserve document order among the parent's dict values so the LAST
        # accepted child wins the positional tiebreak, per _select_object's rule.
        nested = [(i, v) for i, v in enumerate(obj.values()) if isinstance(v, dict)]
        pick = _select_object(nested, accept)
        if pick is not None:
            return pick
    return None


def _salvage_object(
    content: str, accept: Callable[[Dict[str, Any]], bool]
) -> Optional[Dict[str, Any]]:
    """Salvage the authoritative JSON object matching ``accept`` from raw output.

    The single engine behind ``extract_json_object`` and
    ``extract_task_assignment_from_content``. After stripping think-blocks and
    ``<json>`` wrappers, five ordered strategies run — the first accepted object
    wins, so cheaper/higher-confidence strategies pre-empt the fallbacks:

    1. Top-level balanced spans (string-aware), each strict-parsed then, if it
       is object-shaped, tolerant-repaired; selected by ``_select_object``. This
       resolves clean output, prose-wrapped objects, trailing-comma repair, the
       format-echo-before-payload case, and empty-``{}`` shadowing.
    2. Envelope descent one level into a rejected top-level object.
    3. A whole payload inside a markdown fence — searched on the WRAPPER-STRIPPED
       text so a fenced draft inside a removed ``<think>`` block is not
       resurrected as the answer.
    4. Max-tokens truncation: the first never-closed object, repaired — but only
       when it is object-shaped, so an unclosed prose brace cannot fabricate one.
    5. Recall fallback: ``raw_decode`` from every ``{`` (strict only), which
       finds a real object buried under prose braces/quotes that derail the span
       scan. Last so it never hijacks a case strategies 1-4 already resolved.

    Preconditions: ``content`` is a str (may be empty); ``accept`` is a pure
    predicate over a parsed dict.
    Postconditions: returns an accepted dict, or ``None`` when nothing
    salvageable matches. Never raises on malformed input.
    """
    if not content or not content.strip():
        return None
    stripped = _strip_wrappers(content)
    spans, first_unclosed = _balanced_object_spans(stripped)

    # An accepted but EMPTY ``{}`` is only ever a last resort: it must not
    # short-circuit a later strategy that would find a non-empty payload (e.g. a
    # leading ``{}`` shadowing a real object the recall scan recovers). Non-empty
    # accepted dicts win immediately; an empty one is stashed and returned only
    # if every strategy is exhausted.
    best_empty: Optional[Dict[str, Any]] = None

    def _use(pick: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return *pick* if it is a non-empty dict; stash an empty one instead.

        Preconditions: ``pick`` is an accepted dict or ``None``.
        Postconditions: returns ``pick`` when it is truthy (non-empty); records
        the first empty ``{}`` in ``best_empty`` and returns ``None`` so the
        caller keeps searching; returns ``None`` for ``None``.
        """
        nonlocal best_empty
        if pick is None:
            return None
        if pick:
            return pick
        if best_empty is None:
            best_empty = pick
        return None

    # Strategy 1: top-level spans, strict-then-object-shaped-repair.
    pool: List[Tuple[int, Dict[str, Any]]] = []
    repairs = 0
    for start, end in spans:
        fragment = stripped[start:end]
        try:
            parsed = json.loads(fragment)
            if isinstance(parsed, dict):
                pool.append((start, parsed))
                continue
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            pass
        if repairs < _MAX_REPAIR_ATTEMPTS and _looks_like_json_object(fragment):
            repairs += 1
            repaired = _repair_object(fragment)
            if repaired is not None:
                pool.append((start, repaired))
    result = _use(_select_object(pool, accept))
    if result is not None:
        return result

    # Strategy 2: envelope descent into rejected top-level objects.
    result = _use(_descend_envelope(pool, accept))
    if result is not None:
        return result

    # Strategy 3: whole payload inside a markdown fence (on STRIPPED text).
    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", stripped, re.IGNORECASE)
    if fence:
        parsed = _parse_or_repair(fence.group(1).strip())
        if parsed is not None and accept(parsed):
            result = _use(parsed)
            if result is not None:
                return result

    # Strategy 4: max-tokens truncation — the first object never closed.
    if first_unclosed != -1:
        fragment = stripped[first_unclosed:]
        if _looks_like_json_object(fragment):
            repaired = _repair_object(fragment)
            if repaired is not None and accept(repaired):
                result = _use(repaired)
                if result is not None:
                    return result

    # Strategy 5: recall fallback — strict objects buried under prose.
    recall = list(_iter_strict_objects(stripped))
    result = _use(_select_object(recall, accept))
    if result is not None:
        return result
    result = _use(_descend_envelope(recall, accept))
    if result is not None:
        return result

    return best_empty


def _has_tasks(parsed: Dict[str, Any]) -> bool:
    """Accept predicate: a task-assignment dict with a non-empty ``tasks`` list."""
    tasks = parsed.get("tasks")
    return isinstance(tasks, list) and len(tasks) > 0


def _accept_with_keys(
    required_keys: Optional[Collection[str]],
) -> Callable[[Dict[str, Any]], bool]:
    """Build an accept predicate that anchors on known payload keys.

    With ``required_keys`` set, a candidate is accepted only when it carries at
    least one of them — so a usage echo (``{"tokens": 123}``) or a format recap
    that lacks the anchor is filtered out before selection, and only genuine
    same-schema candidates remain for the positional tiebreak. With no keys
    (``None`` / empty), any dict is accepted (the caller has no schema to anchor
    on).

    Preconditions: ``required_keys`` is ``None``, a single key str, or an
    iterable of str.
    Postconditions: returns a pure predicate; never raises. An absent, empty, or
    empty-yielding ``required_keys`` accepts any dict (no schema to anchor on).
    """
    if not required_keys:
        return lambda parsed: True
    # A bare ``str``/``bytes`` is a single key, not an iterable of one-character
    # keys — ``tuple("tasks")`` would anchor on ``('t','a','s','k','s')``. Any
    # other iterable (list/tuple/set/generator) is materialized once so it is
    # iterated exactly once regardless of candidate count, and so an empty one
    # (e.g. a generator that yields nothing) falls back to accept-any rather than
    # reject-everything.
    if isinstance(required_keys, (str, bytes)):
        keys: Tuple[Any, ...] = (required_keys,)
    else:
        keys = tuple(required_keys)
        if not keys:
            return lambda parsed: True
    return lambda parsed: any(k in parsed for k in keys)


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


def extract_json_object(
    content: str, required_keys: Optional[Collection[str]] = None
) -> Optional[Dict[str, Any]]:
    """Recover the authoritative JSON *object* from raw LLM content.

    Thin wrapper over the shared salvage engine: strips
    ``<think>``/``<thinking>``/``<reasoning>`` blocks, unwraps ``<json>``, and
    runs the five ordered salvage strategies (see ``_salvage_object``), so
    braces inside JSON string values, a format example echoed before the real
    payload, trailing commas, truncation, and prose braces are all handled.

    When the caller knows the payload's schema, pass ``required_keys``: a
    candidate is accepted only if it carries at least one of them, which filters
    out usage echoes and envelope wrappers that would otherwise win the
    positional tiebreak. Without it, any dict is accepted.

    Preconditions:
        - ``content`` is a ``str`` (may be empty).
        - ``required_keys`` is ``None`` or a collection of str anchor keys.
    Postconditions:
        - Returns a ``dict`` on success, or ``None`` when nothing parses (or no
          candidate carries an anchor key). Never raises on malformed input.
    """
    return _salvage_object(content, _accept_with_keys(required_keys))


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
