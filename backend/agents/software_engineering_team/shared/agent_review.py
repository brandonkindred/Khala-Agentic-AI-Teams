"""Shared QA/security agent-review orchestration for the V2 sub-teams.

The backend and frontend Code-V2 teams both run external QA and security agents
over the files a task produced. The orchestration around those agents —
function-aware chunking of each file's raw source, per-piece invocation,
skip-on-failure, and issue construction — is identical for both teams; only the
``ReviewIssue`` type differs. This module owns that shared orchestration so it
lives in one place; each team passes in its own ``ReviewIssue`` factory.

QA and security agents analyze *source*, so they are fed each file's **raw**
content split at function/method boundaries — not the code-review renderer's
``### path ###`` headers or ``N:`` line-number prefixes (those exist only for the
code-review prompt's line anchoring and would make the code syntactically
invalid, provoking bogus findings).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

# Each V2 team owns a distinct ``ReviewIssue`` type, so the helpers are generic
# over whatever the caller's ``issue_factory`` produces.
IssueT = TypeVar("IssueT")


class AgentReviewCache:
    """Per-run cache of per-piece QA/security review outcomes, keyed on exact LLM input.

    Complementary to ``code_review_agent.mapping``'s chunk-outcome cache (which
    covers code review), for the QA/security agents ``run_chunked_agent_review``
    drives. Deliberately NOT process-global like that cache: the caller
    constructs one instance per fix loop (see ``run_chunked_agent_review``'s
    ``cache`` precondition) and discards it when the loop ends, so a verdict
    never leaks across unrelated runs or tasks.

    Invariants:
        - A caller can never observe or mutate the cache's internal state:
          both ``get`` and ``put`` deep-copy the items, so mutating a
          returned/stored item's fields — not just appending to the list
          itself — never affects the other's copy.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, List[Any]] = {}

    def get(self, key: str) -> Optional[List[Any]]:
        """Return a deep copy of the items stored under ``key``, or None on a miss."""
        cached = self._entries.get(key)
        return copy.deepcopy(cached) if cached is not None else None

    def put(self, key: str, items: List[Any]) -> None:
        """Store a deep copy of ``items`` under ``key``, overwriting any prior entry."""
        self._entries[key] = copy.deepcopy(items)


