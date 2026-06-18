"""Shared LLM-based code-review fallback for the V2 sub-teams.

The backend and frontend Code-V2 teams each fall back to an LLM-driven review
when no external ``code_review_agent`` is available (or it raises). The
orchestration around that fallback — function-aware chunking, per-chunk prompt
formatting, parsing, and issue construction — is identical for both teams; only
the prompt, the parser, and the issue type differ. This module owns that shared
orchestration so it lives in one place; each team passes in its own
``REVIEW_PROMPT``, ``parse_review_template``, and ``ReviewIssue`` factory.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, TypeVar

from software_engineering_team.shared.models import Task

logger = logging.getLogger(__name__)

# The two V2 teams each own a distinct ``ReviewIssue`` type, so the helper is
# generic over whatever the caller's ``issue_factory`` produces.
IssueT = TypeVar("IssueT")


def run_llm_review(
    *,
    task: Task,
    files: Dict[str, str],
    prompt_template: str,
    parse_template: Callable[[str], Dict[str, Any]],
    issue_factory: Callable[..., IssueT],
    invoke_model: Callable[[str], str],
    max_chars: int,
    warn_threshold: int,
) -> List[IssueT]:
    """LLM-based code review when no external review agent is available.

    Preconditions:
        - ``files`` maps file paths to their full source text.
        - ``prompt_template`` accepts ``requirements``, ``acceptance_criteria``,
          and ``code`` format fields.
        - ``parse_template`` returns a dict that may contain an ``"issues"`` list
          of dicts.
        - ``issue_factory`` is a callable accepting keyword arguments ``source``,
          ``severity``, ``description``, ``file_path``, and ``recommendation``
          (e.g. each team's ``ReviewIssue``); an incompatible factory raises
          ``TypeError``.
        - ``invoke_model`` runs one prompt through the team's LLM and returns the
          raw text response.
        - ``max_chars`` > 0 and ``warn_threshold`` >= 0.

    Postconditions:
        - Inputs that exceed the per-call budget are split into function-aware
          chunks (cuts land between whole functions/methods, never mid-body)
          and every chunk is reviewed; issues from all chunks are returned.
          No file content is silently truncated away, so a large file's tail is
          reviewed rather than dropped. Blank files contribute nothing.
        - A chunk that is itself over budget (a single line longer than the cap,
          e.g. a minified bundle) is hard-split before the LLM call, with the
          ``### path ###`` header re-attached to every piece (``cap_review_chunk``)
          so it is never sent in one prompt that may overflow the context and be
          skipped, and a finding in any tail piece stays attributable to its file.
        - A chunk whose LLM call or parse fails is logged and skipped; issues
          from the other chunks are still returned (one bad chunk never aborts
          the whole review).
        - Small inputs are reviewed in a single call, as before.
    """
    # Imported lazily (not at module level) so importing this helper does not
    # pull in the whole code_review_agent package; this also matches the V2
    # teams' existing convention and avoids assuming the
    # software_engineering_team package dir is itself on sys.path.
    from software_engineering_team.code_review_agent.coordinator import (
        build_review_chunks,
        cap_review_chunk,
    )

    blocks = [(path, content) for path, content in files.items() if content and content.strip()]
    if not blocks:
        return []
    # Budget each prompt at the same per-call size the old code truncated to,
    # but split at function/method boundaries so no construct is severed and no
    # file tail is dropped.
    chunks = list(build_review_chunks(blocks, max_chars))
    if len(chunks) > warn_threshold:
        logger.warning(
            "LLM code review: %d chunks for %d file(s) — large review, many LLM calls",
            len(chunks),
            len(blocks),
        )
    else:
        logger.debug("LLM code review: %d chunk(s) for %d file(s)", len(chunks), len(blocks))
    issues: List[IssueT] = []
    for idx, chunk in enumerate(chunks, start=1):
        logger.debug("LLM code review: reviewing chunk %d/%d", idx, len(chunks))
        # A chunk holding a single line longer than the cap is over budget by
        # contract; hard-split it so the whole file is reviewed rather than sent
        # in one prompt that may overflow and be skipped — keeping the file
        # header on every piece so tail findings stay attributable.
        for piece in cap_review_chunk(chunk, max_chars):
            prompt = prompt_template.format(
                requirements=task.requirements or task.description,
                acceptance_criteria=", ".join(task.acceptance_criteria)
                if task.acceptance_criteria
                else "N/A",
                code=piece,
            )
            try:
                raw = invoke_model(prompt)
                data = parse_template(raw)
            except Exception as exc:
                logger.warning("LLM code review: chunk %d/%d failed: %s", idx, len(chunks), exc)
                continue
            for item in data.get("issues") or []:
                if isinstance(item, dict):
                    issues.append(
                        issue_factory(
                            source=item.get("source", "code_review"),
                            severity=item.get("severity", "medium"),
                            description=item.get("description", ""),
                            file_path=item.get("file_path", ""),
                            recommendation=item.get("recommendation", ""),
                        )
                    )
    return issues
