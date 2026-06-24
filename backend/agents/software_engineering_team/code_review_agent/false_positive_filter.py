"""False-positive verification for code-review findings.

The map-reduce reviewer (``coordinator.py``) flags issues from *bounded chunks*:
each chunk review sees only a slice of one file and none of the rest of the
codebase. That blind spot manufactures false positives — a finding like
"function ``foo`` is never defined", "this import is unused", or "no tests for
X" can be wrong because the defining/using/test code lives in a part of the file
(or another file) the chunk reviewer never saw.

This module re-checks each genuine reviewer finding against the *whole*
submission before it reaches the developer. The verification agent is given read
access to every file under review via tools (``read_file``, ``list_files``,
``search_codebase``, ``find_function_at_line``), so it can pull up exactly the
code needed to confirm or refute a finding rather than guessing from a single chunk.

Two invariants hold:

    - **Fail-safe.** A finding is dropped ONLY on an explicit, confident
      false-positive verdict. Anything the verifier cannot assess — a finding
      with no file path, a finding for a path not in the submission, an
      unparsable verdict, or a verifier/LLM error — keeps the finding.
      Verification can only ever *remove* a confirmed false positive; it never
      invents a finding, upgrades a severity, or breaks the review. Dropping a
      real issue is far worse than keeping a questionable one, so every
      ambiguous case keeps the issue.

    - **Coverage/safety findings never reach this module.** The coordinator
      passes only genuine reviewer findings here; the "not reviewed" degraded
      findings and empty-file notices are filtered separately and can never be
      removed as "false positives", so the gate's anti-loop safety nets are
      untouched.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from strands import Agent, tool
from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient
from shared_env import env_flag_enabled
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import FALSE_POSITIVE_VERIFY_PROMPT

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_FALSE_POSITIVE_FILTER=false``/``0``/``no``
# disables the verification pass (see docs/ENV_VARS.md). Any other value (or unset)
# leaves it enabled.
_FILTER_ENV = "CODE_REVIEW_FALSE_POSITIVE_FILTER"

# How many file paths to enumerate inline in the verification prompt before
# deferring the rest to the ``list_files`` tool. A manifest is a convenience so
# the model knows what it can read; it is never the only way to discover files.
_MANIFEST_LIMIT = 300

# Cap on substring matches returned by ``search_codebase`` so a common token
# cannot flood the tool result.
_SEARCH_MATCH_LIMIT = 60

# Cap on the task-description and each acceptance-criterion text inlined into the
# verification prompt. The file body already has its own ``max_inline_chars``
# bound; this keeps an unbounded task/criteria field from dominating the prompt
# or overflowing context. Normal task text is far below this.
_CONTEXT_FIELD_CHARS = 4_000


def _verify_parallelism() -> int:
    """Concurrency cap for per-file verification calls.

    Verification reuses the map phase's knob (``CODE_REVIEW_MAP_PARALLELISM``):
    the two phases run one after the other (all chunks reviewed, then findings
    verified), so they share one concurrency budget rather than a second one to
    tune. Delegating to the coordinator's ``_map_parallelism`` keeps a single
    definition of that knob and its default. Imported lazily because the
    coordinator imports this module at load time.

    Postconditions:
        - Returns an int >= 1 (the coordinator clamps the env value to floor 1);
          ``1`` runs the per-file verification calls sequentially.
    """
    from .coordinator import _map_parallelism

    return _map_parallelism()


@dataclass
class CodebaseIndex:
    """In-memory view of all code the verifier may read to check a finding.

    Invariants:
        - ``files`` maps a file path to its FULL content (never a chunk or a
          truncated excerpt): seeing the whole file is the entire point — the
          chunk reviewer's partial view is what produced the false positive.
        - ``existing_codebase`` is the (already capped) pre-existing-code excerpt
          passed for context; it is exposed as the read-only pseudo-path
          ``<existing codebase>`` so the verifier can consult it like any file.
        - The index is read-only after construction: no method mutates ``files``
          or ``existing_codebase``, so it is safe to share across the parallel
          verification worker threads.
    """

    files: Dict[str, str]
    existing_codebase: str = ""

    EXISTING_CODEBASE_PATH = "<existing codebase>"

    @classmethod
    def from_input(cls, input_data: CodeReviewInput) -> "CodebaseIndex":
        """Build the index from a review input's ``files`` or legacy ``code``.

        Postconditions:
            - When ``files`` is set, every file with non-blank content is
              included (insertion order preserved), with no header parsing.
            - Otherwise the legacy ``code`` blob is parsed into ``### path ###``
              blocks via the coordinator's canonical parser; headerless and
              blank blocks are dropped (they cannot be addressed by a path).
            - ``existing_codebase`` carries the input's existing-codebase excerpt
              (empty string when absent).
        """
        if input_data.files is not None:
            files = {
                path: content
                for path, content in input_data.files.items()
                if content and content.strip()
            }
        else:
            # Lazy import keeps this module free of an import cycle with the
            # coordinator (which imports ``filter_false_positives`` at module load).
            from .coordinator import parse_code_into_file_blocks

            files = {}
            for path, content in parse_code_into_file_blocks(input_data.code or ""):
                if path and content.strip():
                    files[path] = content
        return cls(files=files, existing_codebase=input_data.existing_codebase or "")

    def _readable_sources(self) -> List[Tuple[str, str]]:
        """All ``(path, content)`` the verifier can read, existing-codebase last.

        The single source of truth for :meth:`list_files` and the search index,
        so both expose exactly the same set of readable sources.

        Postconditions:
            - Returns the submission's own files as ``(path, content)`` in
              insertion order, then the existing-codebase excerpt under the
              ``<existing codebase>`` pseudo-path iff a non-blank one was
              provided. Never raises; the returned list is a fresh copy.
        """
        sources = list(self.files.items())
        if self.existing_codebase.strip():
            sources.append((self.EXISTING_CODEBASE_PATH, self.existing_codebase))
        return sources

    def list_files(self) -> List[str]:
        """Return every readable path, the existing-codebase pseudo-path last.

        Postconditions:
            - The submission's own files come first in insertion order; the
              ``<existing codebase>`` pseudo-path is appended only when a
              non-blank existing-codebase excerpt was provided.
        """
        return [path for path, _ in self._readable_sources()]

    def _resolve(self, key: str) -> Tuple[Optional[str], List[str]]:
        """Resolve a stripped ``key`` to ``(canonical_key_or_None, suffix_hits)``.

        The one place path resolution runs, shared by :meth:`resolve_path` and
        :meth:`read_file` so neither rescans. ``suffix_hits`` is returned so a
        caller can tell an absent path (empty) from an ambiguous one (>1) without
        a second scan.

        Preconditions:
            - ``key`` is already whitespace-stripped.

        Postconditions:
            - ``(<existing codebase>, [])`` when ``key`` names the pseudo-path and
              a non-blank excerpt exists; ``(None, [])`` when it names it without
              one, or when ``key`` is blank.
            - ``(exact_key, [])`` on an exact file match.
            - ``(sole_hit, hits)`` when exactly one suffix match, else
              ``(None, hits)``, where ``hits`` are the bare-name suffix matches —
              never raises.
        """
        if not key:
            return None, []
        if key == self.EXISTING_CODEBASE_PATH:
            return (self.EXISTING_CODEBASE_PATH if self.existing_codebase.strip() else None), []
        if key in self.files:
            return key, []
        # Bare-name fallback: the model often cites ``main.py`` for
        # ``app/services/main.py``. Match every stored path whose final
        # ``/``-segment equals ``key`` (a leading ``./`` ignored); a unique hit
        # resolves, and the full list lets ``read_file`` distinguish ambiguity.
        normalized = key.lstrip("./")
        hits = [p for p in self.files if p == normalized or p.endswith("/" + normalized)]
        return (hits[0] if len(hits) == 1 else None), hits

    def resolve_path(self, path: str) -> Optional[str]:
        """Resolve a cited path to a canonical readable key, or None.

        Shared by ``read_file`` (to locate a hit) and the filter (to decide
        whether a finding's file is even readable before spending a
        verification call on it).

        Postconditions:
            - Returns the ``<existing codebase>`` pseudo-path when the cited path
              names it and a non-blank excerpt exists.
            - Returns an exact file key, or the sole suffix match (``main.py`` →
              ``app/main.py``).
            - Returns None for a blank, absent, or ambiguous path — the verifier
              would have no single primary file to read, so the caller keeps the
              finding rather than verify it.
        """
        resolved, _ = self._resolve((path or "").strip())
        return resolved

    def read_file(self, path: str) -> str:
        """Return the full content of ``path``, resolving near-misses.

        Postconditions:
            - An exact path match returns that file's full content.
            - The ``<existing codebase>`` pseudo-path returns the existing-code
              excerpt.
            - A path that uniquely matches one file by suffix (the model often
              cites ``main.py`` for ``app/main.py``) returns that file; an
              ambiguous or absent path returns an ``Error: ...`` string (never
              raises) so a bad tool argument degrades to a message rather than
              aborting the verification.
        """
        key = (path or "").strip()
        if not key:
            return "Error: no path provided."
        resolved, hits = self._resolve(key)
        if resolved == self.EXISTING_CODEBASE_PATH:
            return self.existing_codebase
        if resolved is not None:
            return self.files[resolved]
        # Resolution failed — give the tool a message that distinguishes an
        # ambiguous citation from an absent one (and a missing excerpt).
        if key == self.EXISTING_CODEBASE_PATH:
            return "Error: no existing-codebase excerpt available."
        if len(hits) > 1:
            return (
                f"Error: path '{path}' is ambiguous; it matches "
                f"{', '.join(sorted(hits))}. Use list_files() and read the exact path."
            )
        return f"Error: file not found: {path}. Use list_files() to see available paths."

    def search(
        self, query: str, max_matches: int = _SEARCH_MATCH_LIMIT
    ) -> List[Tuple[str, int, str]]:
        """Find a case-insensitive substring across all files.

        Preconditions:
            - ``max_matches`` > 0.

        Postconditions:
            - Returns ``(path, 1-based-line-number, line-text)`` tuples for the
              first ``max_matches`` occurrences in path then line order; the
              existing-codebase excerpt is searched last under its pseudo-path.
            - A blank query returns no matches (a substring search for "" would
              match every line and is never a useful false-positive check).
        """
        assert max_matches > 0, "max_matches must be positive"
        needle = (query or "").strip().lower()
        if not needle:
            return []
        results: List[Tuple[str, int, str]] = []
        for path, content in self._readable_sources():
            for lineno, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    results.append((path, lineno, line.rstrip()))
                    if len(results) >= max_matches:
                        return results
        return results


def _find_python_function_at_line(content: str, line_number: int, path: str) -> str:
    """Find the innermost function/method/class containing ``line_number`` via AST.

    Preconditions:
        - ``content`` is a non-empty string.
        - ``line_number`` >= 1.
        - ``path`` is a non-empty string used only for display.

    Postconditions:
        - Returns a human-readable description of the innermost enclosing
          ``FunctionDef``, ``AsyncFunctionDef``, or ``ClassDef`` node that
          brackets ``line_number`` (start and end line inclusive; the start
          is the earliest decorator line when decorators are present).
        - Returns a "module level" message when no enclosing construct is found.
        - Returns a parse-error message and never raises on ``SyntaxError`` or
          any other ``ast.parse`` failure so the caller can fall back gracefully.
        - Requires Python 3.8+ for ``ast.AST.end_lineno``; nodes without
          ``end_lineno`` are skipped (not possible on the project's Python 3.10
          target, but handled defensively via ``getattr``).
    """
    try:
        tree = ast.parse(content)
    except Exception as exc:
        return (
            f"Could not parse {path} as Python ({type(exc).__name__}: {exc}); "
            "use read_file to inspect the full file manually."
        )

    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_line = getattr(node, "end_lineno", None)
        if end_line is None:
            continue
        start_line = node.lineno
        for dec in node.decorator_list:
            start_line = min(start_line, dec.lineno)
        if start_line <= line_number <= end_line:
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            candidates.append((end_line - start_line, start_line, end_line, node.name, kind))

    if not candidates:
        return (
            f"Line {line_number} of {path} is at module level "
            "(no enclosing function, method, or class found)."
        )

    # Smallest span → innermost enclosing construct.
    _, start_line, end_line, name, kind = min(candidates)
    return f"Line {line_number} is inside {kind} '{name}' ({path} lines {start_line}–{end_line})."


def _find_heuristic_function_at_line(content: str, line_number: int, path: str) -> str:
    """Guess the enclosing construct for ``line_number`` using column-0 heuristics.

    Scans from the first line up to ``line_number`` and returns the start line of
    the last column-0 declaration found — the same heuristic used by
    ``code_boundaries._heuristic_break_lines`` for chunk splitting. Useful for
    TypeScript, JavaScript, Go, and other non-Python languages.

    Preconditions:
        - ``content`` is a non-empty string.
        - ``line_number`` >= 1.
        - ``path`` is a non-empty string used only for display.

    Postconditions:
        - Returns the best-guess start line and advises using ``read_file`` for
          the precise construct name.
        - Returns a "no construct found" message (never raises) when no
          column-0 declaration precedes ``line_number``.
    """
    _SKIP = ("}", ")", "]", "*/", "/*", "//", "#", "*")
    best_start: Optional[int] = None
    for i, line in enumerate(content.splitlines(), start=1):
        if i > line_number:
            break
        if not line or not line.strip():
            continue
        if line[0].isspace():
            continue
        if line.startswith(_SKIP):
            continue
        best_start = i

    if best_start is None:
        return (
            f"Could not identify an enclosing construct for line {line_number} of {path} "
            "(no column-0 declaration found before that line). "
            "Use read_file to inspect the full file."
        )
    return (
        f"Line {line_number} of {path} appears to be inside the construct "
        f"starting at line {best_start}. "
        "Use read_file to see the full construct name and body."
    )


def _build_tools(index: CodebaseIndex) -> list:
    """Build strands tools bound to ``index`` for one verification agent.

    Postconditions:
        - Returns four tools (``read_file``, ``list_files``, ``search_codebase``,
          ``find_function_at_line``) that delegate to ``index``; each returns a
          string and never raises, so a bad model-supplied argument becomes a
          tool message rather than an error that aborts the agent loop.
    """

    @tool
    def read_file(path: str) -> str:
        """Read the full contents of a file in the code under review.

        Use this to inspect the real code a finding refers to (and any related
        code), instead of trusting the finding. Pass the exact path from
        list_files() when possible.

        Args:
            path: The file path to read (e.g. "app/main.py"). The special path
                "<existing codebase>" returns the pre-existing-code excerpt.

        Returns:
            The file's full text, or an "Error: ..." message if the path is
            unknown or ambiguous.
        """
        return index.read_file(path)

    @tool
    def list_files() -> str:
        """List every file path available to read in the code under review.

        Returns:
            One path per line. Read any of them with read_file(path).
        """
        paths = index.list_files()
        return "\n".join(paths) if paths else "(no files available)"

    @tool
    def search_codebase(query: str) -> str:
        """Search every file for a substring (case-insensitive).

        Use this to find where a symbol is defined, imported, registered, used,
        or tested before deciding whether a finding is real — e.g. search for a
        function name a finding claims is "never defined".

        Args:
            query: The substring to search for (e.g. a function or class name).

        Returns:
            Matching "path:line: text" lines, or a message that nothing matched.
        """
        matches = index.search(query)
        if not matches:
            return f"No matches for {query!r}."
        return "\n".join(f"{path}:{lineno}: {text}" for path, lineno, text in matches)

    @tool
    def find_function_at_line(path: str, line_number: int) -> str:
        """Identify which function, method, or class contains a specific line number.

        Use this when a finding cites a line number and you need to know its
        enclosing construct — instead of reading the file in incremental sections
        or expanding a search range one step at a time.

        Args:
            path: The file path to inspect (same paths accepted by read_file).
            line_number: The 1-based line number to locate.

        Returns:
            The name and line range of the innermost enclosing function, method,
            or class (Python files), or the start line of the best-guess enclosing
            construct (all other languages). Returns an error string if the path
            is not readable; never raises.
        """
        content = index.read_file(path)
        if content.startswith("Error:"):
            return content
        resolved = index.resolve_path(path)
        display_path = resolved if resolved and resolved != index.EXISTING_CODEBASE_PATH else path
        _, ext = os.path.splitext(display_path)
        if ext.lower() in (".py", ".pyi"):
            return _find_python_function_at_line(content, line_number, display_path)
        return _find_heuristic_function_at_line(content, line_number, display_path)

    return [read_file, list_files, search_codebase, find_function_at_line]


@dataclass
class _Verdict:
    """One verifier verdict for a single finding.

    Invariants:
        - ``is_false_positive`` is True only when the verifier explicitly judged
          the finding NOT a real issue with ``"high"`` or ``"medium"``
          confidence; every other shape (real, low/blank/missing or unrecognized
          confidence) leaves it False so the finding is kept.
    """

    is_false_positive: bool = False
    confidence: str = ""
    reasoning: str = ""


def _coerce_verdict(item: object) -> Optional[Tuple[int, _Verdict]]:
    """Parse one raw verdict dict into ``(index, _Verdict)``, or None.

    Postconditions:
        - Returns None for any item without a parseable integer ``index`` (a
          verdict we cannot map back to a finding is ignored, not guessed).
        - ``is_false_positive`` is True only for ``is_real_issue is False`` with
          an explicit ``"high"`` or ``"medium"`` confidence; every other shape —
          real, low/blank/missing confidence, OR any unrecognized confidence
          value — is kept. The allowlist is deliberate: an off-contract
          confidence is an ambiguous verdict, and the fail-safe rule keeps
          ambiguous findings rather than dropping them. Never raises on
          malformed input.
    """
    if not isinstance(item, dict):
        return None
    raw_index = item.get("index")
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return None
    confidence = str(item.get("confidence", "") or "").strip().lower()
    is_real = item.get("is_real_issue")
    # Drop ONLY on an explicit, confident "not a real issue". An allowlist (not a
    # denylist) so an unrecognized confidence ("none", "unsure", a non-string the
    # model returned, ...) is treated as not-confident and the finding is kept —
    # dropping a real issue is far worse than keeping a questionable one.
    is_false_positive = is_real is False and confidence in ("high", "medium")
    return index, _Verdict(
        is_false_positive=is_false_positive,
        confidence=confidence,
        reasoning=str(item.get("reasoning", "") or "").strip(),
    )


def _parse_verdicts(data: object, count: int) -> Dict[int, _Verdict]:
    """Map a verifier reply to ``{finding_index: _Verdict}`` for indices in range.

    Postconditions:
        - Returns verdicts only for integer indices in ``[0, count)``; a verdict
          referencing an out-of-range index is dropped (it cannot be mapped to a
          finding this call was asked about).
        - A non-dict reply, or one without a list ``verdicts``, yields ``{}`` so
          the caller keeps every finding in the group.
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}
    verdicts: Dict[int, _Verdict] = {}
    for item in raw:
        parsed = _coerce_verdict(item)
        if parsed is None:
            continue
        index, verdict = parsed
        if 0 <= index < count:
            verdicts[index] = verdict
    return verdicts


