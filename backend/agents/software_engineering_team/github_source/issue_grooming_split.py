"""
Sub-issue splitting for GitHub issue grooming (Phase B).

Decomposes an oversized, already-scored issue (Phase A has already run) into one
sub-issue per acceptance-criteria checklist item, capped so a pathologically long
checklist still yields a bounded, deterministic number of children. Pure/rendering
logic only -- creating and linking the sub-issues via ``GitHubClient`` is the
runner's job (``issue_grooming_runner``), including the one genuinely
non-idempotent check (skip if the issue already has sub-issues).
"""

from __future__ import annotations

from .client import Issue
from .issue_grooming_scoring import (
    CHECKLIST_ITEM_RE,
    PHASE_B_END,
    PHASE_B_START,
    ScoreBreakdown,
    inject_marked_block,
    strip_grooming_blocks,
)

# Aggregate score at/above which an issue is a split candidate.
SPLIT_THRESHOLD = 8
# Minimum checklist items an issue must carry to be worth splitting.
MIN_SPLIT_ITEMS = 3
# Hard cap on sub-issues created per split, so a pathological checklist can't
# fan out unboundedly. Items past the cap are folded into one final
# "Remaining items" sub-issue rather than silently dropped.
MAX_SUB_ISSUES = 8

_TITLE_MAX = 120


def extract_checklist_items(body: str) -> list[str]:
    """Extract acceptance-criteria checklist item text from an issue body.

    Preconditions:
        - None.
    Postconditions:
        - Returns the text of every ``- [ ]``/``- [x]``/``* [ ]`` line in
          ``strip_grooming_blocks(body)`` (grooming's own previously-injected
          content is never treated as the issue's own checklist), in document
          order. Returns ``[]`` when there are none.
    """
    clean_body = strip_grooming_blocks(body)
    return [m.strip() for m in CHECKLIST_ITEM_RE.findall(clean_body)]


def should_split(score: ScoreBreakdown, checklist_items: list[str]) -> bool:
    """Decide whether an issue is a Phase B split candidate.

    Preconditions:
        - ``checklist_items`` is the result of :func:`extract_checklist_items`
          on the same issue ``score`` was computed for.
    Postconditions:
        - Returns True iff ``score.aggregate >= SPLIT_THRESHOLD`` and
          ``len(checklist_items) >= MIN_SPLIT_ITEMS``. Does not check whether the
          issue already has sub-issues -- that idempotency guard is the caller's
          (the runner's) responsibility, since it requires a network call this
          pure function must not make.
    """
    return score.aggregate >= SPLIT_THRESHOLD and len(checklist_items) >= MIN_SPLIT_ITEMS


def plan_sub_issue_items(checklist_items: list[str]) -> list[str]:
    """Cap ``checklist_items`` at :data:`MAX_SUB_ISSUES`, folding the tail into one item.

    Preconditions:
        - ``checklist_items`` is non-empty.
    Postconditions:
        - Returns ``checklist_items`` unchanged when its length is at most
          ``MAX_SUB_ISSUES``. Otherwise returns the first ``MAX_SUB_ISSUES - 1``
          items followed by one synthetic "Remaining items" entry summarizing the
          rest, so the total returned count never exceeds ``MAX_SUB_ISSUES`` and no
          item's text is silently dropped.
    """
    if len(checklist_items) <= MAX_SUB_ISSUES:
        return list(checklist_items)
    head = checklist_items[: MAX_SUB_ISSUES - 1]
    tail = checklist_items[MAX_SUB_ISSUES - 1 :]
    folded = f"Remaining items ({len(tail)}): " + "; ".join(tail)
    return [*head, folded]


def build_sub_issue(parent: Issue, item_text: str, index: int, total: int) -> tuple[str, str]:
    """Build the ``(title, body)`` for one sub-issue split off ``parent``.

    Preconditions:
        - ``index`` is 1-based; ``1 <= index <= total``.
    Postconditions:
        - ``title`` is ``"{parent.title} — {item_text}"`` truncated to
          ``_TITLE_MAX`` characters (an ellipsis replaces the tail when it would
          overflow), matching ``issue_proposals._proposal_title``'s
          budget-then-ellipsis truncation convention. ``body`` carries a
          provenance line naming the parent and the single checklist item
          rendered as this sub-issue's own acceptance criterion.
    """
    suffix = f" — {item_text}"
    budget = _TITLE_MAX - len(suffix)
    base_title = (
        parent.title
        if len(parent.title) <= budget
        else parent.title[: max(0, budget - 1)].rstrip() + "…"
    )
    title = f"{base_title}{suffix}"
    body = "\n".join(
        [
            f"Split from #{parent.number} ({parent.html_url}) by automated grooming ({index}/{total}).",
            "",
            "## Acceptance criteria",
            f"- [ ] {item_text}",
        ]
    )
    return title, body


def render_sub_issues_block(children: list[tuple[int, str]]) -> str:
    """Render the ``## Sub-issues`` markdown block listing created children.

    Preconditions:
        - ``children`` is a list of ``(number, title)`` pairs, in creation order.
    Postconditions:
        - Returns the block's content only (no surrounding markers); one
          ``- #{number} {title}`` line per child.
    """
    lines = ["## Sub-issues"] + [f"- #{number} {title}" for number, title in children]
    return "\n".join(lines)


def inject_sub_issues_block(body: str, children: list[tuple[int, str]]) -> str:
    """Inject/replace the Phase B sub-issues list in ``body``.

    Preconditions:
        - ``children`` is non-empty.
    Postconditions:
        - Equivalent to ``inject_marked_block(body, PHASE_B_START, PHASE_B_END,
          render_sub_issues_block(children))``.
    """
    return inject_marked_block(body, PHASE_B_START, PHASE_B_END, render_sub_issues_block(children))
