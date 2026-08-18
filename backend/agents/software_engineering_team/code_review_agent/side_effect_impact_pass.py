"""Side-effect / blast-radius pass for code review.

The map-reduce reviewer (``coordinator.py``) flags issues from *bounded
chunks*, and even the checklist item that asks the chunk reviewer to notice a
behavior change in the function/method it is editing (see
``profiles._CODE_REVIEW_CRITERIA`` item 3, Caller Side Effects) has no tools
and cannot see beyond the chunk it was given — it can flag that a function's
contract *looks* like it changed, but it can never know who else in the
codebase calls that function, or whether the new behavior breaks them.
Neither the false-positive
filter nor the architecture-consistency pass answers that question either:
both operate over *this submission's* files (plus, via ``RepoReader``, read
access to the rest of the repository), but neither searches the wider
repository for *callers* of a changed function and checks their assumptions.
This pass adds exactly that.

This pass runs ONCE PER SUBMISSION (never once per chunk), after the
architecture-consistency pass, and is purely additive: it is given the full
content of the changed files, with read access to the rest of the repository
via the same tools the false-positive filter and architecture pass use
(``read_file``, ``read_lines``, ``read_function``, ``list_files``,
``search_codebase``, ``find_function_at_line``, ``find_references``), plus
one new tool this pass introduces, ``search_repository``, which searches
the REST of the repository (beyond the submission) for a substring — the
capability actually needed to find a
changed function's callers outside the diff, which no existing tool provides
(``search_codebase`` is explicitly submission-only). It emits new findings in
two categories:

    - ``"side-effects"`` — a genuine side effect with an unintended logical
      consequence: a changed function's current behavior (return value,
      exceptions, mutation of shared/passed-in state, I/O, ordering/timing)
      breaks a tool-verified caller elsewhere in the system.
    - ``"documentation"`` — a docstring/comment that no longer matches the
      implementation. A stale docstring is a documentation-accuracy problem,
      NOT a side effect; it is reported under its own category rather than
      being mislabeled as ``"side-effects"``.

Invariants:

    - **Additive-only, fail-safe.** This pass can only ever ADD findings; it
      never removes, mutates, or re-judges anything the map phase, the
      false-positive filter, or the architecture-consistency pass already
      produced. Any setup or LLM failure is swallowed and logged, returning
      no additional findings — the same fail-safe posture those passes use.

    - **One call with reactive bisect recovery.** Agent construction and
      overflow recovery (file-list bisect only; no character truncation) are
      owned by the shared
      :func:`~code_review_agent.submission_pass_runner.run_submission_pass`
      runner; this module supplies only its system prompt, tool set, and
      prompt/parse callbacks. Prompts inline full file content — there is no
      character packing.

    - **``CODE_REVIEW`` profile only.** The other :class:`.profiles.ReviewProfile`
      values narrow the engine to a specific checklist whose contract expects
      every issue to map to a specific criterion/requirement (e.g. the
      acceptance profile's per-criterion attribution); a side-effects finding
      never maps to one of those, so this pass never runs under any profile
      but the default (mirrors ``architecture_consistency_pass``'s identical
      restriction and rationale).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from strands import tool

from llm_service import LLMClient
from shared.env import env_flag_enabled
from software_engineering_team.shared.llm import extract_json_from_response

from .chunking import _coerce_bool
from .false_positive_filter import CodebaseIndex, _build_tools, _code_fence_for, _make_call_tracker
from .models import CodeReviewInput, CodeReviewIssue, coerce_line, is_no_op_suggestion
from .profiles import ReviewProfile
from .prompts import (
    SIDE_EFFECT_IMPACT_FORMATTING_INSTRUCTIONS,
    build_side_effect_impact_reasoning_system_prompt,
)
from .repo_reader import DEFAULT_MAX_LISTED_FILES, DiskRepoReader, RepoReader
from .side_effect_consolidation import MUTATION_ANALYSIS_ENV, effective_replaced_content
from .submission_pass_runner import FileBatch, run_submission_pass

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS=false``/``0``/``no``
# disables the pass (see docs/ENV_VARS.md). Any other value (or unset) leaves it enabled.
_PASS_ENV = "CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS"

# The mutation-vs-replaced-code contract sub-check's toggle name (and the shared
# ``effective_replaced_content`` gating helper) live in ``side_effect_consolidation``
# -- despite the name, that module has no LLM/tool/Agent dependencies of its own
# (env-var name strings, pure finding-grouping logic, and this one pure helper --
# see its module docstring), not a tail pass. See ``MUTATION_ANALYSIS_ENV``'s
# docstring there for why it lives there: mapping.py's cache fingerprint must be
# able to import it without pulling in a tail-pass module.

_ALLOWED_CATEGORIES = frozenset({"side-effects", "documentation"})
_ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})

# Cap on substring matches ``search_repository`` returns, mirroring
# ``false_positive_filter._SEARCH_MATCH_LIMIT``'s rationale for ``search_codebase``.
_REPO_SEARCH_MATCH_LIMIT = 60

# Cap on how many repository files a single ``search_repository`` call will scan
# when the reader's per-fetch cost is unknown or expensive (e.g. the PR-review
# path's ``GitHubRepoReader``, which caps at ``DEFAULT_MAX_FETCHES = 200`` distinct
# file fetches for the WHOLE review -- coding_team/github_source/repo_reader.py --
# and that one reader instance is shared across the false-positive filter, the
# architecture-consistency pass, and this pass, with this pass running LAST).
# Every scanned file costs one fetch under that reader (no cheaper contains-only
# check exists), so a limit anywhere near the full 200 could single-handedly
# exhaust whatever budget the earlier two passes left, starving every later
# ``read_file``/``search_repository`` call -- including this pass's own further
# tool calls -- for the rest of the review. 40 keeps one call's worst case a small
# fraction of the shared budget while still covering realistic caller searches,
# especially combined with ``search_codebase``, ``list_files``, and the model's
# own targeted ``read_file``/``find_function_at_line`` calls.
_REPO_SEARCH_FILE_SCAN_LIMIT = 40

# Cap used instead of ``_REPO_SEARCH_FILE_SCAN_LIMIT`` when the reader is a
# ``DiskRepoReader`` (the SE-pipeline path): it has no per-file fetch cost, only
# its own ``list_files()`` listing cap (``DEFAULT_MAX_LISTED_FILES``), so the
# GitHub-budget rationale above does not apply to it. Using the low cap there
# anyway is actively harmful, not just conservative: ``DiskRepoReader.list_files()``
# returns paths in fixed sorted (alphabetical) order, so a 40-file cap would
# deterministically scan only the alphabetically-first ~40 non-submission files on
# every call, silently missing nearly every real caller in a repository of any
# realistic size. Bounding this at the reader's own listing cap instead lets a
# disk-backed search cover everything ``list_files()`` can see.
_DISK_REPO_SEARCH_FILE_SCAN_LIMIT = DEFAULT_MAX_LISTED_FILES


def _search_repository(
    index: CodebaseIndex,
    query: str,
    max_matches: int = _REPO_SEARCH_MATCH_LIMIT,
    max_files_scanned: Optional[int] = None,
) -> Tuple[List[Tuple[str, int, str]], bool]:
    """Find a case-insensitive substring across the REST of the repository.

    Complements ``CodebaseIndex.search`` (submission-only) with a repo-wide
    equivalent, backed by ``index.repo_reader``. Finding a changed function's
    callers that live outside the diff is the entire reason this pass exists,
    and the submission-only search cannot reach them.

    Preconditions:
        - ``max_matches`` > 0 and, when given explicitly, ``max_files_scanned`` > 0.

    Postconditions:
        - Returns ``([], False)`` when ``index.repo_reader`` is None or when the
          query is blank.
        - When ``max_files_scanned`` is None (the normal call path), it resolves
          to ``_DISK_REPO_SEARCH_FILE_SCAN_LIMIT`` for a ``DiskRepoReader`` (no
          per-file fetch cost -- bounded only by the reader's own listing cap) or
          ``_REPO_SEARCH_FILE_SCAN_LIMIT`` for any other reader (conservative
          default for an unknown-cost reader, e.g. ``GitHubRepoReader``).
        - Otherwise scans up to the resolved cap's worth of repository paths from
          ``index.repo_reader.list_files()``, skipping any path already a key
          of ``index.files`` (already reachable via ``search_codebase``, so
          scanning it again here would be redundant work), returning up to
          ``max_matches`` ``(path, 1-based-line, line-text)`` tuples in
          list-then-line order, alongside a ``truncated`` flag.
        - ``truncated`` is ``True`` whenever the scan did not actually inspect
          the full content of every candidate path -- the file-scan cap was
          hit, ``max_matches`` was reached first, a candidate file was skipped
          because ``read_file`` raised or returned ``None`` (e.g. a shared
          ``GitHubRepoReader`` fetch budget already exhausted by an earlier
          pass), OR (``DiskRepoReader`` only) the reader's own ``list_files()``
          listing cap was hit, meaning ``paths`` itself omits repository files
          this call never even saw. A caller must not treat an empty result
          list as proof the substring is absent anywhere in the repository
          when ``truncated`` is ``True``; it only proves the substring is
          absent from the files this call actually managed to read.
        - Never raises: a reader ``list_files``/``read_file`` failure is
          logged and treated as "no matches from that path/call" (folded into
          ``truncated``, never dropped silently) -- a broken reader must only
          ever narrow what this search can find, never break the pass.
    """
    assert max_matches > 0, "max_matches must be positive"
    assert max_files_scanned is None or max_files_scanned > 0, "max_files_scanned must be positive"
    if index.repo_reader is None:
        return [], False
    if max_files_scanned is None:
        max_files_scanned = (
            _DISK_REPO_SEARCH_FILE_SCAN_LIMIT
            if isinstance(index.repo_reader, DiskRepoReader)
            else _REPO_SEARCH_FILE_SCAN_LIMIT
        )
    needle = (query or "").strip().lower()
    if not needle:
        return [], False
    try:
        paths = index.repo_reader.list_files()
    except Exception as exc:  # noqa: BLE001 - fail-safe: a reader failure must never raise
        logger.debug("SideEffectImpactPass: repo_reader.list_files() failed: %s", exc)
        return [], True

    results: List[Tuple[str, int, str]] = []
    scanned = 0
    # DiskRepoReader.list_files() has its own listing cap (DEFAULT_MAX_LISTED_FILES),
    # independent of max_files_scanned above -- for a DiskRepoReader the two are set
    # equal (see _DISK_REPO_SEARCH_FILE_SCAN_LIMIT), so a repository with more paths
    # than the listing cap would have every returned path scanned without the
    # per-file-scan cap ever tripping, silently missing the reader's own truncation.
    incomplete = (
        isinstance(index.repo_reader, DiskRepoReader) and index.repo_reader.listing_truncated()
    )
    for path in paths:
        if path in index.files:
            continue
        if scanned >= max_files_scanned:
            return results, True
        scanned += 1
        try:
            content = index.repo_reader.read_file(path)
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not abort the scan
            logger.debug("SideEffectImpactPass: repo_reader.read_file(%r) failed: %s", path, exc)
            incomplete = True
            continue
        if content is None:
            incomplete = True
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if needle in line.lower():
                results.append((path, lineno, line.rstrip()))
                if len(results) >= max_matches:
                    return results, True
    return results, incomplete


def _build_side_effect_tools(index: CodebaseIndex, *, track_call=None) -> list:
    """Build this pass's tools: the shared submission tools plus repo-wide search.

    Preconditions:
        - When given, ``track_call`` is a callable previously returned by
          ``false_positive_filter._make_call_tracker`` -- typically because
          the caller (e.g. ``merged_architecture_side_effect_pass``) is
          layering further tools of its own on top of this set and needs all
          of them to share one run-level budget.

    Postconditions:
        - Returns the seven shared tools from ``false_positive_filter._build_tools``
          (``read_file``, ``read_lines``, ``read_function``, ``list_files``,
          ``search_codebase``, ``find_function_at_line``, ``find_references``)
          plus a new ``search_repository`` tool bound to ``index`` -- the only
          tool in this set whose entire purpose is reaching beyond the
          submission's own files to find a changed function's out-of-diff
          callers. All eight tools share one call tracker (``track_call``
          when given, else one created internally), so the run-level
          duplicate-call/total-budget guard (see
          ``false_positive_filter._make_call_tracker``) actually bounds every
          tool in this set, not just the seven built by ``_build_tools``.
    """
    _track_call = track_call if track_call is not None else _make_call_tracker()

    @tool
    def search_repository(query: str) -> str:
        """Search the REST of the repository (beyond this submission) for a substring.

        Use this to find callers of a changed function/method that live
        outside the diff -- ``search_codebase`` only searches the files shown
        in this prompt. Requires a repository reader to be attached to this
        review; when none is attached, this tool reports that no repository
        access is available beyond the submission.

        Args:
            query: The substring to search for (e.g. a function or class name).

        Returns:
            Matching "path:line: text" lines, or a message that nothing
            matched or that no repository access is available. A truncated
            scan (the repository is larger than this tool's per-call file
            cap) is flagged explicitly rather than reported as if the whole
            repository had been searched -- follow up with targeted
            ``list_files()``/``read_file()`` calls when this matters. A call
            repeated with an identical query beyond
            ``false_positive_filter._MAX_DUPLICATE_TOOL_CALLS`` still returns
            this, with a note appended saying so; once this run's shared
            ``_MAX_TOTAL_TOOL_CALLS`` budget is exhausted, this becomes a
            stop directive instead (see ``false_positive_filter._make_call_tracker``).
        """
        skip, note = _track_call("search_repository", query)
        if skip:
            return note
        if index.repo_reader is None:
            result = "No repository access is available beyond this submission."
        else:
            matches, truncated = _search_repository(index, query)
            if not matches:
                if truncated:
                    result = (
                        f"No matches for {query!r} in the files scanned, but the scan was "
                        "truncated before covering the whole repository -- this does NOT prove "
                        "the substring is absent elsewhere. Use list_files()/read_file() for a "
                        "more targeted follow-up if this caller-impact check matters."
                    )
                else:
                    result = f"No matches for {query!r} in the rest of the repository."
            else:
                result = "\n".join(f"{path}:{lineno}: {text}" for path, lineno, text in matches)
                if truncated:
                    result += (
                        f"\n\n(Scan truncated before covering the whole repository -- there may be "
                        f"more matches for {query!r} beyond what's shown above.)"
                    )
        return f"{result}\n\n{note}" if note else result

    return [*_build_tools(index, track_call=_track_call), search_repository]


def build_side_effect_tools(index: CodebaseIndex, *, track_call=None) -> list:
    """Public wrapper for :func:`_build_side_effect_tools`."""
    return _build_side_effect_tools(index, track_call=track_call)


def _render_manifest(paths: List[str]) -> List[str]:
    """Render the full changed-file path list (no character truncation).

    A small pass-owned duplicate of
    ``merged_architecture_side_effect_pass._render_manifest`` -- that module
    imports this one, so importing the reverse direction would be circular.

    Postconditions: always includes the section header followed by every path.
    """
    return [f"**Changed files in this submission ({len(paths)}):**", *paths]


def _build_prompt(
    index: CodebaseIndex,
    *,
    content_items: Optional[List[Tuple[str, str]]] = None,
    batch_index: Optional[int] = None,
    total_batches: Optional[int] = None,
    is_partial: bool = False,
    replaced_content: Optional[dict] = None,
) -> str:
    """Render the user prompt for one submission-pass runner call.

    Preconditions:
        - ``content_items``, when given, is this call's batch of the changed
          files (a subset of ``index.files.items()``); ``None`` inlines every
          changed file.
        - ``batch_index``/``total_batches`` are both ``None`` (no batch label
          rendered) or both set to this batch's 1-based position and the
          total batch count.
        - ``is_partial`` is True only for a reactive-recovery bisect child
          batch (:attr:`~code_review_agent.submission_pass_runner.FileBatch.is_partial`).
        - ``replaced_content``, when given, is ``CodeReviewInput.replaced_content``
          verbatim (path -> before-image text); not guaranteed to cover every
          path, or to be a complete file body for the paths it does cover.

    Postconditions:
        - The changed-file path manifest lists every changed file in the
          submission (from ``index.files``, not ``content_items``) with no
          truncation.
        - Inlines every file in ``content_items`` (or every changed file when
          ``None``) in full. When ``is_partial`` is True, the content section
          header renders a reduced-view recovery banner; otherwise, when
          ``total_batches`` is set (> 1), it names this batch's position.
        - For each path shown in this call, immediately after that path's
          current-content block, renders a "Replaced (pre-change) content"
          block with ``replaced_content[path]`` when that entry is present and
          non-empty. A path with no entry (or ``replaced_content`` itself
          ``None``/empty) gets no such block -- identical output to omitting
          the parameter entirely.
    """
    parts: List[str] = []

    changed_files = list(index.files.items())
    paths = [path for path, _ in changed_files]
    parts.extend(_render_manifest(paths))
    parts.append("")

    batch_files = content_items if content_items is not None else changed_files
    if is_partial:
        parts.append(
            f"**Content of the changed files shown in this call ({len(batch_files)} of "
            f"{len(changed_files)} changed files in this submission -- a reduced view "
            "produced while recovering from a context-size overflow; any file not shown "
            "here is still listed in the manifest above and reachable via "
            "read_file()/list_files()):**"
        )
    elif total_batches and total_batches > 1:
        parts.append(
            f"**Full content of the changed files (batch {batch_index} of {total_batches} -- "
            f"showing {len(batch_files)} of {len(changed_files)} changed files in this "
            "submission; the rest are listed in the manifest above and reachable via "
            "read_file()/list_files()):**"
        )
    else:
        parts.append("**Full content of the changed files:**")
    for path, content in batch_files:
        body_fence = _code_fence_for(content)
        parts.append(f"### {path} ###")
        parts.append(body_fence)
        parts.append(content)
        parts.append(body_fence)
        replaced = (replaced_content or {}).get(path)
        if replaced:
            replaced_fence = _code_fence_for(replaced)
            parts.append(f"### {path} — Replaced (pre-change) content ###")
            parts.append(replaced_fence)
            parts.append(replaced)
            parts.append(replaced_fence)
    parts.append("")

    parts.append(
        "Use search_codebase()/search_repository()/list_files()/read_file() to find every caller "
        "of any function or method whose behavior this submission changes, before flagging a "
        "caller-impact finding -- search_codebase only searches the files shown above, "
        "search_repository reaches the rest of the repository."
    )
    parts.append(
        "Summarize side-effect-impact findings in structured prose per the system "
        "instructions (severity, category, file_path, line, description, suggestion, "
        "pre_existing). State clearly when you find nothing."
    )
    return "\n".join(parts)


def _coerce_finding(item: object) -> Optional[CodeReviewIssue]:
    """Parse one raw finding dict into a :class:`CodeReviewIssue`, or None.

    Postconditions:
        - Returns None for a non-dict item, an unrecognized/missing
          ``category`` (only ``"side-effects"`` and ``"documentation"`` are
          accepted -- this pass's two axes: a caller-breaking side effect, or
          a docstring/implementation mismatch), a blank ``description`` (an
          unactionable finding is worse than none), or a ``suggestion`` that
          is, in its entirety, a no-op phrasing (e.g. "No changes needed.")
          -- see ``is_no_op_suggestion``. An unrecognized ``severity``
          defaults to ``"medium"`` rather than being dropped. Never raises on
          malformed input.
        - ``pre_existing`` reflects the model's optional per-finding tag
          (coerced via ``chunking._coerce_bool``, tolerating string
          encodings), defaulting to ``False`` when absent -- mirrors
          ``chunking._issues_from_chunk_output``'s identical convention, used
          by the PR-review whole-file path to route a finding about code this
          submission did NOT add or modify to a human-review proposal instead
          of a blocking PR comment (see ``CodeReviewIssue.pre_existing``).
        - ``omission`` is likewise coerced via ``chunking._coerce_bool`` from
          the model's optional per-finding tag, defaulting to ``False`` when
          absent (see ``CodeReviewIssue.omission``).
    """
    if not isinstance(item, dict):
        return None
    category = str(item.get("category", "") or "").strip().lower()
    if category not in _ALLOWED_CATEGORIES:
        return None
    description = str(item.get("description", "") or "").strip()
    if not description:
        return None
    suggestion = str(item.get("suggestion", "") or "").strip()
    if is_no_op_suggestion(suggestion):
        return None
    severity = str(item.get("severity", "") or "").strip().lower()
    if severity not in _ALLOWED_SEVERITIES:
        severity = "medium"
    return CodeReviewIssue(
        severity=severity,
        category=category,
        file_path=str(item.get("file_path", "") or "").strip(),
        line=coerce_line(item.get("line")),
        description=description,
        suggestion=suggestion,
        pre_existing=_coerce_bool(item.get("pre_existing")),
        omission=_coerce_bool(item.get("omission")),
    )


def _parse_findings(data: object) -> List[CodeReviewIssue]:
    """Map the pass's raw JSON reply to a list of new findings.

    Postconditions:
        - Returns ``[]`` for a non-dict reply or one without a list
          ``findings`` (the pass found nothing to add, or replied off
          contract -- both degrade to "nothing new"). Otherwise returns one
          ``CodeReviewIssue`` per item :func:`_coerce_finding` can parse, in
          order; unparseable items are skipped, not raised on.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    return [parsed for item in raw if (parsed := _coerce_finding(item)) is not None]


def parse_findings(data: object) -> List[CodeReviewIssue]:
    """Public wrapper for :func:`_parse_findings`."""
    return _parse_findings(data)


def _validate_finding_line(
    index: CodebaseIndex, file_path: str, line: Optional[int], pre_numbered: bool = False
) -> Optional[int]:
    """Validate a cited line number against the real file it names, or None.

    Mirrors ``architecture_consistency_pass._validate_finding_line`` /
    ``chunking._validate_line``'s guarantee for chunk-review findings: a
    hallucinated citation can never anchor a finding to the wrong (or
    nonexistent) line. This pass has no ``FileSegment`` to bound against (it
    works over whole files), so it bounds against the resolved file's actual
    line count instead -- except under ``pre_numbered`` inputs (e.g. PR hunk
    review mode), where the citation is trusted as-is.

    Postconditions:
        - Under ``pre_numbered``, returns ``line`` unchanged (or None if
          ``line`` is None) without reading the file at all.
        - Returns None when ``line`` is None, ``file_path`` does not resolve to
          a readable file, or the file's content cannot be read (per
          ``index.read_file_or_none``).
        - Returns ``line`` unchanged when it falls within ``[1, total_lines]``
          of the resolved file's content; otherwise returns None. Never raises.
    """
    if line is None:
        return None
    if pre_numbered:
        return line
    resolved = index.resolve_path(file_path)
    if resolved is None:
        return None
    content = index.read_file_or_none(resolved)
    if content is None:
        return None
    total_lines = len(content.splitlines()) or 1
    return line if 1 <= line <= total_lines else None


def _is_changed_file(index: CodebaseIndex, file_path: str) -> bool:
    """True when ``file_path`` is one of the submission's own changed files.

    Postconditions: returns ``False`` for an empty/blank path or one that
    resolves only via ``repo_reader``/the existing-codebase excerpt; ``True``
    iff ``file_path`` resolves (via ``CodebaseIndex.resolve_path``, which also
    accepts a unique basename/suffix alias such as ``main.py`` for
    ``app/main.py``) to a key of ``index.files``. Pure; never raises.
    """
    if not file_path:
        return False
    resolved = index.resolve_path(file_path)
    return resolved is not None and resolved in index.files


def _validate_findings(
    index: CodebaseIndex, findings: List[CodeReviewIssue], pre_numbered: bool = False
) -> List[CodeReviewIssue]:
    """Bounds-check each finding's file/line anchor against the real submission.

    Preconditions:
        - ``pre_numbered`` mirrors ``CodeReviewInput.pre_numbered``: True only
          when the submission's file content already carries original-file
          absolute line-number prefixes (PR hunk review mode).

    Postconditions:
        - A finding whose ``file_path`` is not one of the submission's changed
          files (e.g. the model anchored a caller-impact finding to the
          out-of-diff CALLER file it cited as evidence, rather than to the
          changed file whose behavior actually changed) has its
          ``file_path``/``line`` blanked to ``""``/``None`` -- a PR comment
          cannot attach to a file outside the diff, so this degrades to a
          submission-wide finding rather than pointing at the wrong location.
        - Otherwise ``line`` is replaced by ``None`` wherever it does not fall
          within the cited file's actual line range (a file-wide finding is
          still a valid, useful outcome) -- except under ``pre_numbered``,
          where the citation is trusted as-is.
        - Never drops a finding outright -- only its potentially wrong/hallucinated
          location anchor -- and never raises.
    """
    validated: List[CodeReviewIssue] = []
    for finding in findings:
        if finding.file_path:
            resolved_path = index.resolve_path(finding.file_path)
            if resolved_path is None or resolved_path not in index.files:
                finding = finding.model_copy(update={"file_path": "", "line": None})
                validated.append(finding)
                continue
            if resolved_path != finding.file_path:
                finding = finding.model_copy(update={"file_path": resolved_path})
        checked_line = _validate_finding_line(index, finding.file_path, finding.line, pre_numbered)
        if checked_line != finding.line:
            finding = finding.model_copy(update={"line": checked_line})
        validated.append(finding)
    return validated


def validate_findings(
    index: CodebaseIndex, findings: List[CodeReviewIssue], *, pre_numbered: bool = False
) -> List[CodeReviewIssue]:
    """Public wrapper for :func:`_validate_findings`."""
    return _validate_findings(index, findings, pre_numbered=pre_numbered)


def _effective_pre_numbered(input_data: CodeReviewInput, index: CodebaseIndex) -> bool:
    """True when this pass must treat the submission as bounded/pre-numbered.

    Gates on ``index.full_content_complete`` -- set by ``CodebaseIndex.from_input``
    only when ``input_data.full_content`` covered EVERY path the index holds --
    rather than on ``input_data.full_content`` directly. A caller-supplied
    ``full_content`` that covers only some paths never sets that flag (the
    overlay is all-or-nothing; see ``from_input``), so this correctly keeps
    treating the submission as pre-numbered rather than trusting a
    partially-numbered, partially-full index as if every path were complete.

    Preconditions:
        - ``index`` was built from this same ``input_data`` (``CodebaseIndex.from_input``
          or an equivalent shared build).

    Postconditions:
        - Returns ``input_data.pre_numbered and not index.full_content_complete``.
        - Pure; never raises.
    """
    return input_data.pre_numbered and not index.full_content_complete


def find_side_effect_impact_issues(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> List[CodeReviewIssue]:
    """Run the once-per-submission side-effect / blast-radius pass.

    Preconditions:
        - ``input_data`` is the same review input the coordinator built for
          this submission (its ``files``/``existing_codebase`` back the
          ``CodebaseIndex`` this pass reads from).
        - ``repo_reader`` is None or a read-only, thread-safe
          ``repo_reader.RepoReader`` giving access to the rest of the
          repository beyond the diff (the same object the false-positive
          filter and architecture pass are given).
        - ``index``, when given, must have been built from this same
          ``input_data``/``repo_reader`` (the coordinator shares one index
          across this pass and the others rather than each rebuilding it);
          ``None`` builds a fresh one (here, before the pre-numbered gate
          check below, so :func:`_effective_pre_numbered` can consult it).

    Postconditions:
        - Returns ``[]`` (no LLM call) when the pass is disabled via
          ``CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS``, when ``input_data.profile``
          is anything other than ``ReviewProfile.CODE_REVIEW`` (the other
          profiles have a narrower contract that expects every issue to be
          attributable to a specific criterion/requirement, which a
          side-effects finding never is -- see
          ``architecture_consistency_pass``'s identical restriction), when
          :func:`_effective_pre_numbered` is True (the PR-review hunk-fallback
          mode, used when whole-file fetching is unavailable and no fully-covering
          ``full_content`` was supplied: ``index.files`` then holds partial
          diff-hunk excerpts rendered with original-line-number prefixes, not
          complete file content, and no tool this pass has can retrieve a
          more complete view of a changed file -- ``read_file`` resolves a
          changed path from ``index.files`` first, so it returns the same
          partial excerpt already in the prompt. This pass's entire contract
          is "never flag from a guess"; reasoning about a function's complete
          current behavior from a partial hunk would violate that, risking
          false-positive caller-impact findings on code the pass never
          actually saw in full), or when the submission has no readable
          files. A caller that supplies ``full_content`` covering every
          changed path re-enables this pass (see ``_effective_pre_numbered``
          / ``CodebaseIndex.full_content_complete``); a ``full_content`` that
          covers only some paths does NOT re-enable it -- the pass would
          otherwise reason over a mix of real bodies and bounded excerpts as
          if all were complete.
        - Otherwise returns zero or more NEW ``CodeReviewIssue``s in category
          ``"side-effects"`` (a caller-breaking side effect) or
          ``"documentation"`` (a docstring/implementation mismatch) only, each
          with its cited ``line`` bounds-checked against the real file (a
          hallucinated out-of-range line is nulled to a file-wide finding,
          never trusted verbatim); never mutates or removes any issue the
          caller already has.
        - Never raises: any setup or LLM failure is logged at warning level
          and yields ``[]`` -- this pass can only ever add findings, so a
          failure here must never affect the review already computed by the
          earlier phases.
    """
    if not env_flag_enabled(_PASS_ENV):
        return []
    if input_data.profile != ReviewProfile.CODE_REVIEW:
        return []
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if _effective_pre_numbered(input_data, index):
        return []
    try:
        return _run_pass(llm, input_data, repo_reader, index)
    except Exception as exc:  # noqa: BLE001 - fail-safe: this pass must never break the review
        logger.warning(
            "SideEffectImpactPass: failed (%s: %s); returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return []


def _run_pass(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: Optional[CodebaseIndex] = None,
) -> List[CodeReviewIssue]:
    """Core of :func:`find_side_effect_impact_issues`; may raise.

    Split out so its sole caller can wrap it in the fail-safe guard.

    Preconditions:
        - ``index``, when given, was built from this same ``input_data``/
          ``repo_reader``.

    Postconditions:
        - Same contract as :func:`find_side_effect_impact_issues`, minus the
          env-toggle/profile early returns the caller already handled.
        - Resolves ``mutation_on`` from ``CODE_REVIEW_MUTATION_ANALYSIS``
          (default on) and passes it to
          :func:`~code_review_agent.prompts.build_side_effect_impact_reasoning_system_prompt`,
          so the reasoning system prompt includes the mutation-vs-replaced-code
          contract sub-check only when the toggle is on. Each batch's user
          prompt is built with ``replaced_content`` gated through
          :func:`~code_review_agent.side_effect_consolidation.effective_replaced_content`
          (``input_data.replaced_content`` when ``mutation_on``, else ``None``):
          when the toggle is off, the before-image is hidden from the model
          entirely, never merely passed through with an instruction to ignore it.
        - Delegates ``Agent`` construction and reactive overflow bisect recovery
          to
          :func:`~code_review_agent.submission_pass_runner.run_submission_pass`,
          which never raises; a batch's findings are folded into the returned
          list in batch order. An empty runner result (context too small, or
          every batch unrecoverable) folds to ``[]`` -- never ``None`` and
          never a raised exception.
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        # No readable submission files: there is no changed behavior to check
        # for caller impact or documentation drift.
        return []

    pre_numbered = _effective_pre_numbered(input_data, index)
    tools = _build_side_effect_tools(index)
    mutation_on = env_flag_enabled(MUTATION_ANALYSIS_ENV)

    def _build_prompt_for_batch(batch: FileBatch) -> str:
        return _build_prompt(
            index,
            content_items=batch.items,
            batch_index=batch.index,
            total_batches=batch.total,
            is_partial=batch.is_partial,
            replaced_content=effective_replaced_content(input_data, mutation_on),
        )

    def _parse_batch_reply(raw: str) -> List[CodeReviewIssue]:
        data = extract_json_from_response(raw)
        findings = _parse_findings(data)
        if findings:
            findings = _validate_findings(index, findings, pre_numbered=pre_numbered)
        return findings

    results = run_submission_pass(
        llm,
        changed_files=list(index.files.items()),
        reasoning_system_prompt=build_side_effect_impact_reasoning_system_prompt(
            mutation_on=mutation_on
        ),
        formatting_instructions=SIDE_EFFECT_IMPACT_FORMATTING_INSTRUCTIONS,
        build_prompt=_build_prompt_for_batch,
        tools=tools,
        parse=_parse_batch_reply,
        pass_label="SideEffectImpactPass",
    )
    findings = [finding for batch_findings in results for finding in batch_findings]
    if findings:
        logger.info(
            "SideEffectImpactPass: found %s new finding(s) (side-effects/documentation)",
            len(findings),
        )
    return findings