def _code_fence_for(content: str) -> str:
    """Return a backtick fence that ``content`` cannot close prematurely.

    A run of backticks inside the inlined file body (common in markdown, docs,
    or docstrings that themselves contain ``` fences) would otherwise close the
    surrounding code block early and garble the prompt's structure. CommonMark
    closes a fenced block only on a fence of at least as many backticks as the
    opener, so a fence one backtick longer than the longest run in ``content``
    is immune.

    Postconditions:
        - Returns a string of at least three backticks (the usual fence).
        - Its length strictly exceeds the longest run of consecutive backticks
          in ``content``, so wrapping ``content`` in this fence cannot be
          terminated from inside.
    """
    longest = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _render_finding_block(i: int, issue: CodeReviewIssue) -> List[str]:
    """Render one indexed finding block (anchor line + metadata) for the prompt.

    Postconditions:
        - Returns the lines for finding ``i``: an ``--- Finding index i ---``
          anchor the verdict contract refers back to, a severity/category/
          location line, the description, and the suggestion when present.
    """
    location = issue.file_path or "(file unknown)"
    if issue.line is not None:
        location = f"{location}:{issue.line}"
    block = [
        f"--- Finding index {i} ---",
        f"severity: {issue.severity} | category: {issue.category} | location: {location}",
        f"description: {issue.description}",
    ]
    if issue.suggestion:
        block.append(f"suggestion: {issue.suggestion}")
    return block


