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

import logging
from typing import Any, Callable, Dict, List, TypeVar

logger = logging.getLogger(__name__)

# Each V2 team owns a distinct ``ReviewIssue`` type, so the helpers are generic
# over whatever the caller's ``issue_factory`` produces.
IssueT = TypeVar("IssueT")


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
        - A finding's ``file_path`` defaults to the file actually sent when the
          agent does not report a location, so every piece stays attributable.
        - A piece whose ``run_chunk`` call fails is logged and skipped; issues
          from the other pieces are still returned (one bad piece never aborts
          the whole review).
        - A file that fits in one segment is reviewed in a single call.
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
    for idx, (path, piece) in enumerate(pieces, start=1):
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
            continue
        for item in items or []:
            issues.append(
                issue_factory(
                    source=source,
                    severity=getattr(item, "severity", default_severity),
                    description=getattr(item, "description", str(item)),
                    # `location` may be present but None; fall back to file_path
                    # then to the file we sent, so file_path is always a useful
                    # string and tail pieces stay attributable.
                    file_path=getattr(item, "location", None)
                    or getattr(item, "file_path", None)
                    or path,
                    recommendation=getattr(item, "recommendation", ""),
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
) -> List[IssueT]:
    """Run the external QA agent over each file's raw, function-aware-split source.

    Preconditions:
        - ``qa_agent`` is not None and exposes ``.run(QAInput) -> QAOutput``.

    Postconditions: see ``run_chunked_agent_review``; QA bugs become issues with
    ``source="qa"``.
    """
    from qa_agent.models import QAInput as _QAInput

    def _run_chunk(code: str) -> Any:
        result = qa_agent.run(
            _QAInput(code=code, language=language, task_description=task_description)
        )
        return getattr(result, "bugs_found", getattr(result, "issues", []))

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
) -> List[IssueT]:
    """Run the external security agent over each file's raw, function-aware-split source.

    Preconditions:
        - ``security_agent`` is not None and exposes
          ``.run(SecurityInput) -> SecurityOutput``.

    Postconditions: see ``run_chunked_agent_review``; vulnerabilities become
    issues with ``source="security"``.
    """
    from security_agent.models import SecurityInput as _SecInput

    def _run_chunk(code: str) -> Any:
        result = security_agent.run(
            _SecInput(code=code, language=language, task_description=task_description)
        )
        return getattr(result, "vulnerabilities", getattr(result, "issues", []))

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
    )
