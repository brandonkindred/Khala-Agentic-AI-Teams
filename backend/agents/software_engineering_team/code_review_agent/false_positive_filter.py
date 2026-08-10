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
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from strands import Agent, tool
from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient
from shared.concurrency import parallel_map
from shared.env import env_flag_enabled
from software_engineering_team.shared.context_sizing import parse_env_int
from software_engineering_team.shared.llm import extract_json_from_response

from .function_boundaries import (
    EnclosingConstruct,
    enclosing_construct,
    enclosing_construct_start_heuristic,
    hunk_segment_bounds,
    iter_constructs,
    segment_containing_line,
    strip_numbered_prefixes,
)
from .model_resolution import resolve_code_review_verify_model
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import FALSE_POSITIVE_VERIFY_PROMPT
from .repo_reader import DEFAULT_MAX_LISTED_FILES, DiskRepoReader, RepoReader

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_FALSE_POSITIVE_FILTER=false``/``0``/``no``
# disables the verification pass (see docs/ENV_VARS.md). Any other value (or unset)
# leaves it enabled.
_FILTER_ENV = "CODE_REVIEW_FALSE_POSITIVE_FILTER"

# Cap on substring matches returned by ``search_codebase`` so a common token
# cannot flood the tool result.
_SEARCH_MATCH_LIMIT = 60

# Cap on how many repository files one find_references repo half will scan when
# the reader's per-fetch cost is unknown/expensive (e.g. GitHub-backed readers).
_REPO_SEARCH_FILE_SCAN_LIMIT = 40

# Cap used instead for DiskRepoReader (no per-file fetch cost) — match the
# reader's own listing bound so alphabetical prefixes are not silently missed.
_DISK_REPO_SEARCH_FILE_SCAN_LIMIT = DEFAULT_MAX_LISTED_FILES

# Hard cap on inclusive line span returned by ``read_lines`` so a tool call
# cannot pull an unbounded slice into the verifier context.
_READ_LINES_MAX_SPAN = 400

# Hard cap on the enclosing-construct excerpt size for one find_references hit.
# Above this, _format_reference_hit shows a bounded line-window around the hit
# instead of the whole construct so one oversized function can't flood the result.
_EXCERPT_MAX_LINES = 60

# Size of the fallback line-window shown when no enclosing construct is found
# (module-level hit, non-Python file, or unparsable content) or when a found
# construct exceeds _EXCERPT_MAX_LINES.
_EXCERPT_WINDOW_LINES = 12

# Cap on file paths listed inline in the verification prompt's manifest, so a
# submission touching a large repo can't by itself blow the prompt past the
# model's context window; the rest remains reachable via list_files()/
# read_file(), so nothing is actually inaccessible -- only the inline listing
# is bounded.
_MANIFEST_LIMIT = 300

# Cap on the task description / each acceptance criterion inlined into the
# verification prompt. Unlike the cited file body (deliberately kept in full
# -- see _build_group_prompt), there is no tool the model can call to read the
# rest of an oversized task field, so an unbounded field has no fallback path
# at all if it blows the prompt past context.
_CONTEXT_FIELD_CHARS = 4_000
_CONTEXT_FIELD_TRUNCATION_MARKER = "\n... (truncated)"

# Default per-group verification call timeout (seconds); see
# ``_verify_timeout_seconds`` below.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 3600

# Default cap on findings inlined into a single per-file verification LLM
# call; see ``_verify_max_findings_per_group`` below.
DEFAULT_VERIFY_MAX_FINDINGS_PER_GROUP = 40


def _verify_timeout_seconds() -> int:
    """Per-group verification call timeout (seconds).

    Bounds how long a single ``_verify_one`` call (one LLM verification round
    for one cited file's group of findings) may run before its group is
    treated as a failure (fail-safe: keep its findings, log a warning). Env-
    overridable via ``CODE_REVIEW_VERIFY_TIMEOUT_SECONDS`` (see
    docs/ENV_VARS.md).

    Postconditions:
        - Returns an int >= 1.
    """
    return parse_env_int("CODE_REVIEW_VERIFY_TIMEOUT_SECONDS", DEFAULT_VERIFY_TIMEOUT_SECONDS, 1)


def _verify_max_findings_per_group() -> int:
    """Cap on findings verified in a single per-file LLM call.

    Bounds how many findings ``_build_group_prompt`` renders in one
    verification call for one cited file. A file whose genuine findings
    exceed this cap is split into multiple same-sized batches by
    ``_verify_and_filter`` (each its own ``_verify_group`` call, within the
    cap) instead of growing one prompt/agent turn without bound. Env-
    overridable via ``CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP`` (see
    docs/ENV_VARS.md).

    Postconditions:
        - Returns an int >= 1.
    """
    return parse_env_int(
        "CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP",
        DEFAULT_VERIFY_MAX_FINDINGS_PER_GROUP,
        1,
    )


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


