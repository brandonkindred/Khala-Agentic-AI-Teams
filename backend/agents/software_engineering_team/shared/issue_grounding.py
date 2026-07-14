"""Ground LLM-fallback review findings against real task content.

``run_llm_review`` can invent proper nouns and file paths that appear nowhere in
the task's requirements, acceptance criteria, spec, architecture overview, or
submitted file names. This module blanks bad file anchors and drops findings
whose checkable phrases are absent from that corpus — narrow enough to spare
phrase-free legitimate findings, strict enough to break hallucination fix loops.

Public API:
    - ``extract_checkable_phrases(text)`` — Title Case / quoted claims to check.
    - ``ground_issue_file_path(file_path, files)`` — blank unknown file anchors.
    - ``drop_ungrounded_issues(...)`` — blank bad paths, then drop issues whose
      checkable phrases are absent from the task grounding corpus.

Contracts:
    - All public functions are fail-safe: they never raise on unexpected input.
    - ``drop_ungrounded_issues`` does not mutate its input iterable or the issue
      objects it keeps (path updates use copies); it returns a new list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import is_dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

IssueT = TypeVar("IssueT")

__all__ = [
    "extract_checkable_phrases",
    "ground_issue_file_path",
    "drop_ungrounded_issues",
]

# Multi-word Title Case runs only (e.g. "Insurance Provider") — single tokens
# like ``Index`` or ``HTML`` are skipped to avoid normal code identifiers.
_TITLE_CASE_PHRASE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
# Quoted substrings — non-greedy, no nested quotes.
_QUOTED_PHRASE = re.compile(r"""['"]([^'"]+)['"]""")


def extract_checkable_phrases(text: str) -> List[str]:
    """Extract multi-word Title Case and quoted phrases from ``text``.

    Preconditions:
        - ``text`` is a string (may be empty).

    Postconditions:
        - Returns unique phrases in first-seen order.
        - Never includes a single capitalized token by itself.
        - Never raises.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for match in _TITLE_CASE_PHRASE.finditer(text):
        phrase = match.group(1)
        if phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    for match in _QUOTED_PHRASE.finditer(text):
        phrase = match.group(1).strip()
        if phrase and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def ground_issue_file_path(file_path: str, files: Dict[str, str]) -> str:
    """Return a path that exists in ``files``, or blank if it does not.

    Preconditions:
        - ``files`` maps submitted file paths to content.
        - ``file_path`` is a string (may be blank).

    Postconditions:
        - Exact key match → that key.
        - Unique basename/suffix alias (e.g. ``main.py`` for ``src/main.py``) →
          the real key. Backslashes are normalized to ``/`` before matching so
          Windows-style citations still resolve.
        - Blank, absent, or ambiguous path → ``""``.
        - Never raises.
    """
    key = (file_path or "").strip()
    if not key:
        return ""
    if key in files:
        return key
    # Forward-slash normalize so Windows-style citations still resolve.
    normalized = key.replace("\\", "/").lstrip("./")
    hits = [
        p
        for p in files
        if (pn := p.replace("\\", "/").lstrip("./")) == normalized
        or pn.endswith("/" + normalized)
    ]
    if len(hits) == 1:
        return hits[0]
    return ""


def _flatten_ws(text: str) -> str:
    """Collapse runs of whitespace (including newlines) to a single space."""
    return " ".join((text or "").split())


def _build_grounding_corpus(
    *,
    files: Dict[str, str],
    requirements: str,
    acceptance_criteria: Sequence[str] | None,
    spec_content: str,
    architecture_context: str,
) -> str:
    # Space-join (not newlines) so a Title Case phrase that was split across
    # lines in requirements/spec still matches as a substring.
    parts: List[str] = [
        _flatten_ws(requirements or ""),
        _flatten_ws(
            " ".join(
                stripped
                for a in (acceptance_criteria or ())
                if a is not None
                for stripped in (str(a).strip(),)
                if stripped
            )
        ),
        _flatten_ws(spec_content or ""),
        _flatten_ws(architecture_context or ""),
        _flatten_ws(" ".join(files.keys())),
    ]
    return " ".join(p for p in parts if p).lower()


def _with_file_path(issue: IssueT, file_path: str) -> IssueT:
    """Return a copy of ``issue`` with ``file_path`` updated when possible.

    Postconditions:
        - Never mutates ``issue`` in place.
        - When the issue type cannot be copied (no ``model_copy`` / not a
          dataclass), logs a warning and returns ``issue`` unchanged.
    """
    if getattr(issue, "file_path", None) == file_path:
        return issue
    model_copy = getattr(issue, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"file_path": file_path})
    if is_dataclass(issue) and not isinstance(issue, type):
        return replace(issue, file_path=file_path)
    logger.warning(
        "Cannot copy issue %r of type %s to update file_path; returning unchanged",
        issue,
        type(issue).__name__,
    )
    return issue


def drop_ungrounded_issues(
    issues: Iterable[IssueT],
    *,
    files: Dict[str, str],
    requirements: str,
    acceptance_criteria: Sequence[str] | None,
    spec_content: str,
    architecture_context: str = "",
    on_dropped: Optional[Callable[[Any], None]] = None,
) -> List[IssueT]:
    """Blank bad paths and drop findings with ungrounded checkable phrases.

    Named for the drop step (the hallucination-loop fix); path blanking is the
    same fail-safe posture as ``_validate_findings`` and runs first on every
    issue that is kept.

    Preconditions:
        - ``files`` is the submitted path→content map (keys are grounding sources;
          contents are not).
        - ``issues`` are duck-typed objects with optional ``description``,
          ``recommendation``, and ``file_path`` attributes.

    Postconditions:
        - A non-blank ``file_path`` absent from ``files`` (including aliases) is
          blanked; the issue is kept unless content grounding also fails.
        - An issue is dropped only when description or recommendation contains at
          least one checkable phrase that does not appear (case-insensitive
          substring) in requirements, acceptance criteria, spec, architecture
          context, or ``files.keys()``.
        - Phrase-free issues always keep.
        - ``on_dropped``, when provided, is called with the issue (after path
          blanking) for each drop.
        - Never raises — on unexpected errors the issue is kept (fail-open).
    """
    corpus = _build_grounding_corpus(
        files=files,
        requirements=requirements,
        acceptance_criteria=acceptance_criteria,
        spec_content=spec_content,
        architecture_context=architecture_context,
    )
    kept: List[IssueT] = []
    for issue in issues:
        try:
            raw_path = getattr(issue, "file_path", "") or ""
            grounded_path = ground_issue_file_path(raw_path, files)
            issue = _with_file_path(issue, grounded_path)

            description = getattr(issue, "description", "") or ""
            recommendation = getattr(issue, "recommendation", "") or ""
            phrases = extract_checkable_phrases(f"{description}\n{recommendation}")
            if phrases and any(phrase.lower() not in corpus for phrase in phrases):
                if on_dropped is not None:
                    on_dropped(issue)
                continue
            kept.append(issue)
        except Exception as exc:
            logger.warning(
                "issue grounding failed open (keeping issue): %s",
                exc,
                exc_info=True,
            )
            kept.append(issue)
    return kept
