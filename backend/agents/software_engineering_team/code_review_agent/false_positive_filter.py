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
``search_codebase``), so it can pull up exactly the code needed to confirm or
refute a finding rather than guessing from a single chunk.

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

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from strands import Agent, tool
from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, get_strands_model
from shared_env import env_flag_enabled
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

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

    def list_files(self) -> List[str]:
        """Return every readable path, the existing-codebase pseudo-path last.

        Postconditions:
            - The submission's own files come first in insertion order; the
              ``<existing codebase>`` pseudo-path is appended only when a
              non-blank existing-codebase excerpt was provided.
        """
        paths = list(self.files.keys())
        if self.existing_codebase.strip():
            paths.append(self.EXISTING_CODEBASE_PATH)
        return paths

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
        if key == self.EXISTING_CODEBASE_PATH:
            return self.existing_codebase or "Error: no existing-codebase excerpt available."
        if key in self.files:
            return self.files[key]
        # Suffix match: a unique file whose path ends with the cited fragment.
        normalized = key.lstrip("./")
        suffix_hits = [p for p in self.files if p == normalized or p.endswith("/" + normalized)]
        if len(suffix_hits) == 1:
            return self.files[suffix_hits[0]]
        if len(suffix_hits) > 1:
            return (
                f"Error: path '{path}' is ambiguous; it matches "
                f"{', '.join(sorted(suffix_hits))}. Use list_files() and read the exact path."
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
        sources = list(self.files.items())
        if self.existing_codebase.strip():
            sources.append((self.EXISTING_CODEBASE_PATH, self.existing_codebase))
        for path, content in sources:
            for lineno, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    results.append((path, lineno, line.rstrip()))
                    if len(results) >= max_matches:
                        return results
        return results


def _build_tools(index: CodebaseIndex) -> list:
    """Build strands tools bound to ``index`` for one verification agent.

    Postconditions:
        - Returns three tools (``read_file``, ``list_files``, ``search_codebase``)
          that delegate to ``index``; each returns a string and never raises, so
          a bad model-supplied argument becomes a tool message rather than an
          error that aborts the agent loop.
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

    return [read_file, list_files, search_codebase]


@dataclass
class _Verdict:
    """One verifier verdict for a single finding.

    Invariants:
        - ``is_false_positive`` is True only when the verifier explicitly judged
          the finding NOT a real issue with non-low confidence; every other
          shape (real, low confidence, missing fields) leaves it False so the
          finding is kept.
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
          a confidence that is not ``"low"`` (and not blank); everything else is
          kept. Never raises on malformed input.
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
    # Drop only on an explicit, confident "not a real issue". A missing/None
    # is_real_issue, or low/blank confidence, keeps the finding.
    is_false_positive = is_real is False and confidence not in ("", "low")
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
          primary file body.
    """
    parts: List[str] = []
    if input_data.task_description.strip():
        parts.append(f"**Task being implemented:** {input_data.task_description.strip()}")
    if input_data.acceptance_criteria:
        parts.append("**Acceptance criteria:**")
        parts.extend(f"- {c}" for c in input_data.acceptance_criteria)
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
    parts.append(f"**Full content of `{file_path}` (the file the findings below are about):**")
    parts.append("```")
    parts.append(inlined)
    parts.append("```")
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
        location = issue.file_path or "(file unknown)"
        if issue.line is not None:
            location = f"{location}:{issue.line}"
        parts.append(f"--- Finding index {i} ---")
        parts.append(
            f"severity: {issue.severity} | category: {issue.category} | location: {location}"
        )
        parts.append(f"description: {issue.description}")
        if issue.suggestion:
            parts.append(f"suggestion: {issue.suggestion}")
    parts.append("")
    parts.append(
        'Return a JSON object with a "verdicts" array containing exactly one verdict per finding '
        "index above. Mark is_real_issue=false ONLY when you have confirmed from the actual code "
        "that the finding does not hold; otherwise keep it (is_real_issue=true). Be conservative — "
        "dropping a real issue is worse than keeping a questionable one."
    )
    return "\n".join(parts)


def _resolve_model(llm: LLMClient):
    """Resolve the strands model the verification agent runs on.

    Postconditions:
        - Returns ``llm`` itself when it implements the strands ``Model``
          interface (the test path injects such a client); otherwise the shared
          ``get_strands_model("code_review")`` (production), mirroring how
          ``chunk_reviewer`` and ``synthesis`` resolve their model.
    """
    return llm if isinstance(llm, _StrandsModel) else get_strands_model("code_review")


def _verify_group(
    model,
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
        - Never raises: a per-group verification failure logs a warning and keeps
          that group's findings, so verification can never break the review.
    """
    if not env_flag_enabled(_FILTER_ENV):
        return list(issues)

    verifiable = [i for i in issues if (i.file_path or "").strip()]
    if not verifiable:
        return list(issues)

    index = CodebaseIndex.from_input(input_data)
    if not index.files:
        # No readable submission files — the legacy ``code`` blob had no
        # path-headed content. We cannot show the verifier any real code, so we
        # cannot responsibly drop anything.
        return list(issues)

    model = _resolve_model(llm)
    max_inline_chars = compute_code_review_map_chunk_chars(llm)

    # Group findings by their cited file so each verification call shares one
    # file's context (and can still read any other file via the tools).
    groups: "OrderedDict[str, List[CodeReviewIssue]]" = OrderedDict()
    for issue in verifiable:
        groups.setdefault(issue.file_path, []).append(issue)

    removed: set[int] = set()
    for file_path, group in groups.items():
        try:
            verdicts = _verify_group(model, index, file_path, group, input_data, max_inline_chars)
        except Exception as exc:  # noqa: BLE001 - best-effort; a failure must keep findings, not drop them
            logger.warning(
                "FalsePositiveFilter: verification failed for %s (%s: %s); keeping its findings",
                file_path,
                type(exc).__name__,
                exc,
            )
            continue
        for idx, verdict in verdicts.items():
            if verdict.is_false_positive:
                removed.add(id(group[idx]))
                logger.info(
                    "FalsePositiveFilter: dropping false positive [%s] %s:%s — %s (%s)",
                    group[idx].severity,
                    group[idx].file_path,
                    group[idx].line if group[idx].line is not None else "-",
                    group[idx].description[:120],
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