@dataclass(frozen=True)
class CodebaseIndex:
    """In-memory view of all code the verifier may read to check a finding.

    Invariants:
        - ``files`` maps a file path to content whose completeness is reported
          by ``full_content_complete`` (below): when that flag is True, every
          value is the FULL file body -- seeing the whole file is the entire
          point, since the chunk reviewer's partial view is what produced the
          false positive. When it is False (the common case for a pre-numbered
          PR-review submission), a value may instead be a bounded, ``"N: "``
          -prefixed diff excerpt produced by ``render_annotated_hunks``
          (``github_source/pr_review_mapping.py``), covering only the changed
          hunks plus context rather than the whole file, with a bare ``"..."``
          line marking a gap between two hunks that are not adjacent in the
          real file -- not truncation. Every ``N`` in such a prefix is still
          the line's real number in the original file. Whitespace-only bodies
          (e.g. a newline-only ``__init__.py``) are kept; only ``None`` /
          empty-string content is excluded at construction.
        - ``existing_codebase`` is the full pre-existing-code excerpt passed for
          context; it is exposed as the read-only pseudo-path
          ``<existing codebase>`` so the verifier can consult it like any file.
        - ``repo_reader`` is an optional read-only, thread-safe ``RepoReader``
          giving read access to the rest of the repository (files that already
          exist but were not changed, so are absent from the submission). It is
          consulted only as a *fall-through* after the submission and the
          existing-codebase excerpt fail to resolve a path, which is what lets
          the verifier confirm "this file already exists" and drop the false
          positive. In-memory search (``search``) never touches it.
        - ``full_content_complete`` is True iff ``from_input`` applied the
          ``CodeReviewInput.full_content`` overlay (see that method) -- i.e. a
          pre-numbered submission whose ``full_content`` covered every path this
          index holds, so ``files`` holds real full bodies everywhere, not a mix
          of full bodies and bounded excerpts. A partial ``full_content`` never
          sets it (see ``from_input``'s all-or-nothing overlay rule). A
          whole-codebase pass can gate on this flag alone -- never on
          ``input_data.full_content`` directly -- without re-deriving path
          coverage itself. False (the default, e.g. a directly-constructed
          index, or ``pre_numbered=False`` where the field is inapplicable)
          means "not verified complete."
        - The index is read-only after construction: the dataclass is frozen,
          ``files`` is shallow-copied at init, no method mutates ``files`` or
          ``existing_codebase``, and it never mutates ``repo_reader`` (whose
          own reads are internally synchronized), so it is safe to share across
          the parallel verification worker threads.
    """

    files: Dict[str, str]
    existing_codebase: str = ""
    repo_reader: Optional[RepoReader] = None
    full_content_complete: bool = False

    EXISTING_CODEBASE_PATH = "<existing codebase>"

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", dict(self.files))

    @classmethod
    def from_input(
        cls, input_data: CodeReviewInput, repo_reader: Optional[RepoReader] = None
    ) -> "CodebaseIndex":
        """Build the index from a review input's ``files``.

        Preconditions:
            - ``input_data.files`` is a non-empty mapping (enforced by
              ``CodeReviewInput``'s own validator, so always true here).

        Postconditions:
            - Every file whose content is not ``None`` and not ``""`` is included
              (insertion order preserved), including whitespace-only bodies, with
              no header parsing.
            - When ``input_data.pre_numbered`` is True and ``input_data.full_content``
              covers EVERY path this index would otherwise hold, each path's bounded
              pre-numbered excerpt is replaced by its full body -- so a whole-codebase
              pass reading via this index sees real content everywhere, while the
              chunk reviewer (built from ``code``/``pre_numbered`` directly, not from
              this index) is unaffected. A ``full_content`` that covers only SOME
              paths is not applied at all (all-or-nothing): overlaying just the
              covered subset would leave the rest as bounded excerpts sitting
              alongside full bodies with no way for a caller to tell them apart,
              which is worse than not overlaying anything -- a pass would read those
              still-bounded, ``"N: "``-prefixed excerpts as if they were complete
              files. ``full_content_complete`` on the returned index reports which
              case occurred (see the class docstring). Ignored entirely when
              ``pre_numbered`` is False. Only replaces content for paths already in
              ``files`` -- an extra ``full_content`` key beyond what this submission
              already holds is never added, so a caller that (like ``full_content_complete``
              itself allows) supplies more paths than the submission covers can never
              expand the index's changed-path set beyond what ``files``/``code``
              actually determined; a whole-codebase pass reading ``index.files`` must
              never see a path this submission did not itself include.
            - ``existing_codebase`` carries the input's full existing-codebase
              excerpt (empty string when absent); ``repo_reader`` is stored
              verbatim.
        """
        files = {
            path: content
            for path, content in input_data.files.items()
            if content is not None and content != ""
        }
        full_content_complete = bool(
            input_data.pre_numbered
            and input_data.full_content
            and set(input_data.full_content) >= set(files)
        )
        if full_content_complete:
            # Intersect, never union: full_content may (per the coverage check above)
            # legitimately carry MORE paths than this submission actually reviews --
            # only paths files already holds are ever replaced, so an extra key can
            # never expand the index's changed-path set.
            files = {
                path: input_data.full_content.get(path, content) for path, content in files.items()
            }
        return cls(
            files=files,
            existing_codebase=input_data.existing_codebase or "",
            repo_reader=repo_reader,
            full_content_complete=full_content_complete,
        )

    def _reader_read(self, path: str) -> Optional[str]:
        """Read ``path`` from the repo reader, degrading to ``None``.

        Postconditions:
            - Returns the reader's content for ``path`` when a reader is attached
              and it resolves the path — INCLUDING an empty string for an existing
              zero-byte file (e.g. a package ``__init__.py``), so an existing empty
              file is confirmed present rather than reported absent. Returns
              ``None`` only when there is no reader, the reader itself returns
              ``None`` (path absent/unreadable), or the reader raises (fail-safe:
              a reader failure only ever *keeps* a finding). Never raises.
        """
        if self.repo_reader is None:
            return None
        try:
            return self.repo_reader.read_file(path)
        except Exception as exc:  # noqa: BLE001 - a reader failure must never break verification
            logger.debug("CodebaseIndex: repo_reader.read_file(%r) failed: %s", path, exc)
            return None

    def _reader_files(self) -> List[str]:
        """List the repo reader's paths, degrading to ``[]``.

        Postconditions:
            - Returns the reader's paths when a reader is attached; ``[]`` when
              there is no reader or it raises. Never raises.
        """
        if self.repo_reader is None:
            return []
        try:
            return list(self.repo_reader.list_files())
        except Exception as exc:  # noqa: BLE001 - a reader failure must never break verification
            logger.debug("CodebaseIndex: repo_reader.list_files() failed: %s", exc)
            return []

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
        """Return every readable path, repo-reader paths after the submission's.

        Postconditions:
            - The submission's own files come first in insertion order, then the
              ``<existing codebase>`` pseudo-path (only when a non-blank excerpt
              was provided), then the repo reader's paths (when a reader is
              attached), de-duplicated with submission paths winning. Never
              raises (a reader failure contributes no paths).
        """
        paths = [path for path, _ in self._readable_sources()]
        seen = set(paths)
        for path in self._reader_files():
            if path not in seen:
                seen.add(path)
                paths.append(path)
        return paths

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
            - ``(sole_exact_normalized, [])`` when exactly one stored path equals
              ``key`` after ``_normalize_leading`` on both sides (so ``./app/main.py``
              prefers ``app/main.py`` over a nested suffix like ``src/app/main.py``).
            - ``(None, exact_hits)`` when multiple stored paths share that exact
              normalized form.
            - ``(sole_hit, hits)`` when exactly one bare-name / path-suffix match,
              else ``(None, hits)``, where ``hits`` are the candidate paths that
              share the requested final segment or path suffix — never raises.
            - A ``../`` prefix is never treated as ``./``; parent-dir citations do
              not collapse to a bare basename.
        """
        if not key:
            return None, []
        if key == self.EXISTING_CODEBASE_PATH:
            return (self.EXISTING_CODEBASE_PATH if self.existing_codebase.strip() else None), []
        if key in self.files:
            return key, []
        # Prefer an exact match of the leading-normalized key before any
        # bare-name / suffix fallback. ``_normalize_leading`` strips only
        # literal ``./`` repeats (and one leading ``/``) — never ``../`` —
        # so ``./app/main.py`` can resolve to ``app/main.py`` even when
        # ``src/app/main.py`` would also be a suffix hit.
        normalized = self._normalize_leading(key)
        exact = [p for p in self.files if self._normalize_leading(p) == normalized]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact
        # Bare-name / suffix fallback: the model often cites ``main.py`` for
        # ``app/services/main.py``, or ``config/.env`` for ``src/config/.env``.
        # Match every stored path whose final ``/``-segment (bare name) or
        # trailing path suffix equals the normalized key (without mangling
        # hidden names like ``.env``); a unique hit resolves, and the full
        # list lets ``read_file`` distinguish ambiguity.
        if "/" in normalized:
            hits = [p for p in self.files if self._normalize_leading(p).endswith("/" + normalized)]
        else:
            hits = [p for p in self.files if self._final_segment(p) == normalized]

        return (hits[0] if len(hits) == 1 else None), hits

    @staticmethod
    def _normalize_leading(p: str) -> str:
        """Strip leading ``./`` repeats and a single leading ``/`` from ``p``.

        Preconditions:
            - ``p`` is a string (may be empty).

        Postconditions:
            - Returns ``p`` with any leading ``./`` prefixes and at most one
              leading ``/`` removed; a leading single-dot name like ``.env``
              is preserved.
        """
        while p.startswith("./"):
            p = p[2:]
        if p.startswith("/"):
            p = p[1:]
        return p

    @classmethod
    def _final_segment(cls, p: str) -> str:
        """Return the final path segment of ``p`` after leading-prefix normalize.

        Preconditions:
            - ``p`` is a string (may be empty).

        Postconditions:
            - Returns the substring after the last ``/`` in the
              leading-normalized form of ``p`` (the whole string when it
              contains no ``/``).
        """
        return cls._normalize_leading(p).rsplit("/", 1)[-1]

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
            - When the submission has multiple suffix hits for the cited bare
              name, returns None (ambiguous) and does **not** consult the repo
              reader — findings about one of several same-basename submission
              files must not silently resolve to a repository file.
            - Falls through to the repo reader only when the submission has
              zero matches: if the reader can read the cited path, returns it
              verbatim (see ``CodebaseIndex``'s ``repo_reader`` invariant for why).
            - Returns None for a blank, absent, or ambiguous path with no
              eligible reader hit — the verifier would have no single primary
              file to read, so the caller keeps the finding rather than verify it.
        """
        key = (path or "").strip()
        resolved, hits = self._resolve(key)
        if resolved is not None:
            return resolved
        if key == self.EXISTING_CODEBASE_PATH:
            return None
        if len(hits) > 1:
            return None
        if key and self._reader_read(key) is not None:
            return key
        return None

    def _read(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        """Resolve and read ``path``, returning ``(content, error_message)``.

        Exactly one of the two return values is not None. Shared by
        ``read_file`` (tool-facing: forwards ``error_message`` as the
        returned string) and ``read_file_or_none`` (internal-facing: collapses
        ``error_message`` to ``None`` so a real file's content can never be
        mistaken for a sentinel string, however it happens to start).

        Resolution order: exact / existing-codebase / unique suffix match;
        then ambiguous submission hits as an error (before any repo-reader
        lookup); then repo-reader fallback for absent submission paths;
        then missing-excerpt / not-found errors.

        Postconditions:
            - Returns ``(content, None)`` on exact or unique-suffix match, or
              when the repo reader supplies the file.
            - Returns ``(None, error_string)`` for blank paths, ambiguous
              matches, missing excerpts, or not-found paths.
            - Never raises.
        """
        key = (path or "").strip()
        if not key:
            return None, "Error: no path provided."
        resolved, hits = self._resolve(key)
        if resolved == self.EXISTING_CODEBASE_PATH:
            return self.existing_codebase, None
        if resolved is not None:
            return self.files[resolved], None
        if len(hits) > 1:
            return None, (
                f"Error: path '{path}' is ambiguous; it matches "
                f"{', '.join(sorted(hits))}. Use list_files() and read the exact path."
            )
        # Fall through to the repo reader for an existing-but-unchanged file
        # (see CodebaseIndex's repo_reader invariant for why). Ambiguous
        # submission hits never reach here.
        if key == self.EXISTING_CODEBASE_PATH:
            return None, "Error: no existing-codebase excerpt available."

        reader_content = self._reader_read(key)
        if reader_content is not None:
            return reader_content, None
        return None, f"Error: file not found: {path}. Use list_files() to see available paths."

    def read_file(self, path: str) -> str:
        """Return the full content of ``path``, resolving near-misses.

        Postconditions:
            - An exact path match returns that file's full content.
            - The ``<existing codebase>`` pseudo-path returns the existing-code
              excerpt.
            - A path that uniquely matches one file by suffix (the model often
              cites ``main.py`` for ``app/main.py``) returns that file.
            - An ambiguous submission suffix match returns an ``Error: ...``
              string and never falls through to the repo reader.
            - An absent submission path may fall through to the repo reader; if
              that also misses, returns an ``Error: ...`` string (never raises)
              so a bad tool argument degrades to a message rather than aborting
              the verification.
        """
        content, error = self._read(path)
        return error if content is None else content

    def read_file_or_none(self, path: str) -> Optional[str]:
        """Return the full content of ``path``, or None if it can't be read.

        Same resolution as ``read_file`` (exact match, existing-codebase
        pseudo-path, suffix match, repo-reader fallback), but for internal
        callers that must tell "unreadable" apart from file content — unlike
        ``read_file``, whose ``Error: ...`` return is a sentinel string that a
        real file's own content could coincidentally start with. Never raises.
        """
        content, _ = self._read(path)
        return content

    def read_lines(self, path: str, start: int, end: int) -> str:
        """Return an inclusive 1-based line slice of ``path``, capped by max span.

        Preconditions:
            - Callers should pass 1-based inclusive ``start``/``end``. Invalid
              bounds are reported as ``Error: ...`` strings rather than raised.

        Postconditions:
            - Returns ``Error: ...`` for non-positive/non-int bounds, inverted
              ranges, spans above ``_READ_LINES_MAX_SPAN``, unreadable paths, or
              ``start`` past EOF — never raises on those cases.
            - On success, returns a header ``{path} lines {start}–{end_eff} ({n} lines):``
              followed by ``N| content`` body lines for the inclusive slice.
            - When ``end`` exceeds file length and ``start`` is in range, clamps
              ``end`` to the last line.
            - When ``content`` is pre-numbered (``render_annotated_hunks`` output),
              ``start``/``end`` are resolved as *original file* line numbers via
              ``strip_numbered_prefixes``, never as physical excerpt positions:
              - When both resolve into the same gap-bounded hunk segment, the
                header and body are built entirely from the mapper — the
                header's claimed range always matches the body's own embedded
                numbers. A line absent from the excerpt (e.g. a removed diff
                line) falls back to the nearest preceding available line.
                ``end`` beyond the segment's last available line clamps to
                that last line.
              - A ``start`` outside its resolved segment's real coverage
                (e.g. requesting original line 1 against a 100–102 excerpt)
                returns ``Error: ...`` naming that segment's real coverage
                instead of a self-contradictory header.
              - A ``start``/``end`` pair whose physical positions resolve into
                two different hunk segments returns ``Error: ...`` naming
                both segments' real coverage; a successful response never
                contains the ``"..."`` gap marker or the other segment's
                content.
            - Path resolution matches ``read_file``.
        """
        if not isinstance(start, int) or isinstance(start, bool) or start < 1:
            return f"Error: start must be a positive integer, got {start!r}."
        if not isinstance(end, int) or isinstance(end, bool) or end < 1:
            return f"Error: end must be a positive integer, got {end!r}."
        if start > end:
            return f"Error: invalid range: start ({start}) > end ({end})."
        span = end - start + 1
        if span > _READ_LINES_MAX_SPAN:
            return (
                f"Error: range spans {span} lines; maximum is {_READ_LINES_MAX_SPAN}. "
                "Narrow start/end or use read_function."
            )

        content, error = self._read(path)
        if content is None:
            return error if error is not None else f"Error: file not found: {path}."

        stripped, start_physical, mapper = strip_numbered_prefixes(content, start)
        if mapper is not None:
            _, end_physical, _ = strip_numbered_prefixes(content, end)
            display = self.resolve_path(path) or path
            if display == self.EXISTING_CODEBASE_PATH:
                display = path

            # Resolve segment bounds against the raw pre-numbered ``content``, not
            # ``stripped``: ``strip_numbered_prefixes`` rebuilds ``stripped`` via
            # ``"\n".join(...)``, and ``str.splitlines()`` silently drops a
            # trailing blank line from that reconstruction (e.g. a hunk's last
            # numbered line being empty) — an artifact ``content`` doesn't have.
            # Bare ``...`` separators are untouched by prefix-stripping, so they
            # sit at the same physical positions in both strings either way.
            start_seg = hunk_segment_bounds(content, start_physical, annotated_hunks=True)
            if start_seg is None:
                return f"Error: line {start} has no resolvable coverage in {display}'s excerpt."

            # ``strip_numbered_prefixes`` falls back to the *nearest preceding*
            # numbered line when ``start`` has no exact match, defaulting to the
            # excerpt's very first physical line when nothing precedes it either
            # (e.g. requesting original line 1 against a 100-102 excerpt) — check
            # the requested ``start`` against the segment's real coverage rather
            # than trusting that fallback blindly, or the header would silently
            # claim a range the caller never asked for.
            real_start, real_end = mapper(start_seg[0]), mapper(start_seg[1])
            if start < real_start or start > real_end:
                return (
                    f"Error: start line {start} is outside {display}'s excerpt coverage "
                    f"(excerpt covers lines {real_start}-{real_end})."
                )

            end_seg = hunk_segment_bounds(content, end_physical, annotated_hunks=True)
            if end_seg != start_seg:
                if end_seg is None:
                    return (
                        f"Error: end line {end} has no resolvable coverage in {display}'s "
                        f"excerpt (nearest hunk covers lines {real_start}-{real_end})."
                    )
                other_start, other_end = mapper(end_seg[0]), mapper(end_seg[1])
                return (
                    f"Error: requested range {start}-{end} spans two separate hunks in "
                    f"{display}: lines {real_start}-{real_end} and lines "
                    f"{other_start}-{other_end}. Narrow the request to a single hunk."
                )

            # ``str.split("\n")`` is the exact inverse of the ``"\n".join(...)``
            # that built ``stripped``, so it preserves a trailing blank line
            # that ``.splitlines()`` would drop (see note above).
            stripped_lines = stripped.split("\n")
            end_eff_physical = min(end_physical, start_seg[1])
            n = end_eff_physical - start_physical + 1
            display_start = mapper(start_physical)
            display_end = mapper(end_eff_physical)
            header = f"{display} lines {display_start}–{display_end} ({n} lines):"
            body = "\n".join(
                f"{mapper(i)}| {stripped_lines[i - 1]}"
                for i in range(start_physical, end_eff_physical + 1)
            )
            return f"{header}\n{body}"

        lines = content.splitlines()
        n_lines = len(lines)
        if start > n_lines:
            display = self.resolve_path(path) or path
            if display == self.EXISTING_CODEBASE_PATH:
                display = path
            return (
                f"Error: start line {start} is beyond the end of {display} "
                f"(file has {n_lines} lines)."
            )
        end_eff = min(end, n_lines)
        display = self.resolve_path(path) or path
        if display == self.EXISTING_CODEBASE_PATH:
            display = path
        n = end_eff - start + 1
        header = f"{display} lines {start}–{end_eff} ({n} lines):"
        body = "\n".join(f"{i}| {lines[i - 1]}" for i in range(start, end_eff + 1))
        return f"{header}\n{body}"

    def read_function(self, path: str, line: int) -> str:
        """Return the enclosing Python construct body for ``line``, or an error.

        Preconditions:
            - Callers should pass a 1-based ``line``. Invalid bounds and
              unresolved lookups are reported as ``Error: ...`` strings rather
              than raised.

        Postconditions:
            - Returns ``Error: ...`` for bad ``line``, unreadable paths,
              non-``.py``/``.pyi`` paths, or when no enclosing function/class
              brackets ``line`` — never raises on those cases.
            - On success, returns a header
              ``{path} {kind} {name} lines {start}–{end} ({n} lines):``
              followed by ``N| content`` body lines for the inclusive construct
              span (decorators included). Path resolution matches ``read_file``.
            - Does not apply ``_READ_LINES_MAX_SPAN``.
        """
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return f"Error: line must be a positive integer, got {line!r}."

        content, error = self._read(path)
        if content is None:
            return error if error is not None else f"Error: file not found: {path}."

        display = self.resolve_path(path) or path
        if display == self.EXISTING_CODEBASE_PATH:
            display = path
        _, ext = os.path.splitext(display)
        if ext.lower() not in (".py", ".pyi"):
            return f"Error: read_function by line requires a Python file (.py/.pyi); got {display}."

        stripped, physical, mapper = strip_numbered_prefixes(content, line)
        construct = enclosing_construct(stripped, physical, annotated_hunks=mapper is not None)
        if construct is None:
            return f"Error: no enclosing function/class for line {line} of {display}."

        return _format_construct_slice(display, construct, stripped.splitlines(), mapper=mapper)

    def read_function_by_name(self, path: str, name: str) -> str:
        """Return the construct body for an exact name match, or an error.

        Preconditions:
            - ``name`` should be a non-empty string matching ``EnclosingConstruct.name``
              exactly (bare or ``Class.method``).

        Postconditions:
            - Returns ``Error: ...`` for bad name, unreadable/non-Python paths,
              zero matches, or multiple matches — never raises on those cases.
            - On a unique match, returns the same success format as ``read_function``.
        """
        if not isinstance(name, str) or not name.strip():
            return f"Error: name must be a non-empty string, got {name!r}."
        needle = name.strip()

        content, error = self._read(path)
        if content is None:
            return error if error is not None else f"Error: file not found: {path}."

        display = self.resolve_path(path) or path
        if display == self.EXISTING_CODEBASE_PATH:
            display = path
        _, ext = os.path.splitext(display)
        if ext.lower() not in (".py", ".pyi"):
            return f"Error: read_function by name requires a Python file (.py/.pyi); got {display}."

        stripped, _, mapper = strip_numbered_prefixes(content, 1)
        # Pre-numbered hunk excerpts use annotated_hunks so a sibling
        # unparseable continuation does not hide constructs in other hunks.
        matches = [
            c
            for c in iter_constructs(stripped, annotated_hunks=mapper is not None)
            if c.name == needle
        ]
        if not matches:
            return f"Error: no function/class named {needle!r} in {display}."
        if len(matches) > 1:

            def _disp(n: int) -> int:
                return mapper(n) if mapper is not None else n

            detail = ", ".join(
                f"{c.name} (lines {_disp(c.start_line)}–{_disp(c.end_line)})" for c in matches
            )
            return (
                f"Error: name {needle!r} is ambiguous in {display}; matches: {detail}. "
                f"Call read_function with a line number from one of those ranges."
            )
        return _format_construct_slice(display, matches[0], stripped.splitlines(), mapper=mapper)

    def search(
        self, query: str, max_matches: int = _SEARCH_MATCH_LIMIT
    ) -> List[Tuple[str, int, str]]:
        """Find a case-insensitive substring across the in-memory sources.

        Searches only the submission's files plus the existing-codebase excerpt
        (the in-memory ``_readable_sources``) — NOT the repo reader, which would
        require fetching/scanning the whole repository per query. To check a
        specific repo file's existence or content, the verifier uses
        ``list_files`` + a targeted ``read_file`` (which does consult the reader).

        Preconditions:
            - ``max_matches`` > 0.

        Postconditions:
            - Returns ``(path, 1-based-line-number, line-text)`` tuples for the
              first ``max_matches`` occurrences in path then line order; the
              existing-codebase excerpt is searched last under its pseudo-path.
            - A blank query returns no matches (a substring search for "" would
              match every line and is never a useful false-positive check).
        """
        if max_matches <= 0:
            raise ValueError("max_matches must be positive")
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

    def find_references(self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT) -> str:
        """Search submission (and repo_reader when present) for capped path:line hits.

        Submission matches come from :meth:`search` first. When a ``repo_reader``
        is attached and slots remain under ``max_matches``, fills them from the
        repository (skipping submission paths) via ``_search_repo_references``.

        Preconditions:
            - ``max_matches`` > 0.

        Postconditions:
            - On complete hits with a reader: newline-joined hit blocks; each block
              starts with ``path:line`` and appends a bounded excerpt: a full
              enclosing-construct slice (same shape as ``read_function``) for
              readable ``.py``/``.pyi`` files when the construct is at most
              ``_EXCERPT_MAX_LINES``, else an ``_EXCERPT_WINDOW_LINES``-line
              window around the hit (also used when no construct is found).
            - When truncated (repo scan incomplete, or submission filled
              ``max_matches`` so the repo half was skipped): append a truncated
              banner (hits) or an empty-truncated message (no hits).
            - When no ``repo_reader``: always append the no-repository-access note.
            - Blank/whitespace-only ``symbol`` is not searched; the response must
              not read as a complete empty scan of submission or repository.
            - Never raises for missing symbols or reader failures; raises
              ``ValueError`` when ``max_matches`` is non-positive (via ``search``).
        """
        if not (symbol or "").strip():
            body = f"No references for {symbol!r}."
            if self.repo_reader is None:
                return f"{body}\n\nNo repository access is available beyond this submission."
            return (
                f"{body} Blank/whitespace symbols are not searched -- this does NOT prove "
                "the symbol is absent from the submission or repository."
            )

        hits = list(self.search(symbol, max_matches=max_matches))
        truncated = False
        if self.repo_reader is None:
            body = (
                "\n\n".join(_format_reference_hit(self, path, lineno) for path, lineno, _ in hits)
                if hits
                else f"No references for {symbol!r}."
            )
            return f"{body}\n\nNo repository access is available beyond this submission."

        remaining = max_matches - len(hits)
        if remaining == 0:
            truncated = True
        elif remaining > 0:
            repo_hits, repo_truncated = _search_repo_references(self, symbol, max_matches=remaining)
            hits.extend(repo_hits)
            truncated = repo_truncated

        if not hits:
            if truncated:
                return (
                    f"No references for {symbol!r} in the files scanned, but the scan was "
                    "truncated before covering the whole repository -- this does NOT prove "
                    "the symbol is absent elsewhere. Use list_files()/read_file() for a "
                    "more targeted follow-up if this matters."
                )
            return f"No references for {symbol!r}."

        result = "\n\n".join(_format_reference_hit(self, path, lineno) for path, lineno, _ in hits)
        if truncated:
            result += (
                f"\n\n(Scan truncated before covering the whole repository -- there may be "
                f"more matches for {symbol!r} beyond what's shown above.)"
            )
        return result


def _search_repo_references(
    index: CodebaseIndex,
    query: str,
    max_matches: int,
    max_files_scanned: Optional[int] = None,
) -> Tuple[List[Tuple[str, int, str]], bool]:
    """Find case-insensitive substring hits via ``index.repo_reader`` only.

    Preconditions:
        - ``max_matches`` > 0 and, when given, ``max_files_scanned`` > 0.

    Postconditions:
        - Returns ``([], False)`` when ``repo_reader`` is None or ``query`` is blank.
        - When ``max_files_scanned`` is None, uses ``_DISK_REPO_SEARCH_FILE_SCAN_LIMIT``
          for ``DiskRepoReader`` else ``_REPO_SEARCH_FILE_SCAN_LIMIT``.
        - Skips paths already keys of ``index.files``; returns up to ``max_matches``
          ``(path, 1-based-line, line-text)`` tuples alongside a ``truncated`` flag.
        - ``truncated`` is ``True`` whenever the scan did not inspect every candidate
          path (file-scan cap, match cap, reader listing truncation, or read failures).
        - Never raises on reader errors.
    """
    if max_matches <= 0:
        raise ValueError("max_matches must be positive")
    if max_files_scanned is not None and max_files_scanned <= 0:
        raise ValueError("max_files_scanned must be positive")
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
    except Exception as exc:  # noqa: BLE001 - fail-safe
        logger.debug("find_references: repo_reader.list_files() failed: %s", exc)
        return [], True

    results: List[Tuple[str, int, str]] = []
    scanned = 0
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
        except Exception as exc:  # noqa: BLE001
            logger.debug("find_references: repo_reader.read_file(%r) failed: %s", path, exc)
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


def _strip_numbered_prefixes(
    content: str, line_number: int
) -> Tuple[str, int, Optional[Callable[[int], int]]]:
    """Strip ``N: `` line-number prefixes from pre-numbered hunk content.

    Thin re-export of :func:`function_boundaries.strip_numbered_prefixes`,
    kept under this name for existing call sites/tests in this module.
    """
    return strip_numbered_prefixes(content, line_number)


def _format_construct_slice(
    display: str,
    construct: EnclosingConstruct,
    body_lines: List[str],
    *,
    mapper: Optional[Callable[[int], int]] = None,
) -> str:
    """Format one construct span as a header plus ``N| content`` body lines.

    Preconditions:
        - ``body_lines`` contains at least ``construct.end_line`` entries
          (1-based indexing into ``body_lines``).

    Postconditions:
        - Returns the shared success format used by ``read_function`` and
          ``read_function_by_name``.
    """
    display_start = mapper(construct.start_line) if mapper is not None else construct.start_line
    display_end = mapper(construct.end_line) if mapper is not None else construct.end_line
    n = construct.end_line - construct.start_line + 1
    header = (
        f"{display} {construct.kind} {construct.name} "
        f"lines {display_start}–{display_end} ({n} lines):"
    )
    body = "\n".join(
        f"{(mapper(i) if mapper is not None else i)}| {body_lines[i - 1]}"
        for i in range(construct.start_line, construct.end_line + 1)
    )
    return f"{header}\n{body}"


def _format_line_window(
    display: str,
    body_lines: List[str],
    lineno: int,
    *,
    mapper: Optional[Callable[[int], int]] = None,
    lo: int = 1,
    hi: Optional[int] = None,
) -> str:
    """Format a bounded window of raw lines around ``lineno`` (no/oversized construct).

    Preconditions:
        - ``lineno`` is a 1-based index into ``body_lines``.
        - When given, ``1 <= lo <= lineno <= hi``.

    Postconditions:
        - Returns a header plus ``N| content`` body lines for up to
          ``_EXCERPT_WINDOW_LINES`` lines centered on ``lineno``, clamped to
          ``[lo, hi or len(body_lines)]`` so a window inside an oversized
          construct never spills past that construct's own span.
        - Header reports the shown range and the total lines in ``[lo, hi]``
          so the window reads as bounded/partial, not a complete excerpt.
    """
    total_hi = hi if hi is not None else len(body_lines)
    span = total_hi - lo + 1
    half = _EXCERPT_WINDOW_LINES // 2
    start = max(lo, lineno - half)
    end = min(total_hi, start + _EXCERPT_WINDOW_LINES - 1)
    start = max(lo, end - _EXCERPT_WINDOW_LINES + 1)
    display_start = mapper(start) if mapper is not None else start
    display_end = mapper(end) if mapper is not None else end
    shown = end - start + 1
    header = f"{display} lines {display_start}–{display_end} (window, {shown} of {span} lines):"
    body = "\n".join(
        f"{(mapper(i) if mapper is not None else i)}| {body_lines[i - 1]}"
        for i in range(start, end + 1)
    )
    return f"{header}\n{body}"


def _format_reference_hit(index: CodebaseIndex, path: str, lineno: int) -> str:
    """Format one find_references hit as path:line plus a bounded excerpt.

    Preconditions:
        - ``lineno`` >= 1 and is a 1-based storage index from ``search`` /
          ``_search_repo_references`` (physical line in the stored blob).

    Postconditions:
        - Always starts with ``{path}:{display_line}`` where ``display_line`` is
          the original ``N:`` file line when content is pre-numbered, else
          ``lineno``.
        - When readable ``.py``/``.pyi`` content has an enclosing construct at
          the physical hit line spanning at most ``_EXCERPT_MAX_LINES``,
          appends a full construct slice from ``_format_construct_slice``
          (same shape as ``read_function``).
        - When the construct exceeds ``_EXCERPT_MAX_LINES``, or no construct
          is found (module-level hit, non-Python file, unparsable content),
          appends a bounded ``_EXCERPT_WINDOW_LINES``-line window around the
          hit instead, via ``_format_line_window`` -- excerpt payloads stay
          bounded even when no construct can be resolved.
        - Returns only the locator when the file content itself is
          unreadable.
        - Never raises.
    """
    loc = f"{path}:{lineno}"
    content, _error = index._read(path)
    if content is None:
        return loc
    display = index.resolve_path(path) or path
    if display == index.EXISTING_CODEBASE_PATH:
        display = path
    _, ext = os.path.splitext(display)
    try:
        # ``lineno`` from search is a storage/physical index. ``strip_numbered_prefixes``
        # remaps an *original* file line; pass a dummy original and keep ``lineno``
        # as the physical index (same pattern as ``read_function_by_name``).
        stripped, _, mapper = strip_numbered_prefixes(content, 1)
        if mapper is not None:
            loc = f"{path}:{mapper(lineno)}"
        construct = (
            enclosing_construct(stripped, lineno, annotated_hunks=mapper is not None)
            if ext.lower() in (".py", ".pyi")
            else None
        )
    except Exception:  # noqa: BLE001 - excerpt failure must not abort find_references
        return loc
    body_lines = stripped.splitlines()
    if construct is not None:
        n = construct.end_line - construct.start_line + 1
        if n <= _EXCERPT_MAX_LINES:
            excerpt = _format_construct_slice(display, construct, body_lines, mapper=mapper)
            return f"{loc}\n{excerpt}"
        excerpt = _format_line_window(
            display,
            body_lines,
            lineno,
            mapper=mapper,
            lo=construct.start_line,
            hi=construct.end_line,
        )
        return f"{loc}\n{excerpt}"
    excerpt = _format_line_window(display, body_lines, lineno, mapper=mapper)
    return f"{loc}\n{excerpt}"


def _find_python_function_at_line(
    content: str,
    line_number: int,
    path: str,
    display_line: Optional[int] = None,
    line_mapper: Optional[Callable[[int], int]] = None,
) -> str:
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
        - Start/end lines come from :func:`function_boundaries.enclosing_construct`,
          which itself uses the shared ``node_start_line``/``node_end_line``
          helpers, so all AST consumers agree on construct ranges.
    """
    if not isinstance(content, str) or not content:
        raise ValueError("content must be a non-empty string")
    if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
        raise ValueError("line_number must be a positive integer")
    shown = display_line if display_line is not None else line_number
    lines = content.splitlines()
    if line_number > len(lines):
        return f"Line {shown} is beyond the end of {path} (file has {len(lines)} lines)."

    construct = enclosing_construct(content, line_number, annotated_hunks=line_mapper is not None)

    if construct is None:
        # enclosing_construct() never raises; re-parse once here only to tell a
        # genuine parse failure apart from "parsed fine, but module level" so
        # the two get distinct messages. Re-parse only the gap-bounded segment
        # enclosing_construct() itself resolved against -- naively re-parsing
        # the full annotated-hunk content would join independent hunks across
        # a bare "..." gap marker and can raise on its own (e.g.
        # IndentationError from a later hunk's indented continuation),
        # misreporting a valid module-level line as unparseable.
        segment = segment_containing_line(
            content, line_number, annotated_hunks=line_mapper is not None
        )
        if segment is None:
            return (
                f"Line {shown} of {path} falls between annotated hunks "
                "(no excerpt covers that line); use read_file to inspect the full file."
            )
        try:
            ast.parse(segment)
        except Exception as exc:
            return (
                f"Could not parse {path} as Python ({type(exc).__name__}: {exc}); "
                "use read_file to inspect the full file manually."
            )
        return (
            f"Line {shown} of {path} is at module level "
            "(no enclosing function, method, or class found)."
        )

    kind = construct.kind
    name = construct.name
    class_label = ""
    if kind == "function" and "." in name:
        class_name, name = name.split(".", 1)
        class_label = f" in class '{class_name}'"

    display_start = (
        line_mapper(construct.start_line) if line_mapper is not None else construct.start_line
    )
    display_end = line_mapper(construct.end_line) if line_mapper is not None else construct.end_line
    return (
        f"Line {shown} is inside {kind} '{name}'{class_label} "
        f"({path} lines {display_start}–{display_end})."
    )


def _find_heuristic_function_at_line(
    content: str,
    line_number: int,
    path: str,
    display_line: Optional[int] = None,
    line_mapper: Optional[Callable[[int], int]] = None,
) -> str:
    """Guess the enclosing construct for ``line_number`` using column-0 heuristics.

    Scans from the first line up to ``line_number`` and formats a message naming
    the start line of the last column-0 declaration found — the same heuristic
    used by ``code_boundaries._heuristic_break_lines`` for chunk splitting.
    Useful for TypeScript, JavaScript, Go, and other non-Python languages.

    Preconditions:
        - ``content`` is a non-empty string.
        - ``line_number`` >= 1.
        - ``path`` is a non-empty string used only for display.

    Postconditions:
        - Returns a human-readable message identifying the best-guess start line
          of the enclosing construct and advising the use of ``read_file`` for
          the precise construct name.
        - Returns an explicit beyond-EOF message when ``line_number`` exceeds
          the file length.
        - Returns a "no construct found" message (never raises when
          preconditions hold) when no column-0 declaration precedes
          ``line_number``.
    """
    if not isinstance(content, str) or not content:
        raise ValueError("content must be a non-empty string")
    if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
        raise ValueError("line_number must be a positive integer")
    shown = display_line if display_line is not None else line_number
    lines = content.splitlines()
    if line_number > len(lines):
        return f"Line {shown} is beyond the end of {path} (file has {len(lines)} lines)."
    best_start = enclosing_construct_start_heuristic(content, line_number)

    if best_start is None:
        return (
            f"Could not identify an enclosing construct for line {shown} of {path} "
            "(no column-0 declaration found before that line). "
            "Use read_file to inspect the full file."
        )
    display_start = line_mapper(best_start) if line_mapper is not None else best_start
    return (
        f"Line {shown} of {path} appears to be inside the construct "
        f"starting at line {display_start}. "
        "Use read_file to see the full construct name and body."
    )


def _truncate_for_log(text: Optional[str], max_len: int = 400) -> str:
    """Return ``text`` capped to ``max_len`` characters for log lines.

    Preconditions:
        - ``max_len`` >= 1.

    Postconditions:
        - Returns ``""`` when ``text`` is None or empty.
        - Returns ``text`` unchanged when ``len(text) <= max_len``.
        - Otherwise returns ``text[:max_len] + "..."``.
    """
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _build_tools(index: CodebaseIndex) -> List[Callable[..., str]]:
    """Build strands tools bound to ``index`` for one verification agent.

    Postconditions:
        - Returns seven tools (``read_file``, ``read_lines``, ``read_function``,
          ``list_files``, ``search_codebase``, ``find_function_at_line``,
          ``find_references``) that delegate to ``index``; each returns a
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
        try:
            return index.read_file(path)
        except Exception as exc:
            return f"Error: could not read {path!r}: {type(exc).__name__}: {exc}"

    @tool
    def read_lines(path: str, start: int, end: int) -> str:
        """Read an inclusive 1-based line range from a file under review.

        Prefer this over read_file when you only need a bounded slice. The
        maximum span is 400 lines; use a narrower range or read_function for
        larger constructs. start/end and the returned line numbers are the
        file's real (original) line numbers, even though the underlying
        content itself may be a bounded diff excerpt rather than the whole
        file.

        Args:
            path: File path (same paths accepted by read_file).
            start: 1-based inclusive start line.
            end: 1-based inclusive end line.

        Returns:
            A header plus ``N| content`` lines, or an ``Error: ...`` message.
        """
        try:
            return index.read_lines(path, start, end)
        except Exception as exc:
            return (
                f"Error: could not read_lines {path!r} [{start}:{end}]: {type(exc).__name__}: {exc}"
            )

    @tool
    def read_function(path: str, name_or_line) -> str:
        """Read one function/method/class body by line number or exact name.

        Pass a positive integer for line-based lookup, or a name such as
        ``foo`` / ``Class.method`` for exact name lookup. A string of only
        digits (e.g. ``"12"``) is treated as a line number, not a name —
        prefer an int when you mean a line.

        Args:
            path: File path (same paths accepted by read_file).
            name_or_line: 1-based line number (int or digit-only string) or
                exact construct name (any other non-empty string).

        Returns:
            Header plus ``N| content`` lines, or an ``Error: ...`` message.
        """
        try:
            if isinstance(name_or_line, bool):
                return f"Error: name_or_line must be a line number or name, got {name_or_line!r}."
            if isinstance(name_or_line, int):
                return index.read_function(path, name_or_line)
            if isinstance(name_or_line, str) and name_or_line.strip().isdigit():
                return index.read_function(path, int(name_or_line.strip()))
            if isinstance(name_or_line, str):
                return index.read_function_by_name(path, name_or_line)
            return f"Error: name_or_line must be a line number or name, got {name_or_line!r}."
        except Exception as exc:
            return (
                f"Error: could not read_function {path!r} ({name_or_line!r}): "
                f"{type(exc).__name__}: {exc}"
            )

    @tool
    def list_files() -> str:
        """List every file path available to read in the code under review.

        Returns:
            One path per line. Read any of them with read_file(path).
        """
        try:
            paths = index.list_files()
            return "\n".join(paths) if paths else "(no files available)"
        except Exception as exc:
            return f"Error: could not list files: {type(exc).__name__}: {exc}"

    @tool
    def search_codebase(query: str) -> str:
        """Search every in-memory file (submission files and existing-codebase
        excerpt) for a substring (case-insensitive).

        Does not search repository files reached only via the repo reader —
        inspect those with ``read_file`` / ``list_files`` instead. Use this to
        find where a symbol is defined, imported, registered, used, or tested
        before deciding whether a finding is real — e.g. search for a function
        name a finding claims is "never defined". Returned line numbers are
        the file's real (original) line numbers, even though the underlying
        content itself may be a bounded diff excerpt rather than the whole
        file.

        Args:
            query: The substring to search for (e.g. a function or class name).

        Returns:
            Matching "path:line: text" lines, or a message that nothing matched.
        """
        try:
            matches = index.search(query)
            if not matches:
                return f"No matches for {query!r}."
            return "\n".join(f"{path}:{lineno}: {text}" for path, lineno, text in matches)
        except Exception as exc:
            return f"Error: could not search for {query!r}: {type(exc).__name__}: {exc}"

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
        try:
            if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
                return f"Error: line_number must be a positive integer, got {line_number!r}."
            resolved = index.resolve_path(path)
            if not resolved:
                return f"Error: {path!r} is not a readable path."
            content = index.read_file_or_none(path)
            if content is None:
                return f"Error: {path!r} is not a readable path."
            display_path = resolved if resolved != index.EXISTING_CODEBASE_PATH else path
            # Helpers ``_strip_numbered_prefixes``, ``_find_python_function_at_line``,
            # and ``_find_heuristic_function_at_line`` all take 1-based line numbers
            # (matching this tool's public contract) — no 0-based conversion.
            # Strip ``N: `` line-number prefixes that the PR-review path injects via
            # ``render_annotated_hunks``; remap to the physical line index so the
            # helper functions operate on plain code, then restore original numbers
            # in the output via ``display_line`` / ``line_mapper``.
            stripped, physical, mapper = _strip_numbered_prefixes(content, line_number)
            _, ext = os.path.splitext(display_path)
            if ext.lower() in (".py", ".pyi"):
                return _find_python_function_at_line(
                    stripped, physical, display_path, display_line=line_number, line_mapper=mapper
                )
            return _find_heuristic_function_at_line(
                stripped, physical, display_path, display_line=line_number, line_mapper=mapper
            )
        except Exception as exc:
            return f"Error: could not inspect {path!r} at line {line_number}: {type(exc).__name__}: {exc}"

    @tool
    def find_references(symbol: str) -> str:
        """Find bounded path:line references to a symbol across the submission
        and (when attached) the wider repository, each with a short excerpt.

        Use this to check whether a finding's claim about a symbol's usage
        (e.g. "never called", "unused import") holds up, without manually
        combining search_codebase and read_lines.

        Args:
            symbol: The function, class, or variable name to search for.

        Returns:
            Newline-separated reference blocks with excerpts, or a message
            that nothing matched / access is limited to this submission.
        """
        try:
            return index.find_references(symbol)
        except Exception as exc:
            return f"Error: could not find references for {symbol!r}: {type(exc).__name__}: {exc}"

    return [
        read_file,
        read_lines,
        read_function,
        list_files,
        search_codebase,
        find_function_at_line,
        find_references,
    ]


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

    def __post_init__(self) -> None:
        if self.is_false_positive and self.confidence not in ("high", "medium"):
            raise ValueError(
                "is_false_positive=True requires confidence 'high' or 'medium', "
                f"got confidence={self.confidence!r}"
            )


def _coerce_verdict(item: object) -> Optional[Tuple[int, _Verdict]]:
    """Parse one raw verdict dict into ``(index, _Verdict)``, or None.

    Postconditions:
        - Returns None for any item without a non-negative integer ``index``
          (bool, float, string, negative, or missing — a verdict we cannot map
          back to a finding is ignored, not guessed).
        - Builds ``is_false_positive`` from an explicit ``"high"``/``"medium"``
          confidence allowlist, not a denylist — see ``_Verdict``'s invariant for
          the exact shape and the module docstring's Fail-safe invariant for why
          (an off-contract confidence is ambiguous, and ambiguous findings are
          kept, never dropped). Never raises on malformed input.
    """
    if not isinstance(item, dict):
        return None
    raw_index = item.get("index")
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
        return None
    index = raw_index
    confidence = str(item.get("confidence", "") or "").strip().lower()
    is_real = item.get("is_real_issue")
    # Allowlist, not a denylist — see module docstring's Fail-safe invariant.
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
        - When multiple verdicts share an index, the first in-range verdict is
          kept and later duplicates are ignored with a warning (first-wins so a
          later malformed or incorrect entry cannot overwrite a valid earlier one).
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
            if index in verdicts:
                logger.warning(
                    "FalsePositiveFilter: duplicate verdict for index %s, ignoring duplicate",
                    index,
                )
                continue
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


def code_fence_for(content: str) -> str:
    """Public wrapper for :func:`_code_fence_for`.

    This is intentionally exposed for reuse by other prompt-building passes
    (e.g. the merged architecture/side-effect pass) without reaching into
    private helper names.
    """
    return _code_fence_for(content)


def _sanitize_finding_field(text: str) -> str:
    """Collapse whitespace and neutralize prompt-structure metacharacters.

    Finding ``description`` / ``suggestion`` text is untrusted reviewer output.
    Runs of three or more backticks can mimic a CommonMark fence; runs of three
    or more hyphens can mimic the ``--- Finding index i ---`` separators this
    module emits. Breaking those runs with U+200B keeps the text readable while
    preventing structural corruption of the verifier prompt.

    Preconditions:
        - ``text`` is a string (may be empty).

    Postconditions:
        - Returns a single line (all whitespace collapsed to spaces).
        - Contains no run of three or more consecutive backticks or hyphens.
        - Never raises.
    """
    collapsed = " ".join(text.split())

    def _break_runs(match: re.Match[str]) -> str:
        return "\u200b".join(match.group())

    collapsed = re.sub(r"`{3,}", _break_runs, collapsed)
    collapsed = re.sub(r"-{3,}", _break_runs, collapsed)
    return collapsed


def _render_finding_block(i: int, issue: CodeReviewIssue) -> List[str]:
    """Render one indexed finding block (anchor line + metadata) for the prompt.

    Postconditions:
        - Returns the lines for finding ``i``: an ``--- Finding index i ---``
          anchor the verdict contract refers back to, a severity/category/
          location line, the description, and the suggestion when present.
        - ``description`` and ``suggestion`` are whitespace-normalized and
          sanitized via ``_sanitize_finding_field`` so multi-line or oddly
          spaced text collapses to a single prompt line and backtick / ``---``
          runs cannot corrupt the surrounding prompt structure. The structural
          finding-index anchor is built here and is not passed through the
          sanitizer.
    """
    location = issue.file_path or "(file unknown)"
    if issue.line is not None:
        location = f"{location}:{issue.line}"
    block = [
        f"--- Finding index {i} ---",
        f"severity: {issue.severity} | category: {issue.category} | location: {location}",
        f"description: {_sanitize_finding_field(issue.description)}",
    ]
    if issue.suggestion:
        block.append(f"suggestion: {_sanitize_finding_field(issue.suggestion)}")
    return block


def _cap_context_field(text: str) -> str:
    """Truncate an inlined task/AC field to ``_CONTEXT_FIELD_CHARS``.

    Preconditions: ``text`` is a non-None string (may be empty).
    Postconditions: returns ``text`` unchanged when within the cap; otherwise
        a prefix of length ``_CONTEXT_FIELD_CHARS`` plus
        ``_CONTEXT_FIELD_TRUNCATION_MARKER``. Never raises.
    """
    if len(text) <= _CONTEXT_FIELD_CHARS:
        return text
    return text[:_CONTEXT_FIELD_CHARS] + _CONTEXT_FIELD_TRUNCATION_MARKER


def _build_group_prompt(
    index: CodebaseIndex,
    file_path: str,
    issues: List[CodeReviewIssue],
    input_data: CodeReviewInput,
) -> str:
    """Render the user prompt for verifying one file's findings.

    The prompt inlines the cited file's full content (so the model has the
    primary evidence even without a tool call) and lists up to
    ``_MANIFEST_LIMIT`` available paths; other files (including any manifest
    overflow) and the existing-codebase excerpt remain reachable through the
    tools. The wording is a stable anchor for the verdict contract: it names
    the file, indexes each finding, and asks for a ``verdicts`` array.

    Preconditions:
        - ``file_path`` is a canonical key previously returned by
          ``index.resolve_path`` (the production filter only groups resolved
          paths). Unreadable keys still degrade to a placeholder rather than
          raising.

    Postconditions:
        - The returned text contains one indexed block per finding (index 0..n-1
          matching ``issues`` order) and inlines the cited file's full body
          when readable, otherwise a ``(file content unavailable)`` placeholder.
          The task description and each acceptance criterion are capped at
          ``_CONTEXT_FIELD_CHARS`` so an oversized task field cannot dominate
          the prompt.
        - Never raises.
    """
    parts: List[str] = []
    task = input_data.task_description.strip()
    if task:
        parts.append(f"**Task being implemented:** {_cap_context_field(task)}")
    if input_data.acceptance_criteria:
        parts.append("**Acceptance criteria:**")
        parts.extend(f"- {_cap_context_field(c)}" for c in input_data.acceptance_criteria)
        parts.append("")

    manifest = index.list_files()
    parts.append(
        f"**Files available to read ({len(manifest)} total) — use read_file/search_codebase:**"
    )
    parts.extend(manifest[:_MANIFEST_LIMIT])
    if len(manifest) > _MANIFEST_LIMIT:
        parts.append(f"... and {len(manifest) - _MANIFEST_LIMIT} more (call list_files()).")
    parts.append("")

    body = index.read_file_or_none(file_path)
    if body is None:
        body = "(file content unavailable)"
    fence = _code_fence_for(body)
    parts.append(f"**Full content of `{file_path}` (the file the findings below are about):**")
    parts.append(fence)
    parts.append(body)
    parts.append(fence)
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
) -> Dict[int, _Verdict]:
    """Run one verification LLM call over one batch of a single file's findings.

    ``issues`` is the whole file's findings unless the caller split them into
    multiple batches under ``CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP``; this
    function has no notion of "the whole file" and simply verifies whatever
    slice it is given.

    Postconditions:
        - Returns ``{finding_index: _Verdict}`` for the findings the model gave
          a parseable, in-range verdict on; findings with no verdict are absent
          (and therefore kept by the caller). ``finding_index`` is the 0-based
          position of the finding within ``issues`` (i.e. a valid index into
          the ``issues``/``group`` list the caller passed in), so callers may
          index back into their own list with it.
    """
    prompt = _build_group_prompt(index, file_path, issues, input_data)
    agent = Agent(
        model=model,
        system_prompt=FALSE_POSITIVE_VERIFY_PROMPT,
        tools=_build_tools(index),
    )
    raw = str(agent(prompt)).strip()
    data = extract_json_from_response(raw)
    return _parse_verdicts(data, len(issues))