def _build_group_prompt(
    index: CodebaseIndex,
    file_path: str,
    issues: List[CodeReviewIssue],
    input_data: CodeReviewInput,
    max_inline_chars: int,
) -> str:
    """Render the user prompt for verifying one file's findings.

    The prompt inlines the cited file's full content up to ``max_inline_chars``
    (so the model has the primary evidence even without a tool call) and lists
    the other available paths; everything beyond the budget — the rest of a huge
    file, every other file, the existing-codebase excerpt — is reachable through
    the tools. The wording is a stable anchor for the verdict contract: it names
    the file, indexes each finding, and asks for a ``verdicts`` array.

    Postconditions:
        - The returned text contains one indexed block per finding (index 0..n-1
          matching ``issues`` order) and never exceeds the inline budget for the
          primary file body. The task description and each acceptance criterion
          are capped at ``_CONTEXT_FIELD_CHARS`` so an oversized task field can
          never dominate the prompt or overflow context.
    """
    parts: List[str] = []
    task = input_data.task_description.strip()[:_CONTEXT_FIELD_CHARS]
    if task:
        parts.append(f"**Task being implemented:** {task}")
    if input_data.acceptance_criteria:
        parts.append("**Acceptance criteria:**")
        parts.extend(f"- {c[:_CONTEXT_FIELD_CHARS]}" for c in input_data.acceptance_criteria)
        parts.append("")

    manifest = index.list_files()
    parts.append(
        f"**Files available to read ({len(manifest)} total) — use read_file/search_codebase:**"
    )
    parts.extend(manifest[:_MANIFEST_LIMIT])
    if len(manifest) > _MANIFEST_LIMIT:
        parts.append(f"... and {len(manifest) - _MANIFEST_LIMIT} more (call list_files()).")
    parts.append("")

    body = index.read_file(file_path)
    inlined = body[:max_inline_chars]
    truncated = len(body) > max_inline_chars
    fence = _code_fence_for(inlined)
    parts.append(f"**Full content of `{file_path}` (the file the findings below are about):**")
    parts.append(fence)
    parts.append(inlined)
    parts.append(fence)
    if truncated:
        parts.append(
            f"(Only the first {max_inline_chars} characters of `{file_path}` are shown above; "
            "call read_file to see the rest.)"
        )
    parts.append("")

    parts.append(
        "**Findings to check for false positives.** For EACH finding, look at the real code "
        "(use read_file/search_codebase to inspect this file and any related file — where a symbol "
        "is defined, imported, registered, used, or tested) and decide whether it is a real issue "
        "or a false positive:"
    )
    for i, issue in enumerate(issues):
        parts.extend(_render_finding_block(i, issue))
    parts.append("")
    parts.append(
        'Return a JSON object with a "verdicts" array containing exactly one verdict per finding '
        "index above. Mark is_real_issue=false ONLY when you have confirmed from the actual code "
        "that the finding does not hold; otherwise keep it (is_real_issue=true). Be conservative — "
        "dropping a real issue is worse than keeping a questionable one."
    )
    return "\n".join(parts)


