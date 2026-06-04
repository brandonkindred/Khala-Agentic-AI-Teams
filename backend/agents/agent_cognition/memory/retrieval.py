"""Retrieval / digest builder for the Agent Cognition Core.

Assembles the compact ``memory_digest`` block the invoke boundary folds into an
agent's prompt — the "what this agent remembers" side channel. This is the read
counterpart to :mod:`agent_cognition.memory.rollup`: the rollup engine *writes*
calendar-scoped summaries, this module *reads* them back into one bounded block.

Two design constraints shape the digest, both from the cognition spec:

* **Closed-period rollups only.** A rollup summary exists only once its calendar
  period has *closed*, so mid-week/mid-month there is no current-period summary.
  The digest therefore stitches the most recent **closed, non-stale** month /
  week / day summaries (stable long-range context) together with the
  **in-progress** period rendered directly from the top-N most salient raw events
  that no such summary covers yet (events at/after the latest non-stale closed
  day). A summary the memory subsystem has flagged ``stale`` (a late writeback
  landed in it and the rollup hasn't reconciled it yet) is skipped in favour of
  the latest summary still considered current — so the digest never shows
  invalidated context. The late events that triggered the staleness are surfaced
  in the live section instead (directly, for a late event in an *older* stale day
  whose unfolded tail the in-progress window wouldn't reach), so fresh memory is
  never dropped while the rollup catches up. It never goes empty merely because
  the current period hasn't closed.
* **Caller-bounded size.** The whole block is trimmed to a caller-supplied
  ``token_budget`` (converted to characters, then compacted and hard-capped), so
  the injector can size it against the model context window.

Design by Contract: :func:`build_memory_digest` documents its Preconditions and
Postconditions. The module is stateless — all durable state lives in
:mod:`agent_cognition.memory.store`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from agent_cognition.memory import store
from agent_cognition.models import MemoryEvent, PeriodSummary, Scale
from llm_service import compact_text, get_client

logger = logging.getLogger(__name__)

# Token→char conversion. The repo uses a conservative ~4-chars-per-token
# heuristic (see ``llm_service`` clients), reused here so callers express the
# digest budget in tokens while ``compact_text`` works in characters.
_CHARS_PER_TOKEN = 4

# Default number of recent raw events used to represent the in-progress period.
# Read at call time so operators/tests can override via the env var below.
_DEFAULT_EVENT_TOP_N = 20

# Page size for scanning summaries (newest first) when skipping ``stale`` rows to
# find the latest still-current one. The scan pages through as far as needed — a
# long run of stale summaries can never hide an older valid one — while never
# materializing the agent's entire summary history in a single read.
_SUMMARY_PAGE_SIZE = 32

# Closed-period summary scales folded into the digest, broadest first so the
# rendered block reads long-range → short-range before the live events. Year is
# intentionally omitted from v1 (day/week/month give enough long-range context).
_SUMMARY_SCALES: tuple[Scale, ...] = (Scale.MONTH, Scale.WEEK, Scale.DAY)


def build_memory_digest(agent_id: str, token_budget: int) -> str:
    """Build the compact memory digest injected on invoke.

    Stitches the most recent **closed** month/week/day rollup summaries together
    with the **in-progress** period — rendered from the top-N most salient recent
    raw events — and trims the result to ``token_budget``.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``token_budget >= 0``.
    Postconditions:
        * ``len(result) <= token_budget * _CHARS_PER_TOKEN`` (hard-capped, so an
          over-budget or best-effort LLM compaction can never breach the budget).
        * Sections are ordered broadest→narrowest: month, week, day summaries,
          then the recent in-progress events in ``(salience DESC, occurred_at
          DESC)`` order.
        * Only **non-stale** summaries are surfaced; a ``stale`` latest summary is
          skipped in favour of the latest non-stale one at that scale (or none).
        * Returns ``""`` when the agent has no non-stale summaries and no recent
          events, or when ``token_budget == 0``.
    """
    assert agent_id, "agent_id must be non-empty"
    assert token_budget >= 0, "token_budget must be non-negative"

    if token_budget == 0:
        return ""

    char_budget = token_budget * _CHARS_PER_TOKEN

    # Most recent *non-stale* closed summary per scale (stable long-range context).
    # A scale with no current summary yet simply contributes nothing.
    summaries: list[tuple[Scale, PeriodSummary]] = []
    for scale in _SUMMARY_SCALES:
        summary = _latest_non_stale_summary(agent_id, scale)
        if summary is not None:
            summaries.append((scale, summary))

    # In-progress period: the freshest, most salient raw events *not yet covered*
    # by a closed summary (see _fetch_in_progress_events).
    events = _fetch_in_progress_events(agent_id, summaries)

    if not summaries and not events:
        return ""

    digest = _render_digest(summaries, events)

    if len(digest) > char_budget:
        digest = compact_text(digest, char_budget, get_client("cognition"), "memory digest")
        # compact_text is best-effort — an LLM may overshoot the target, or fall
        # back to the original text on failure — so hard-truncate to guarantee
        # the budget postcondition regardless of the model's behaviour.
        if len(digest) > char_budget:
            digest = digest[:char_budget]

    assert len(digest) <= char_budget
    return digest


def _latest_non_stale_summary(agent_id: str, scale: Scale) -> PeriodSummary | None:
    """Most recent **non-stale** closed summary at ``scale``, or ``None``.

    ``get_last_summary`` returns the newest period regardless of state, but a
    summary flagged ``stale`` has been invalidated by a late writeback the rollup
    engine hasn't reconciled yet. Surfacing it would inject obsolete long-term
    memory and — for the day scale, whose ``period_end`` bounds the live event
    window — hide the late events behind a boundary that no longer reflects them.
    So skip stale rows (newest first) and return the latest summary the memory
    subsystem still considers current, paging past an arbitrarily long run of
    stale rows so a transient stale cascade never hides an older valid summary.

    Postconditions:
        * Returns the non-stale summary with the maximal ``period_start`` at
          ``scale`` (regardless of how many stale summaries precede it), or
          ``None`` if the agent has no non-stale summary at that scale.
    """
    offset = 0
    while True:
        page = store.fetch_summaries(agent_id, scale, limit=_SUMMARY_PAGE_SIZE, offset=offset)
        for summary in page:
            if not summary.stale:
                return summary
        if len(page) < _SUMMARY_PAGE_SIZE:
            return None
        offset += len(page)


def _fetch_in_progress_events(
    agent_id: str, summaries: list[tuple[Scale, PeriodSummary]]
) -> list[MemoryEvent]:
    """Top-N salient events the shown summaries don't already cover.

    Two uncovered sources, each fetched bounded+ordered+limited in the store, then
    merged and re-ranked:

    1. **In-progress window.** The shown non-stale summaries cover everything up to
       the latest ``period_end`` among them, so the live section need only carry
       events at or after that boundary — fetched as the top-N salient rows with
       ``occurred_at >= boundary`` (the order and limit run in SQL, never
       materializing the whole tail). The boundary is the most recent point the
       shown summaries cover (the max ``period_end``); with current contiguous
       rollups that is the day's ``period_end``, but a week/month-only digest is
       still bounded to the week's/month's end rather than scanning all history.
       Only when **no** summary is shown (cold start) is the bound dropped. Without
       it, an unbounded scan could let a high-salience already-summarized event
       evict current activity.
    2. **Late events behind stale days.** A late writeback into an *older* closed
       day marks that day (and its parents) ``stale``; ``_latest_non_stale_summary``
       then skips it and the in-progress window starts *after* it, so the late
       event would be covered by neither a shown summary nor the window. The
       store's :func:`store.fetch_recent_unfolded_events` returns the top-N salient
       unfolded rows across all stale days in one query, so fresh memory isn't
       dropped while the rollup catches up.

    Postconditions:
        * At most ``_event_top_n()`` events, deduplicated by id and ordered
          ``(salience DESC, occurred_at DESC, id ASC)``.
        * Every event is uncovered by a shown summary: it is either in the
          in-progress window or an unfolded late event from a stale day.
    """
    top_n = _event_top_n()
    # Most recent point the shown (non-stale) summaries cover; None when nothing is
    # shown (cold start) → no lower bound.
    boundary = max((summary.period_end for _scale, summary in summaries), default=None)

    window = store.fetch_recent_events(agent_id, top_n, by_salience=True, since=boundary)
    late = store.fetch_recent_unfolded_events(agent_id, Scale.DAY, top_n, snapshot=_utcnow())

    merged = _dedupe_by_id(window, late)
    # Deterministic (salience DESC, occurred_at DESC, id ASC): a stable id-ASC
    # pre-sort makes equal-(salience, occurred_at) ties resolve by id ascending
    # under the reverse primary sort.
    merged.sort(key=lambda e: e.id)
    merged.sort(key=lambda e: (e.salience, e.occurred_at), reverse=True)
    return merged[:top_n]


def _dedupe_by_id(*groups: list[MemoryEvent]) -> list[MemoryEvent]:
    """Concatenate event groups, keeping the first occurrence of each id."""
    seen: set[str] = set()
    out: list[MemoryEvent] = []
    for group in groups:
        for event in group:
            if event.id not in seen:
                seen.add(event.id)
                out.append(event)
    return out


# ---------------------------------------------------------------------------
# Rendering helpers (pure)
# ---------------------------------------------------------------------------
def _render_digest(
    summaries: list[tuple[Scale, PeriodSummary]],
    events: list[MemoryEvent],
) -> str:
    """Assemble the labeled multi-section digest block.

    Postconditions: sections only appear when populated; long-term summaries
    precede recent activity; sections are blank-line separated.
    """
    sections: list[str] = []
    if summaries:
        lines = [_render_summary_line(scale, summary) for scale, summary in summaries]
        sections.append("## Long-term memory\n" + "\n".join(lines))
    if events:
        sections.append("## Recent activity\n" + _render_events(events))
    return "\n\n".join(sections)


def _render_summary_line(scale: Scale, summary: PeriodSummary) -> str:
    """One labeled line per summary: ``[scale] text | highlights: a; b``."""
    suffix = (
        f" | highlights: {'; '.join(str(h) for h in summary.highlights)}"
        if summary.highlights
        else ""
    )
    return f"[{scale.value}] {summary.summary}{suffix}"


def _render_events(events: list[MemoryEvent]) -> str:
    """One line per event: timestamp, kind, salience, content.

    Mirrors the rollup engine's event rendering so the in-progress events read
    consistently with the day summaries that will eventually fold them in.
    """
    return "\n".join(
        f"[{e.occurred_at.isoformat()}] {e.kind.value} (salience={e.salience:.2f}): {e.content}"
        for e in events
    )


# ---------------------------------------------------------------------------
# Env-backed tunables
# ---------------------------------------------------------------------------
def _utcnow() -> datetime:
    """Current UTC time (indirection so tests can pin the unfolded-event snapshot)."""
    return datetime.now(timezone.utc)


def _event_top_n() -> int:
    """In-progress event count (env ``AGENT_COGNITION_DIGEST_EVENT_TOP_N``)."""
    return _read_positive_int("AGENT_COGNITION_DIGEST_EVENT_TOP_N", _DEFAULT_EVENT_TOP_N)


def _read_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default``.

    Postconditions: returns the parsed value when ``>= 1``; unset/garbage/
    non-positive values fall back to ``default``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default