def filter_false_positives(
    llm: LLMClient,
    input_data: CodeReviewInput,
    issues: List[CodeReviewIssue],
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
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
        - ``index``, when given, must have been built from this same
          ``input_data``/``repo_reader`` (the coordinator shares one index
          across this filter and the architecture-consistency pass rather than
          each rebuilding it); ``None`` builds a fresh one, so any caller that
          does not have one yet is unaffected.

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
          path, or when the submission exposes no readable files and no
          ``repo_reader`` was provided.
        - When ``repo_reader`` is provided, the verifier can additionally read
          existing repository files outside the diff, so it can drop findings
          that claim an existing file/module is missing.
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
        return _verify_and_filter(llm, input_data, issues, repo_reader, index)
    except Exception as exc:  # noqa: BLE001 - fail-safe: verification must never break the review
        logger.warning(
            "FalsePositiveFilter: verification failed during setup (%s: %s); keeping all findings",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return list(issues)


def _verify_and_filter(
    llm: LLMClient,
    input_data: CodeReviewInput,
    issues: List[CodeReviewIssue],
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> List[CodeReviewIssue]:
    """Core of :func:`filter_false_positives`; may raise on setup errors.

    Split out so its sole caller can wrap it in the fail-safe guard: model
    resolution and context sizing happen here (outside the per-group loop) and
    can raise, and the caller turns any such error into "keep all findings".

    Preconditions:
        - The caller has already confirmed at least one issue in ``issues`` has
          a non-blank file path (otherwise this is a wasted call, not a bug).
        - ``index``, when given, was built from this same ``input_data``/
          ``repo_reader``.

    Postconditions:
        - Same removal contract as :func:`filter_false_positives`, minus the
          env-toggle and blank-path early returns the caller already handled.
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files and index.repo_reader is None:
        # No readable submission files AND no repo reader — the legacy ``code``
        # blob had no path-headed content and there is no repository to consult.
        # We cannot show the verifier any real code, so we cannot responsibly
        # drop anything. (With a reader attached, a finding citing an existing
        # repo file is still verifiable, so we proceed.)
        return list(issues)

    model = resolve_code_review_verify_model(llm)

    # Group findings by the resolved canonical path of their cited file so each
    # verification call shares one real file's context (and can still read any
    # other file via the tools). A finding whose cited file is absent from the
    # submission (or is ambiguous) is kept without a verification call: the
    # verifier would have no primary file to read, so the call would inline an
    # error string, waste an LLM round, and still keep the finding (fail-safe).
    groups: OrderedDict[str, List[CodeReviewIssue]] = OrderedDict()
    group_orig_indices: Dict[str, List[int]] = {}
    for orig_idx, issue in enumerate(issues):
        if not (issue.file_path or "").strip():
            continue
        resolved = index.resolve_path(issue.file_path)
        if resolved is None:
            logger.debug(
                "FalsePositiveFilter: keeping finding for unresolved path %r (not in submission)",
                issue.file_path,
            )
            continue
        groups.setdefault(resolved, []).append(issue)
        group_orig_indices.setdefault(resolved, []).append(orig_idx)

    # Each group is an independent verification LLM call over the same read-only
    # index, so they fan out: with N cited files the wall-clock is the slowest
    # single call, not the sum. A per-group failure keeps that group's findings
    # (best-effort), exactly as the sequential path did, and the merge below
    # consumes results in submission order so the outcome stays deterministic.
    #
    # A file whose finding count exceeds _verify_max_findings_per_group() is
    # split here into multiple same-sized batches, each becoming its own
    # group_items entry (its own _verify_group call), so no single prompt
    # inlines more than the cap. group_items and group_orig_index_batches are
    # positionally aligned by list index rather than keyed by file_path, since
    # one file_path can now produce more than one entry.
    max_per_group = _verify_max_findings_per_group()
    group_items: List[Tuple[str, List[CodeReviewIssue]]] = []
    group_orig_index_batches: List[List[int]] = []
    for file_path, group in groups.items():
        orig_indices = group_orig_indices[file_path]
        if len(group) > max_per_group:
            batch_count = -(-len(group) // max_per_group)
            logger.info(
                "FalsePositiveFilter: splitting %s findings for %s into %s batches "
                "of up to %s (CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP)",
                len(group),
                file_path,
                batch_count,
                max_per_group,
            )
        for start in range(0, len(group), max_per_group):
            group_items.append((file_path, group[start : start + max_per_group]))
            group_orig_index_batches.append(orig_indices[start : start + max_per_group])

    def _verify_one(item: Tuple[str, List[CodeReviewIssue]]) -> Dict[int, _Verdict]:
        file_path, group = item
        try:
            return _verify_group(model, index, file_path, group, input_data)
        except Exception as exc:  # noqa: BLE001 - best-effort; a failure must keep findings, not drop them
            logger.warning(
                "FalsePositiveFilter: verification failed for %s (%s: %s); keeping its findings",
                file_path,
                type(exc).__name__,
                _truncate_for_log(str(exc)),
            )
            return {}

    workers = min(_verify_parallelism(), len(group_items))
    if workers <= 1:
        group_verdicts = [_verify_one(item) for item in group_items]
    else:
        # Fan out via the shared parallel_map helper instead of a hand-rolled
        # ThreadPoolExecutor: same bounded-pool/per-item-timeout semantics as
        # before, but with contextvars (trace_id, LLM attribution) now
        # propagated into each worker. _verify_one already catches every
        # exception internally and returns {} (never raises), so only
        # parallel_map's timeout path is ever exercised here.
        timeout = _verify_timeout_seconds()

        def _on_verify_timeout(item: Tuple[str, List[CodeReviewIssue]]) -> None:
            file_path, _group = item
            logger.warning(
                "FalsePositiveFilter: verification timed out after %ss for %s; keeping its findings",
                timeout,
                file_path,
            )

        raw_results = parallel_map(
            group_items,
            _verify_one,
            max_workers=workers,
            skip_none=False,
            timeout=timeout,
            on_timeout=_on_verify_timeout,
        )
        # skip_none=False + preserve_order=True (default) keeps a degraded
        # (timed-out) slot as None in place, so this stays positionally
        # aligned with group_items for the zip-based merge below. A timeout
        # means "keep the group's findings" (fail-safe), i.e. an empty
        # verdict dict — same as the old FuturesTimeoutError handler.
        group_verdicts = [r if r is not None else {} for r in raw_results]

    removed_indices: set[int] = set()
    for (file_path, group), orig_indices, verdicts in zip(
        group_items, group_orig_index_batches, group_verdicts
    ):
        group_len = len(group)
        for idx, verdict in verdicts.items():
            if not isinstance(idx, int) or idx < 0 or idx >= group_len:
                logger.warning(
                    "FalsePositiveFilter: ignoring out-of-range verdict index %r for %s (group size %s)",
                    idx,
                    file_path,
                    group_len,
                )
                continue
            if verdict.is_false_positive:
                issue = group[idx]
                removed_indices.add(orig_indices[idx])
                logger.info(
                    "FalsePositiveFilter: dropping false positive [%s] %s:%s — %s (%s)",
                    issue.severity,
                    issue.file_path,
                    issue.line if issue.line is not None else "-",
                    _truncate_for_log(issue.description),
                    _truncate_for_log(verdict.reasoning) or "no reasoning given",
                )

    if not removed_indices:
        return list(issues)
    kept = [i for orig_idx, i in enumerate(issues) if orig_idx not in removed_indices]
    logger.info(
        "FalsePositiveFilter: removed %s of %s findings as false positives",
        len(issues) - len(kept),
        len(issues),
    )
    return kept