def _piece_cache_key(source: str, cache_context: str, piece: str) -> str:
    """Hash of one review piece's exact LLM input (source/context/raw content).

    Postconditions:
        - Two calls collide only when ``source``, ``cache_context``, and
          ``piece`` are all identical — mirroring
          ``code_review_agent.mapping._chunk_cache_key``'s "exact LLM input" key
          design — so any edit to the piece's content (or a different
          language/task_description folded into ``cache_context``) changes the
          digest and naturally invalidates a prior entry. Hashing a JSON array
          (rather than a flat NUL-joined string) keeps this true even when a
          field contains a literal NUL byte, which would otherwise let two
          different ``(cache_context, piece)`` pairs join to an identical body
          string.
    """
    body = json.dumps([source, cache_context, piece])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def run_chunked_agent_review(
    *,
    run_chunk: Callable[[str], Any],
    files: Dict[str, str],
    source: str,
    default_severity: str,
    label: str,
    task_id: str,
    issue_factory: Callable[..., IssueT],
    max_chars: int,
    warn_threshold: int,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
    cache_context: str = "",
    failure_severity: str = "critical",
) -> List[IssueT]:
    """Run a quality agent over each file's raw, function-aware-split source.

    Preconditions:
        - ``run_chunk(code)`` invokes the agent on one piece of raw source and
          returns its raw issue/vulnerability items.
        - ``files`` maps file paths to their full source text.
        - ``issue_factory`` accepts keyword arguments ``source``, ``severity``,
          ``description``, ``file_path``, and ``recommendation`` (each team's
          ``ReviewIssue``); an incompatible factory raises ``TypeError``.
        - ``max_chars`` > 0 and ``warn_threshold`` >= 0.
        - ``cache``, when given, is scoped to the caller's own fix loop (e.g. one
          microtask's review-cycle lifetime) — never shared across unrelated
          runs or tasks. ``cache_context`` folds in whatever besides the piece's
          raw content affects the agent's verdict (e.g. language/task
          description); a caller that omits it while distinguishing calls only
          by those fields risks a false cache hit.
        - ``failure_severity`` should be a severity the caller's blocking policy
          treats as blocking (e.g. "critical"/"high") — it drives the synthetic
          "review incomplete" issue below, and a non-blocking value would defeat
          its purpose.

    Postconditions:
        - Each non-blank file is split at function/method boundaries via
          ``split_block_into_segments`` (the same function-aware splitter
          ``build_review_chunks`` uses) and every segment's **raw** content is
          reviewed — no ``### path ###`` header, no ``N:`` line prefixes — so the
          agent receives valid source and no file content is truncated away.
          Blank files contribute nothing.
        - A segment still over ``max_chars`` (a single line longer than the cap,
          e.g. a minified bundle) is hard-split at character boundaries so no
          over-budget string is ever sent; the agent is invoked one piece at a
          time.
        - A finding's ``file_path`` is the agent's own ``file_path`` field when
          present, else the file actually sent, so every piece stays
          attributable to a real key in the caller's file map. ``location``
          (free text -- "file path, function name, or line reference") is
          never used as ``file_path``; when present it is folded into
          ``description`` instead.
        - A piece whose ``run_chunk`` call fails is logged and skipped; issues
          from the other pieces are still returned (one bad piece never aborts
          the whole review). Such a piece is never cached, so it is retried for
          real on the next call.
        - If every piece that actually attempted a fresh ``run_chunk`` call
          this invocation failed (no partial coverage to fall back on), a
          single synthetic issue at ``failure_severity`` is appended instead
          of silently returning an empty list: an empty result is otherwise
          indistinguishable from "reviewed cleanly, no findings," and a
          downstream gate that only checks for blocking issues would pass a
          microtask that was never actually reviewed. A piece served from
          ``cache`` already carries a real verdict from an earlier call, so it
          counts toward neither "attempted" nor "failed" here — it must not
          silently outnumber a failed fresh attempt into looking like partial
          coverage (e.g. one cached-clean file plus one changed file whose
          only fresh attempt fails must still fail closed, not read as "1 of 2
          pieces failed, stay lenient"). A mix of failed and successful fresh
          attempts still keeps today's lenient "one bad piece never aborts the
          whole review" behavior, since partial coverage is still real
          coverage.
        - A file that fits in one segment is reviewed in a single call.
        - When ``cache`` is given, a piece whose exact LLM input (``source`` +
          ``cache_context`` + raw content) was already reviewed earlier in the
          cache's lifetime is served from the cache instead of calling
          ``run_chunk`` again, producing identical issues. When ``cache`` is
          None (the default), behavior is identical to today — a pure
          passthrough.
    """
    # Imported lazily (not at module level) so importing this helper does not
    # pull in the whole code_review_agent package; this also matches the V2
    # teams' existing convention and avoids assuming the
    # software_engineering_team package dir is itself on sys.path.
    from software_engineering_team.code_review_agent.coordinator import (
        cap_chunk_content,
        split_block_into_segments,
    )

    blocks = [(path, content) for path, content in files.items() if content and content.strip()]
    if not blocks:
        return []
    # Function-aware split per file (cuts land between whole functions/methods),
    # feeding RAW seg.content so the agents get valid source. cap_chunk_content
    # is only a fallback for a single line longer than the cap, which no
    # function boundary can bound.
    pieces = [
        (path, piece)
        for path, content in blocks
        for seg in split_block_into_segments(path, content, max_chars)
        for piece in cap_chunk_content(seg.content, max_chars)
    ]
    if len(pieces) > warn_threshold:
        logger.warning(
            "[%s] %s: %d pieces for %d file(s) — large review, many calls%s",
            task_id,
            label,
            len(pieces),
            len(blocks),
            context,
        )
    issues: List[IssueT] = []
    failed = 0
    attempted = 0
    for idx, (path, piece) in enumerate(pieces, start=1):
        cache_key = _piece_cache_key(source, cache_context, piece) if cache is not None else None
        items = cache.get(cache_key) if cache_key is not None else None
        if items is None:
            attempted += 1
            try:
                items = run_chunk(piece)
            except Exception as exc:
                logger.warning(
                    "[%s] %s failed (piece %d/%d)%s: %s",
                    task_id,
                    label,
                    idx,
                    len(pieces),
                    context,
                    exc,
                )
                failed += 1
                continue
            # Materialize once so a one-shot iterable (e.g. a generator) isn't
            # exhausted by the cache write, leaving nothing for the loop below.
            items = list(items or [])
            if cache_key is not None:
                cache.put(cache_key, items)
        for item in items or []:
            description = getattr(item, "description", str(item))
            # `file_path` (when the agent's model declares it) is a verified
            # real path; `path` is the exact file we sent, always a real key
            # into the caller's file map. `location` is free text ("file
            # path, function name, or line reference") and must never
            # override either -- an exact-match downstream lookup keyed on
            # file_path would silently miss and fall back to arbitrary
            # files. Fold `location` into the description instead, so its
            # hint isn't discarded, just no longer treated as authoritative.
            file_path = getattr(item, "file_path", None) or path
            location = getattr(item, "location", None)
            if location and location not in file_path and location not in description:
                description = f"{description} (location: {location})"
            issues.append(
                issue_factory(
                    source=source,
                    severity=getattr(item, "severity", default_severity),
                    description=description,
                    file_path=file_path,
                    recommendation=getattr(item, "recommendation", ""),
                )
            )
    if failed and failed == attempted:
        # Zero completed reviews among the pieces that actually needed one this
        # call: every fresh attempt failed, so there is no partial coverage to
        # fall back on (unlike the mixed-outcome case above, which stays
        # lenient). Compared against `attempted`, not `len(pieces)` -- a cache
        # hit already has a real verdict from an earlier call and correctly
        # contributes nothing to either counter, so it must not silently
        # outnumber a failed fresh attempt into looking like partial coverage.
        # An empty `issues` here would be indistinguishable from a genuine
        # clean pass, silently defeating a downstream gate that only checks
        # for blocking issues -- surface it instead.
        issues.append(
            issue_factory(
                source=source,
                severity=failure_severity,
                description=(f"{label} could not complete review: all {failed} piece(s) failed"),
                file_path="",
                recommendation=f"Investigate and re-run {label.lower()}; no pieces were reviewed.",
            )
        )
    return issues


