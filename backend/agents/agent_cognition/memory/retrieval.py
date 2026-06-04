"""Retrieval / digest builder for the Agent Cognition Core.

Assembles the compact ``memory_digest`` block the invoke boundary folds into an
agent's prompt — the "what this agent remembers" side channel. This is the read
counterpart to :mod:`agent_cognition.memory.rollup`: the rollup engine *writes*
calendar-scoped summaries, this module *reads* them back into one bounded block.

Two design constraints shape the digest, both from the cognition spec:

* **Closed-period rollups only.** A rollup summary exists only once its calendar
  period has *closed*, so mid-week/mid-month there is no current-period summary.
  The digest therefore stitches the most recent **closed** month / week / day
  summaries (stable long-range context) together with the **in-progress** period
  rendered directly from the top-N most salient recent raw events. It never goes
  empty merely because the current period hasn't closed yet.
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
          DESC)`` order as returned by :func:`store.fetch_recent_events`.
        * Returns ``""`` when the agent has no summaries and no recent events, or
          when ``token_budget == 0``.
    """
    assert agent_id, "agent_id must be non-empty"
    assert token_budget >= 0, "token_budget must be non-negative"

    if token_budget == 0:
        return ""

    char_budget = token_budget * _CHARS_PER_TOKEN

    # Most recent closed summary per scale (stable long-range context). A scale
    # with no closed period yet simply contributes nothing.
    summaries: list[tuple[Scale, PeriodSummary]] = []
    for scale in _SUMMARY_SCALES:
        summary = store.get_last_summary(agent_id, scale)
        if summary is not None:
            summaries.append((scale, summary))

    # In-progress period: the freshest, most salient raw events. Ordering is the
    # store's (salience DESC, occurred_at DESC, id ASC).
    events = store.fetch_recent_events(agent_id, _event_top_n(), by_salience=True)

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
