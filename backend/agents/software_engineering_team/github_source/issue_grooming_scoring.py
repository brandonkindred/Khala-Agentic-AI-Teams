"""
Heuristic Fibonacci complexity scoring for GitHub issue grooming (Phase A).

Scores an issue's conceptual/anticipated-LOC/solution-complexity dimensions from
its title and body alone -- no network calls, no LLM. LLM-assisted scoring is a
separate, deferred epic; this module is heuristic-only by design.

Also owns the marker-delimited grooming blocks Phase A (and Phase B, in
``issue_grooming_split``) inject into an issue body, so a re-run replaces its own
prior output in place instead of duplicating it, and so scoring never mistakes a
previously-injected block for part of the issue's own content.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, model_validator

FIBONACCI: tuple[int, ...] = (1, 2, 3, 5, 8, 13, 21)

PHASE_A_START = "<!-- khala-grooming:phase-a:start -->"
PHASE_A_END = "<!-- khala-grooming:phase-a:end -->"
PHASE_B_START = "<!-- khala-grooming:phase-b:start -->"
PHASE_B_END = "<!-- khala-grooming:phase-b:end -->"

# Single source of truth for "what counts as an acceptance-criteria checklist
# line", shared with issue_grooming_split's item extraction so the two modules
# can never disagree about what a checklist item looks like.
CHECKLIST_ITEM_RE = re.compile(r"^[ \t]*[-*]\s+\[[ xX]\]\s+(.+?)\s*$", re.MULTILINE)

_ISSUE_REF_RE = re.compile(r"#(\d+)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# Keyword -> weight. Deliberately small and hand-picked rather than exhaustive:
# these are the signals that show up repeatedly in this repo's own real issues
# when a change is more than a local, single-concern edit.
_KEYWORD_WEIGHTS: dict[str, int] = {
    "breaking change": 3,
    "migration": 3,
    "architecture": 2,
    "authentication": 2,
    "concurrency": 2,
    "security": 2,
    "durable": 1,
    "integration": 1,
    "schema": 1,
    "temporal": 1,
}


class ScoreBreakdown(BaseModel):
    """Heuristic Fibonacci complexity score for one GitHub issue.

    Invariants:
        - ``conceptual``, ``anticipated_loc``, ``solution_complexity``, and
          ``aggregate`` are each a member of :data:`FIBONACCI`.
        - ``aggregate == nearest_fibonacci(max(conceptual, anticipated_loc, solution_complexity))``.
    """

    conceptual: int
    conceptual_rationale: str
    anticipated_loc: int
    anticipated_loc_rationale: str
    solution_complexity: int
    solution_complexity_rationale: str
    aggregate: int

    @model_validator(mode="after")
    def _enforce_fibonacci_invariants(self) -> "ScoreBreakdown":
        """Enforce this class's own documented invariants at construction time.

        Preconditions:
            - None (runs on every construction, including ``model_validate``).
        Postconditions:
            - Raises ``ValueError`` when any of ``conceptual``/``anticipated_loc``/
              ``solution_complexity``/``aggregate`` is not a member of
              :data:`FIBONACCI`, or when ``aggregate`` does not equal
              ``nearest_fibonacci(max(conceptual, anticipated_loc, solution_complexity))``.
              Returns ``self`` unchanged otherwise.
        """
        for field_name in ("conceptual", "anticipated_loc", "solution_complexity", "aggregate"):
            value = getattr(self, field_name)
            if value not in FIBONACCI:
                raise ValueError(f"{field_name}={value} is not a member of FIBONACCI")
        expected = nearest_fibonacci(
            max(self.conceptual, self.anticipated_loc, self.solution_complexity)
        )
        if self.aggregate != expected:
            raise ValueError(
                f"aggregate={self.aggregate} does not match expected nearest_fibonacci {expected}"
            )
        return self


def nearest_fibonacci(n: int) -> int:
    """Snap ``n`` to the nearest value in :data:`FIBONACCI`, rounding a tie up.

    Preconditions:
        - None -- ``n`` may be any int, including zero or negative.
    Postconditions:
        - Returns ``FIBONACCI[0]`` for ``n <= FIBONACCI[0]`` and ``FIBONACCI[-1]``
          for ``n >= FIBONACCI[-1]``; otherwise the closest member. An exact tie
          between two members is broken toward the larger one -- a tied estimate
          should read as "more complex", not less.
    """
    if n <= FIBONACCI[0]:
        return FIBONACCI[0]
    if n >= FIBONACCI[-1]:
        return FIBONACCI[-1]
    best = FIBONACCI[0]
    best_dist = abs(n - best)
    for f in FIBONACCI[1:]:
        dist = abs(n - f)
        if dist < best_dist or (dist == best_dist and f > best):
            best = f
            best_dist = dist
    return best


def _score_conceptual(title: str, body: str) -> tuple[int, str]:
    haystack = f"{title}\n{body}".lower()
    hits = sorted(kw for kw in _KEYWORD_WEIGHTS if kw in haystack)
    weight = sum(_KEYWORD_WEIGHTS[kw] for kw in hits)
    score = nearest_fibonacci(max(weight, 1))
    rationale = (
        f"{len(hits)} keyword signal(s): {', '.join(hits)}"
        if hits
        else "no complexity keyword signals found"
    )
    return score, rationale


def _count_file_references(body: str) -> int:
    spans = _INLINE_CODE_RE.findall(body)
    refs = {s for s in spans if len(s) < 200 and ("/" in s or "." in s)}
    return len(refs)


def _score_anticipated_loc(body: str) -> tuple[int, str]:
    words = len(body.split())
    files = _count_file_references(body)
    if words < 50 and files == 0:
        score = 1
    elif words < 150 and files <= 2:
        score = 2
    elif words < 400 and files <= 5:
        score = 3
    elif words < 900:
        score = 5
    elif words < 2000:
        score = 8
    elif words < 4000:
        score = 13
    else:
        score = 21
    rationale = f"~{words} word(s), {files} file reference(s)"
    return score, rationale


def _score_solution_complexity(body: str) -> tuple[int, str]:
    checklist_items = len(CHECKLIST_ITEM_RE.findall(body))
    refs = len(set(_ISSUE_REF_RE.findall(body)))
    signal = checklist_items + refs
    if signal <= 1:
        score = 1
    elif signal <= 3:
        score = 2
    elif signal <= 5:
        score = 3
    elif signal <= 8:
        score = 5
    elif signal <= 13:
        score = 8
    elif signal <= 21:
        score = 13
    else:
        score = 21
    rationale = f"{checklist_items} checklist item(s), {refs} cross-referenced issue(s)"
    return score, rationale


def score_issue(title: str, body: str) -> ScoreBreakdown:
    """Compute a heuristic Fibonacci complexity score from an issue's title/body.

    Preconditions:
        - ``title``/``body`` are strings (either may be empty).
    Postconditions:
        - Purely a function of ``title``/``body`` -- no network call, no LLM,
          deterministic (same input always yields the same ``ScoreBreakdown``).
          Any grooming blocks already present in ``body`` are stripped first
          (:func:`strip_grooming_blocks`) so re-scoring an already-groomed issue
          never double-counts its own previously-injected complexity table or
          sub-issues list as part of the issue's own content.
    """
    clean_body = strip_grooming_blocks(body)
    conceptual, conceptual_rationale = _score_conceptual(title, clean_body)
    loc, loc_rationale = _score_anticipated_loc(clean_body)
    solution, solution_rationale = _score_solution_complexity(clean_body)
    aggregate = nearest_fibonacci(max(conceptual, loc, solution))
    return ScoreBreakdown(
        conceptual=conceptual,
        conceptual_rationale=conceptual_rationale,
        anticipated_loc=loc,
        anticipated_loc_rationale=loc_rationale,
        solution_complexity=solution,
        solution_complexity_rationale=solution_rationale,
        aggregate=aggregate,
    )


def render_complexity_markdown(score: ScoreBreakdown) -> str:
    """Render ``score`` as the ``## Complexity (Fibonacci)`` markdown block.

    Preconditions:
        - None.
    Postconditions:
        - Returns the block's content only (no surrounding markers) -- the
          bare-string-join style ``issue_proposals.build_issue_from_proposal``
          uses, reproducing the table shape already used by real issues in this
          repo (Dimension/Score/Rationale rows plus an aggregate summary line).
    """
    lines = [
        "## Complexity (Fibonacci)",
        "",
        "| Dimension | Score | Rationale |",
        "| --- | ---: | --- |",
        f"| Conceptual | {score.conceptual} | {score.conceptual_rationale} |",
        f"| Anticipated LOC | {score.anticipated_loc} | {score.anticipated_loc_rationale} |",
        f"| Solution complexity | {score.solution_complexity} | {score.solution_complexity_rationale} |",
        f"| **Aggregate** | **{score.aggregate}** | nearest Fibonacci of max(dims) |",
        "",
        "## Conceptual complexity",
        (
            f"Conceptual={score.conceptual}, anticipated LOC={score.anticipated_loc}, "
            f"solution complexity={score.solution_complexity}; "
            f"aggregate Fibonacci score **{score.aggregate}**."
        ),
    ]
    return "\n".join(lines)


def inject_marked_block(body: str, start_marker: str, end_marker: str, block: str) -> str:
    """Replace-or-append a marker-delimited block in ``body``.

    Preconditions:
        - ``start_marker``/``end_marker`` do not themselves appear inside
          ``block``. Enforced by assertion: a marker leaking into ``block``
          would make ``body.find(end_marker)`` match inside the freshly
          injected content instead of (or in addition to) the real closing
          marker, corrupting a later replace-in-place call.
    Postconditions:
        - When both markers are already present (``start_marker`` before
          ``end_marker``), the span between them (inclusive) is replaced with the
          fresh ``start_marker`` + ``block`` + ``end_marker``. Otherwise the block
          is appended, separated from existing content by a blank line when
          ``body`` is non-blank. Idempotent: injecting the same ``block`` twice in
          a row yields the same result as injecting it once.
    """
    assert start_marker not in block, "start_marker must not appear inside block"
    assert end_marker not in block, "end_marker must not appear inside block"
    wrapped = f"{start_marker}\n{block}\n{end_marker}"
    start_idx = body.find(start_marker)
    end_idx = body.find(end_marker)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return body[:start_idx] + wrapped + body[end_idx + len(end_marker) :]
    if not body.strip():
        return wrapped
    return f"{body.rstrip()}\n\n{wrapped}"


def _strip_marked_block(body: str, start_marker: str, end_marker: str) -> str:
    start_idx = body.find(start_marker)
    end_idx = body.find(end_marker)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        return body
    before = body[:start_idx].rstrip("\n")
    after = body[end_idx + len(end_marker) :].lstrip("\n")
    if before and after:
        return f"{before}\n\n{after}"
    return before or after


def strip_grooming_blocks(body: str) -> str:
    """Strip both the Phase A complexity block and the Phase B sub-issues block from ``body``.

    Preconditions:
        - None.
    Postconditions:
        - Returns ``body`` with any marker-delimited grooming blocks removed
          (symmetric with :func:`inject_marked_block`); returns ``body`` unchanged
          when neither block is present.
    """
    body = _strip_marked_block(body, PHASE_A_START, PHASE_A_END)
    body = _strip_marked_block(body, PHASE_B_START, PHASE_B_END)
    return body


def inject_complexity_block(body: str, score: ScoreBreakdown) -> str:
    """Inject/replace the Phase A complexity table in ``body``.

    Preconditions:
        - None.
    Postconditions:
        - Equivalent to ``inject_marked_block(body, PHASE_A_START, PHASE_A_END,
          render_complexity_markdown(score))``.
    """
    return inject_marked_block(body, PHASE_A_START, PHASE_A_END, render_complexity_markdown(score))


def complexity_label(score: ScoreBreakdown) -> str:
    """Return the ``complexity: N`` label for ``score``'s aggregate."""
    return f"complexity: {score.aggregate}"


_COMPLEXITY_LABEL_RE = re.compile(r"^complexity: \d+$")


def merge_complexity_label(existing_labels: tuple[str, ...], score: ScoreBreakdown) -> list[str]:
    """Return the full label set for ``GitHubClient.update_issue(labels=...)``.

    Preconditions:
        - None.
    Postconditions:
        - Returns ``existing_labels`` (order preserved) with any prior label
          matching ``^complexity: \\d+$`` removed and ``complexity_label(score)``
          appended once. GitHub's issue PATCH replaces the full label set rather
          than merging, so this is the complete list a caller must send, not a
          delta.
    """
    kept = [label for label in existing_labels if not _COMPLEXITY_LABEL_RE.match(label)]
    kept.append(complexity_label(score))
    return kept