def run_qa_agent(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    issue_factory: Callable[..., IssueT],
    max_chars: int,
    warn_threshold: int,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> List[IssueT]:
    """Run the external QA agent over each file's raw, function-aware-split source.

    Preconditions:
        - ``qa_agent`` is not None and exposes ``.run(QAInput) -> QAOutput``.
        - ``cache``: see ``run_chunked_agent_review``.

    Postconditions: see ``run_chunked_agent_review``; QA bugs become issues with
    ``source="qa"``.
    """
    from software_engineering_team.qa_agent.models import QAInput as _QAInput

    def _run_chunk(code: str) -> Any:
        result = qa_agent.run(
            _QAInput(code=code, language=language, task_description=task_description)
        )
        bugs = getattr(result, "bugs_found", getattr(result, "issues", []))
        # QAExpertAgent's structured-output-failure fallback (qa_agent.agent._fallback)
        # returns a normal QAOutput with approved=False and an empty bugs_found rather
        # than raising — indistinguishable from a genuine clean pass unless checked here.
        # A real review never produces this combination (QAExpertAgent._finalize derives
        # approved from bugs_found for every non-fallback result), so raising routes a
        # fallback through the existing failed-piece path below: skipped, issues from
        # other pieces still returned, and never cached — a transient parse/model
        # failure is retried for real next time instead of being frozen as "no bugs".
        if not getattr(result, "approved", True) and not bugs:
            raise RuntimeError(getattr(result, "summary", "QA agent returned a failure fallback"))
        return bugs

    return run_chunked_agent_review(
        run_chunk=_run_chunk,
        files=files,
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id=task_id,
        issue_factory=issue_factory,
        max_chars=max_chars,
        warn_threshold=warn_threshold,
        context=context,
        cache=cache,
        cache_context=f"{language}\x00{task_description}",
        # Matches _QA_TESTING_PHASE_SPEC.missing_severity (backend_code_v2_team/
        # phases/review.py) -- the same severity already used one layer up when
        # the whole QA-agent call fails outright, so a total per-piece failure
        # here blocks the gate exactly as consistently as that existing path.
        failure_severity="high",
    )


def run_security_agent(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    issue_factory: Callable[..., IssueT],
    max_chars: int,
    warn_threshold: int,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> List[IssueT]:
    """Run the external security agent over each file's raw, function-aware-split source.

    Preconditions:
        - ``security_agent`` is not None and exposes
          ``.run(SecurityInput) -> SecurityOutput``.
        - ``cache``: see ``run_chunked_agent_review``.

    Postconditions: see ``run_chunked_agent_review``; vulnerabilities become
    issues with ``source="security"``.
    """
    from software_engineering_team.security_agent.models import SecurityInput as _SecInput

    def _run_chunk(code: str) -> Any:
        result = security_agent.run(
            _SecInput(code=code, language=language, task_description=task_description)
        )
        vulns = getattr(result, "vulnerabilities", getattr(result, "issues", []))
        # CybersecurityExpertAgent's structured-output-failure fallback
        # (security_agent.agent._fallback) returns a normal SecurityOutput with
        # approved=False and no vulnerabilities rather than raising — see the identical
        # rationale in run_qa_agent's _run_chunk just above.
        if not getattr(result, "approved", True) and not vulns:
            raise RuntimeError(
                getattr(result, "summary", "Security agent returned a failure fallback")
            )
        return vulns

    return run_chunked_agent_review(
        run_chunk=_run_chunk,
        files=files,
        source="security",
        default_severity="high",
        label="Security agent",
        task_id=task_id,
        issue_factory=issue_factory,
        max_chars=max_chars,
        warn_threshold=warn_threshold,
        context=context,
        cache=cache,
        cache_context=f"{language}\x00{task_description}",
        # Matches _SECURITY_TESTING_PHASE_SPEC.missing_severity (backend_code_v2_team/
        # phases/review.py) -- see run_qa_agent's identical rationale above.
        failure_severity="critical",
    )