def _verify_group(
    model: _StrandsModel,
    index: CodebaseIndex,
    file_path: str,
    issues: List[CodeReviewIssue],
    input_data: CodeReviewInput,
    max_inline_chars: int,
) -> Dict[int, _Verdict]:
    """Run one verification LLM call over all findings for a single file.

    Postconditions:
        - Returns ``{finding_index: _Verdict}`` for the findings the model gave
          a parseable, in-range verdict on; findings with no verdict are absent
          (and therefore kept by the caller).
    """
    prompt = _build_group_prompt(index, file_path, issues, input_data, max_inline_chars)
    agent = Agent(
        model=model,
        system_prompt=FALSE_POSITIVE_VERIFY_PROMPT,
        tools=_build_tools(index),
    )
    raw = str(agent(prompt)).strip()
    data = json.loads(raw)
    return _parse_verdicts(data, len(issues))


def filter_false_positives(
    llm: LLMClient,
    input_data: CodeReviewInput,
    issues: List[CodeReviewIssue],
) -> List[CodeReviewIssue]:
    """Return ``issues`` minus the ones a full-codebase re-check confirms are false.

    Each finding is re-examined against the whole submission (not the single
    chunk that produced it) by an agent with read access to every file under
    review. This is the step that lets the reviewer "review all relevant code in
    the codebase" before standing behind a finding.

    Preconditions:
        - ``issues`` are genuine reviewer findings only — coverage/safety
          findings (not-reviewed, empty-file) must be excluded by the caller, as
          they are never candidates for removal.

    Postconditions:
        - Returns a list that is ``issues`` with zero or more entries removed;
          the surviving entries are the exact same objects in their original
          relative order (nothing is added, reordered, or mutated).
        - A finding is removed ONLY when the verifier returned an explicit,
          non-low-confidence false-positive verdict for it. A finding with a
          blank file path, a path absent from the submission, an unparsable
          verdict, or any error is kept (fail-safe).
        - Returns ``issues`` unchanged (no LLM call) when the filter is disabled
          via ``CODE_REVIEW_FALSE_POSITIVE_FILTER``, when no finding has a file
          path, or when the submission exposes no readable files.
        - Never raises: any setup failure (index build, model resolution,
          context sizing) or per-group verification failure logs a warning and
          keeps the affected findings, so verification can never break the
          review.
    """
    if not env_flag_enabled(_FILTER_ENV):
        return list(issues)

    verifiable = [i for i in issues if (i.file_path or "").strip()]
    if not verifiable:
        return list(issues)

    try:
        return _verify_and_filter(llm, input_data, issues, verifiable)
    except Exception as exc:  # noqa: BLE001 - fail-safe: verification must never break the review
        logger.warning(
            "FalsePositiveFilter: verification failed during setup (%s: %s); keeping all findings",
            type(exc).__name__,
            exc,
        )
        return list(issues)


