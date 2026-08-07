"""Side-effect / blast-radius pass for code review.

The map-reduce reviewer (``coordinator.py``) flags issues from *bounded
chunks*, and even the checklist item that asks the chunk reviewer to notice a
behavior change in the function/method it is editing (see
``profiles._CODE_REVIEW_CRITERIA`` item 12) has no tools and cannot see beyond
the chunk it was given — it can flag that a function's contract *looks* like
it changed, but it can never know who else in the codebase calls that
function, or whether the new behavior breaks them. Neither the false-positive
filter nor the architecture-consistency pass answers that question either:
both operate over *this submission's* files (plus, via ``RepoReader``, read
access to the rest of the repository), but neither searches the wider
repository for *callers* of a changed function and checks their assumptions.
This pass adds exactly that.

This pass runs ONCE PER SUBMISSION (never once per chunk), after the
architecture-consistency pass, and is purely additive: it is given the full
content of the changed files, with read access to the rest of the repository
via the same tools the false-positive filter and architecture pass use
(``read_file``, ``list_files``, ``search_codebase``,
``find_function_at_line``), plus one new tool this pass introduces,
``search_repository``, which searches the REST of the repository (beyond the
submission) for a substring — the capability actually needed to find a
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

    - **Bounded cost.** Exactly one LLM call per submission (not per chunk),
      matching the architecture pass's per-submission cost shape.

    - **``CODE_REVIEW`` profile only.** The other :class:`.profiles.ReviewProfile`
      values narrow the engine to a specific checklist whose contract expects
      every issue to map to a specific criterion/requirement (e.g. the
      acceptance profile's per-criterion attribution); a side-effects finding
      never maps to one of those, so this pass never runs under any profile
      but the default (mirrors ``architecture_consistency_pass``'s identical
      restriction and rationale).
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from strands import Agent, tool

from llm_service import LLMClient
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

from .chunking import _coerce_bool
from .false_positive_filter import CodebaseIndex, _build_tools, _code_fence_for
from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue, coerce_line, is_no_op_suggestion
from .profiles import ReviewProfile
from .prompts import SIDE_EFFECT_IMPACT_PROMPT
from .repo_reader import DEFAULT_MAX_LISTED_FILES, DiskRepoReader, RepoReader

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS=false``/``0``/``no``
# disables the pass (see docs/ENV_VARS.md). Any other value (or unset) leaves it enabled.
_PASS_ENV = "CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS"

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


def _build_side_effect_tools(index: CodebaseIndex) -> list:
    """Build this pass's tools: the shared submission tools plus repo-wide search.

    Postconditions:
        - Returns the five shared tools from ``false_positive_filter._build_tools``
          (``read_file``, ``read_lines``, ``list_files``, ``search_codebase``,
          ``find_function_at_line``) plus a new ``search_repository`` tool bound
          to ``index`` -- the only tool in this set whose entire purpose is
          reaching beyond the submission's own files to find a changed
          function's out-of-diff callers.
    """

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
            ``list_files()``/``read_file()`` calls when this matters.
        """
        if index.repo_reader is None:
            return "No repository access is available beyond this submission."
        matches, truncated = _search_repository(index, query)
        if not matches:
            if truncated:
                return (
                    f"No matches for {query!r} in the files scanned, but the scan was "
                    "truncated before covering the whole repository -- this does NOT prove "
                    "the substring is absent elsewhere. Use list_files()/read_file() for a "
                    "more targeted follow-up if this caller-impact check matters."
                )
            return f"No matches for {query!r} in the rest of the repository."
        result = "\n".join(f"{path}:{lineno}: {text}" for path, lineno, text in matches)
        if truncated:
            result += (
                f"\n\n(Scan truncated before covering the whole repository -- there may be "
                f"more matches for {query!r} beyond what's shown above.)"
            )
        return result

    return [*_build_tools(index), search_repository]


def build_side_effect_tools(index: CodebaseIndex) -> list:
    """Public wrapper for :func:`_build_side_effect_tools`."""
    return _build_side_effect_tools(index)


def _build_prompt(index: CodebaseIndex, max_inline_chars: int) -> str:
    """Render the single user prompt for this pass.

    Postconditions:
        - Inlines the submission's changed files up to ``max_inline_chars``;
          any files beyond that budget are named as reachable via the
          attached tools rather than silently dropped.
    """
    parts: List[str] = []

    changed_files = list(index.files.items())
    manifest = [path for path, _ in changed_files]
    parts.append(f"**Changed files in this submission ({len(manifest)}):**")
    parts.extend(manifest)
    parts.append("")

    parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(changed_files):
        if remaining <= 0:
            omitted = len(changed_files) - i
            break
        body = content[:remaining]
        body_fence = _code_fence_for(body)
        parts.append(f"### {path} ###")
        parts.append(body_fence)
        parts.append(body)
        parts.append(body_fence)
        if len(body) < len(content):
            parts.append(
                f"(Only the first {len(body)} characters of `{path}` are shown above; call "
                "read_file to see the rest.)"
            )
        remaining -= len(body)
    if omitted:
        parts.append(
            f"... and {omitted} more changed file(s) not shown above; use read_file(path) or "
            "list_files() to see them."
        )
    parts.append("")

    parts.append(
        "Use search_codebase()/search_repository()/list_files()/read_file() to find every caller "
        "of any function or method whose behavior this submission changes, before flagging a "
        "caller-impact finding -- search_codebase only searches the files shown above, "
        "search_repository reaches the rest of the repository."
    )
    parts.append(
        'Return a single JSON object with a "side-effects"/"documentation" findings array, per the '
        'output format above. Return {"findings": []} if you find nothing.'
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
          ``None`` builds a fresh one.

    Postconditions:
        - Returns ``[]`` (no LLM call) when the pass is disabled via
          ``CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS``, when ``input_data.profile``
          is anything other than ``ReviewProfile.CODE_REVIEW`` (the other
          profiles have a narrower contract that expects every issue to be
          attributable to a specific criterion/requirement, which a
          side-effects finding never is -- see
          ``architecture_consistency_pass``'s identical restriction), when
          ``input_data.pre_numbered`` is True (the PR-review hunk-fallback
          mode, used when whole-file fetching is unavailable: ``index.files``
          then holds partial diff-hunk excerpts rendered with original-line-
          number prefixes, not complete file content, and no tool this pass
          has can retrieve a more complete view of a changed file --
          ``read_file`` resolves a changed path from ``index.files`` first, so
          it returns the same partial excerpt already in the prompt. This
          pass's entire contract is "never flag from a guess"; reasoning
          about a function's complete current behavior from a partial hunk
          would violate that, risking false-positive caller-impact findings
          on code the pass never actually saw in full), or when the
          submission has no readable files.
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
    if input_data.pre_numbered:
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
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        # No readable submission files: there is no changed behavior to check
        # for caller impact or documentation drift.
        return []

    model = resolve_code_review_model(llm)
    max_inline_chars = compute_code_review_map_chunk_chars(llm)

    prompt = _build_prompt(index, max_inline_chars)
    agent = Agent(
        model=model,
        system_prompt=SIDE_EFFECT_IMPACT_PROMPT,
        tools=_build_side_effect_tools(index),
    )
    raw = str(agent(prompt)).strip()
    data = json.loads(raw)
    findings = _parse_findings(data)
    if findings:
        findings = _validate_findings(index, findings, pre_numbered=input_data.pre_numbered)
        logger.info(
            "SideEffectImpactPass: found %s new finding(s) (side-effects/documentation)",
            len(findings),
        )
    return findings
