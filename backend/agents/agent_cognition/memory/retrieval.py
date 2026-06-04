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
  invalidated context, and the late events that triggered the staleness fall
  through into the live section rather than being hidden behind an obsolete
  boundary. It never goes empty merely because the current period hasn't closed.
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

# Open upper bound for the in-progress event window — any sane event predates it.
_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)

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
    """Top-N salient events from the in-progress period only.

    The closed, non-stale day/week/month summaries capture everything up to the
    most recent **non-stale** closed day, so the digest's live section must
    represent only what those summaries don't: events occurring at or after that
    day's ``period_end``. Without this bound, an unbounded top-N-by-salience scan
    over the agent's whole retained history could resurface a high-salience event
    from an already-summarized period (duplicating the summary) and evict genuinely
    current-period events from the window.

    Because the bounding day is the latest *non-stale* one (``summaries`` already
    excludes stale rows), events folded into a now-stale day summary correctly fall
    *after* the boundary and surface here as recent activity rather than vanishing
    until the rollup reconciles.

    When no non-stale closed day summary exists yet (cold start, or every recent
    day still stale), every retained event is uncovered, so the unbounded
    recent-events fetch is used.

    Postconditions:
        * At most ``_event_top_n()`` events, ordered ``(salience DESC, occurred_at
          DESC, id ASC)`` — identical to :func:`store.fetch_recent_events`.
        * When a non-stale closed day summary exists, every returned event
          satisfies ``occurred_at >= day.period_end``.
    """
    top_n = _event_top_n()
    day = next((summary for scale, summary in summaries if scale is Scale.DAY), None)
    if day is None:
        return store.fetch_recent_events(agent_id, top_n, by_salience=True)

    # fetch_events_for_period returns the window ordered (occurred_at, id) ASC;
    # a stable sort by (salience, occurred_at) DESC therefore yields the store's
    # (salience DESC, occurred_at DESC, id ASC) order before the top-N slice.
    open_events = store.fetch_events_for_period(agent_id, day.period_end, _FAR_FUTURE)
    open_events.sort(key=lambda e: (e.salience, e.occurred_at), reverse=True)
    return open_events[:top_n]


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