def _verify_and_filter(
    llm: LLMClient,
    input_data: CodeReviewInput,
    issues: List[CodeReviewIssue],
    verifiable: List[CodeReviewIssue],
) -> List[CodeReviewIssue]:
    """Core of :func:`filter_false_positives`; may raise on setup errors.

    Split out so its sole caller can wrap it in the fail-safe guard: model
    resolution and context sizing happen here (outside the per-group loop) and
    can raise, and the caller turns any such error into "keep all findings".

    Preconditions:
        - ``verifiable`` is the subset of ``issues`` with a non-blank file path
          (already computed by the caller).

    Postconditions:
        - Same removal contract as :func:`filter_false_positives`, minus the
          env-toggle and blank-path early returns the caller already handled.
    """
    index = CodebaseIndex.from_input(input_data)
    if not index.files:
        # No readable submission files — the legacy ``code`` blob had no
        # path-headed content. We cannot show the verifier any real code, so we
        # cannot responsibly drop anything.
        return list(issues)

    model = resolve_code_review_model(llm)
    max_inline_chars = compute_code_review_map_chunk_chars(llm)

    # Group findings by the resolved canonical path of their cited file so each
    # verification call shares one real file's context (and can still read any
    # other file via the tools). A finding whose cited file is absent from the
    # submission (or is ambiguous) is kept without a verification call: the
    # verifier would have no primary file to read, so the call would inline an
    # error string, waste an LLM round, and still keep the finding (fail-safe).
    groups: OrderedDict[str, List[CodeReviewIssue]] = OrderedDict()
    for issue in verifiable:
        resolved = index.resolve_path(issue.file_path)
        if resolved is None:
            logger.debug(
                "FalsePositiveFilter: keeping finding for unresolved path %r (not in submission)",
                issue.file_path,
            )
            continue
        groups.setdefault(resolved, []).append(issue)

    # Each group is an independent verification LLM call over the same read-only
    # index, so they fan out: with N cited files the wall-clock is the slowest
    # single call, not the sum. A per-group failure keeps that group's findings
    # (best-effort), exactly as the sequential path did, and the merge below
    # consumes results in submission order so the outcome stays deterministic.
    group_items = list(groups.items())

    def _verify_one(item: Tuple[str, List[CodeReviewIssue]]) -> Dict[int, _Verdict]:
        file_path, group = item
        try:
            return _verify_group(model, index, file_path, group, input_data, max_inline_chars)
        except Exception as exc:  # noqa: BLE001 - best-effort; a failure must keep findings, not drop them
            logger.warning(
                "FalsePositiveFilter: verification failed for %s (%s: %s); keeping its findings",
                file_path,
                type(exc).__name__,
                exc,
            )
            return {}

    workers = min(_verify_parallelism(), len(group_items))
    if workers <= 1:
        group_verdicts = [_verify_one(item) for item in group_items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            group_verdicts = list(executor.map(_verify_one, group_items))

    removed: set[int] = set()
    for (_file_path, group), verdicts in zip(group_items, group_verdicts):
        for idx, verdict in verdicts.items():
            if verdict.is_false_positive:
                issue = group[idx]
                removed.add(id(issue))
                logger.info(
                    "FalsePositiveFilter: dropping false positive [%s] %s:%s — %s (%s)",
                    issue.severity,
                    issue.file_path,
                    issue.line if issue.line is not None else "-",
                    issue.description[:120],
                    verdict.reasoning[:160] or "no reasoning given",
                )

    if not removed:
        return list(issues)
    kept = [i for i in issues if id(i) not in removed]
    logger.info(
        "FalsePositiveFilter: removed %s of %s findings as false positives",
        len(issues) - len(kept),
        len(issues),
    )
    return kept
