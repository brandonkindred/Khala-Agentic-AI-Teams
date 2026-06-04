"""Calendar rollup engine for the Agent Cognition Core.

Turns an agent's append-only episodic memory into compact, hierarchical
summaries — one per closed UTC calendar period at four scales (day, week,
month, year). :func:`ensure_rollups_current` is the single idempotent entry
point the central scheduler and the lazy-on-invoke path both call; it
(re)summarizes every period that is missing or flagged ``stale``, oldest
first, bottom-up, so a parent scale always reads freshly-rebuilt children.

Two correctness pillars, both mandated by the cognition design:

* **Calendar-correct input scoping.** A day summarizes its raw events; a week
  and a *month* each summarize that period's **day** summaries; a year
  summarizes its **month** summaries. Months are deliberately built from days
  rather than weeks: an ISO week straddles month boundaries (e.g. Jan 29 –
  Feb 4), so consuming whole weeks would bleed events across months.
* **Lossless late-arrival handling.** When a late event lands in an
  already-summarized period the upstream writeback marks the containing
  summaries ``stale`` (bumping ``version``); the next rollup resolves them.
  If the period's raw events are still retained the summary is **recomputed**
  from events; once a day's events have been pruned the late event is folded
  in by an **incremental amend** of the existing summary (never a rebuild
  from the lone late row, which would destroy real history).

When a recompute/amend supersedes the ``version`` a learned rule or proposal
cited as evidence, the dependent rows are flagged for operator review — the
hand-off into the rules/reflection layer.

Design by Contract: every function documents its Preconditions,
Postconditions, and (where relevant) Invariants. The module is stateless —
all durable state lives in :mod:`agent_cognition.memory.store`.
"""

from __future__ import annotations

import logging
import os
import uuid
from calendar import monthrange
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from agent_cognition.memory import store
from agent_cognition.models import MemoryEvent, PeriodSummary, Scale
from llm_service import compact_text, complete_validated, get_client

logger = logging.getLogger(__name__)

# Tunables (read at call time so tests/operators can override per environment).
_DEFAULT_INPUT_CHARS = 12000
_DEFAULT_MAX_LOOKBACK_DAYS = 400

# Aggregate scales consume their *child* scale's summaries. Months consume days
# (NOT weeks — ISO weeks straddle month boundaries); years consume months.
_CHILD_SCALE: dict[Scale, Scale] = {
    Scale.WEEK: Scale.DAY,
    Scale.MONTH: Scale.DAY,
    Scale.YEAR: Scale.MONTH,
}

# Processing order: children before parents so a parent reads fresh children.
_SCALE_ORDER: tuple[Scale, ...] = (Scale.DAY, Scale.WEEK, Scale.MONTH, Scale.YEAR)

_TASK_INSTRUCTION = (
    "--- TASK ---\n"
    "Return a single JSON object with two keys: `summary` (a concise factual "
    "digest string) and `highlights` (a list of short strings naming the most "
    "salient outcomes, errors, decisions, or feedback). No other keys."
)

_DAY_SYSTEM_PROMPT = (
    "You summarize one agent's raw episodic memory events for a single UTC "
    "calendar day into a compact, factual digest. Preserve concrete "
    "identifiers, decisions, outcomes, and failures; do not speculate or "
    "invent. Return only the requested JSON object."
)

_AGG_SYSTEM_PROMPT = (
    "You roll up several lower-level period summaries (days into a week or "
    "month, or months into a year) into one higher-level summary. Synthesize "
    "recurring themes and notable changes across the children rather than "
    "concatenating them. Return only the requested JSON object."
)

_AMEND_SYSTEM_PROMPT = (
    "You are revising an EXISTING period summary because late events arrived "
    "after the period's raw events were already pruned. The base summary is "
    "authoritative history you MUST preserve; integrate the new late events as "
    "additions or corrections and never discard captured content. Return only "
    "the requested JSON object."
)


class _RollupResult(BaseModel):
    """Narrow LLM output schema for one rollup.

    Carries only the model-authored fields; the engine assembles the full
    :class:`PeriodSummary` (id, version, period bounds, counts, timestamps)
    around it. Kept separate from ``PeriodSummary`` so the model can never
    author store-managed columns.
    """

    summary: str = ""
    highlights: list[str] = Field(default_factory=list)


class RollupReport(BaseModel):
    """Telemetry returned by :func:`ensure_rollups_current` (not persisted).

    ``recomputed`` / ``amended`` map a ``Scale.value`` to the number of
    periods rebuilt-from-inputs / incrementally-amended in this pass;
    ``deferred`` maps a ``Scale.value`` to the number of aggregate periods
    left for a later pass because a child summary they consume was still
    stale (recomputing then would fold pre-amend child content and clear the
    parent's stale flag, permanently losing the late event upward);
    ``skipped_open`` counts still-open (not-yet-closed) periods deliberately
    left alone; ``evidence_flagged`` counts proposals + rules flagged for
    review because a cited summary's version was superseded.
    """

    agent_id: str
    recomputed: dict[str, int] = Field(default_factory=dict)
    amended: dict[str, int] = Field(default_factory=dict)
    deferred: dict[str, int] = Field(default_factory=dict)
    skipped_open: int = 0
    evidence_flagged: int = 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def ensure_rollups_current(agent_id: str, now: datetime) -> RollupReport:
    """Bring every closed calendar period current for ``agent_id``.

    Processes each scale in ``day -> week -> month -> year`` order and, within
    a scale, every missing-or-stale closed period oldest-first, so a parent is
    always recomputed after its (possibly rebuilt) children.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``now`` is timezone-aware (UTC).
    Postconditions:
        * For each scale, every closed period (``period_end <= now``) inside
          the lookback window that has no summary, or a ``stale`` summary, is
          summarized exactly once via an idempotent ``upsert_summary`` — no
          duplicate rows (unique key) and no spurious version bumps on a
          clean re-run. Periods whose ``period_end > now`` are left untouched.
        * Returns a :class:`RollupReport` counting the work done.
    Invariants:
        * Each period captures its own ``snapshot`` (read-time) immediately
          before reading its inputs, and passes that same value as
          ``computed_at`` to its upsert, so a period's read set and prune guard
          stay consistent. The snapshot is deliberately *not* shared across
          periods: a single up-front snapshot widens (to the whole multi-scale
          run) the window in which a concurrent append to a not-yet-summarized
          period is read-excluded yet has no summary row to flag stale — see
          :func:`_rollup_one_period`'s first-summary re-probe.
    """
    assert agent_id, "ensure_rollups_current: agent_id must be non-empty"
    assert now.tzinfo is not None, "ensure_rollups_current: now must be tz-aware (UTC)"

    llm = get_client("cognition")
    report = RollupReport(agent_id=agent_id)

    for scale in _SCALE_ORDER:
        _, current_end = _period_bounds(scale, now)
        if current_end > now:
            report.skipped_open += 1
        for period_start, period_end in _periods_to_process(agent_id, scale, now):
            _rollup_one_period(
                agent_id,
                scale,
                period_start,
                period_end,
                llm=llm,
                report=report,
            )
    return report


# ---------------------------------------------------------------------------
# Period boundary math (pure — no I/O)
# ---------------------------------------------------------------------------
def _midnight(dt: datetime) -> datetime:
    """Return ``dt`` truncated to 00:00:00.000000 (same tz).

    Preconditions: ``dt`` is timezone-aware.
    Postconditions: hour/minute/second/microsecond are zero; date and tz keep.
    """
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Half-open ``[start, start+1day)`` UTC calendar day containing ``dt``."""
    start = _midnight(dt)
    return start, start + timedelta(days=1)


def _week_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Half-open ISO week (Monday 00:00 .. +7 days) containing ``dt``."""
    start = _midnight(dt) - timedelta(days=dt.weekday())
    return start, start + timedelta(days=7)


def _month_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Half-open calendar month (1st 00:00 .. 1st of next month) for ``dt``."""
    start = _midnight(dt).replace(day=1)
    days = monthrange(start.year, start.month)[1]
    return start, start + timedelta(days=days)


def _year_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Half-open calendar year (Jan 1 00:00 .. next Jan 1) for ``dt``."""
    start = _midnight(dt).replace(month=1, day=1)
    return start, start.replace(year=start.year + 1)


_BOUNDS = {
    Scale.DAY: _day_bounds,
    Scale.WEEK: _week_bounds,
    Scale.MONTH: _month_bounds,
    Scale.YEAR: _year_bounds,
}


def _period_bounds(scale: Scale, dt: datetime) -> tuple[datetime, datetime]:
    """Dispatch to the boundary helper for ``scale``.

    Preconditions: ``dt`` is tz-aware UTC; ``scale`` is a known ``Scale``.
    Postconditions: returns the half-open ``[start, end)`` calendar period at
    ``scale`` containing ``dt`` (``start <= dt < end``), midnight-aligned.
    """
    return _BOUNDS[scale](dt)


def _is_closed(period_end: datetime, now: datetime) -> bool:
    """A period is closed once its (exclusive) end has been reached.

    Postconditions: ``True`` iff ``period_end <= now``.
    """
    return period_end <= now


# ---------------------------------------------------------------------------
# Period enumeration
# ---------------------------------------------------------------------------
def _periods_to_process(
    agent_id: str, scale: Scale, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Ascending closed periods at ``scale`` that are missing or stale.

    Preconditions:
        * ``agent_id`` is non-empty; ``now`` is tz-aware UTC.
    Postconditions:
        * Returns ``(period_start, period_end)`` pairs, ascending by start,
          for every closed period that either has no summary (within the
          lookback window) or has a ``stale`` summary (any age). Open periods
          (``period_end > now``) are excluded. A leading **aggregate** period
          that the lookback floor bisects (``period_start < floor``) is excluded
          from the missing set, so a partial week/month/year at the horizon is
          never summarized from only its post-floor children; an already-stale
          such period is still returned (staleness ignores the floor).
    Invariants:
        * Ascending order plus the bottom-up scale loop guarantees a child is
          rebuilt before any parent that consumes it.
    """
    assert agent_id, "_periods_to_process: agent_id must be non-empty"

    candidates: dict[datetime, tuple[datetime, datetime]] = {}

    # Stale summaries are always re-resolved, regardless of the lookback floor.
    for s in store.fetch_stale_summaries(agent_id, scale):
        if _is_closed(s.period_end, now):
            candidates[s.period_start] = (s.period_start, s.period_end)

    # Missing (never-summarized) closed periods inside the bounded lookback.
    existing_starts = {s.period_start for s in store.fetch_summaries(agent_id, scale)}
    floor = now - timedelta(days=_max_lookback_days())
    start, end = _period_bounds(scale, floor)
    # Skip a leading aggregate period that the floor bisects (``start < floor``):
    # its pre-floor child periods sit outside this missing walk and may never
    # have been summarized, so building the parent now would consume only its
    # post-floor children and silently omit the older calendar-period events
    # while marking the parent non-stale (hence never rebuilt). Days have no
    # children and always read their full event set, so a floor-bisected day is
    # complete and is kept.
    if scale in _CHILD_SCALE and start < floor:
        start, end = _period_bounds(scale, end)
    while _is_closed(end, now):
        if start not in existing_starts:
            candidates[start] = (start, end)
        start, end = _period_bounds(scale, end)

    return [candidates[k] for k in sorted(candidates)]


# ---------------------------------------------------------------------------
# Per-period processing
# ---------------------------------------------------------------------------
def _rollup_one_period(
    agent_id: str,
    scale: Scale,
    period_start: datetime,
    period_end: datetime,
    *,
    llm,
    report: RollupReport,
) -> None:
    """(Re)summarize one period, choosing recompute vs. amend by regime.

    A per-period read ``snapshot`` is captured here, immediately before any
    input is read, and reused as the upsert's ``computed_at`` — keeping this
    period's read set and prune guard consistent while minimizing the window in
    which a concurrent append is read-excluded.

    Preconditions:
        * ``[period_start, period_end)`` is a closed calendar period at
          ``scale``.
    Postconditions:
        * At most one ``upsert_summary`` for ``(agent_id, scale,
          period_start)`` with ``computed_at`` = the per-period snapshot. A
          pruned day is amended (base preserved); any other missing/stale
          period is recomputed from inputs. An empty, never-summarized period
          is a no-op. A recompute/amend of a previously-summarized period flags
          any evidence that cited the now-superseded version.
        * **First-summary re-probe.** When a period gets its *first* summary
          (no prior row) and a day-scale event was recorded after this
          snapshot (so it was read-excluded yet had no row for the writeback to
          flag), the just-created period is self-flagged stale so a later pass
          folds it. Day-scale only: aggregates fold child summaries, and a
          still-stale child already defers the parent.
    """
    snapshot = store._now()
    existing = store.get_existing_summary(agent_id, scale, period_start)

    # Regime (b): the day's raw events were pruned — amend, never rebuild.
    # Only day summaries are ever latched ``events_pruned`` by the pruner, so
    # the amend path is day-scale only; aggregates always recompute (regime a)
    # from their retained child summaries.
    if existing is not None and existing.events_pruned:
        # Fold only events that arrived after this summary's last fold point.
        # Events amended in on a previous pass linger in the table until the
        # next prune, so reading the whole window would re-fold them and
        # double-count; ``fetch_unfolded_events`` bounds by ``computed_at``.
        late = store.fetch_unfolded_events(
            agent_id, scale, period_start, period_end, snapshot=snapshot
        )
        if not late:
            return
        revised = revise_summary(existing, late, llm=llm, snapshot=snapshot)
        store.upsert_summary(agent_id, revised, computed_at=snapshot)
        report.amended[scale.value] = report.amended.get(scale.value, 0) + 1
        _flag_dependent_evidence(agent_id, revised, report)
        return

    # Regime (a): never-summarized or stale-with-events — recompute.
    summary = _build_summary(
        agent_id, scale, period_start, period_end, existing, snapshot, llm, report
    )
    if summary is None:
        return
    store.upsert_summary(agent_id, summary, computed_at=snapshot)
    report.recomputed[scale.value] = report.recomputed.get(scale.value, 0) + 1
    if existing is not None:
        _flag_dependent_evidence(agent_id, summary, report)
    elif scale is Scale.DAY and store.has_events_recorded_after(
        agent_id, period_start, period_end, after=snapshot
    ):
        # First-ever summary for this day, but an event was appended after our
        # read snapshot and before this row existed — so the writeback's
        # mark_period_stale found no row to flag and the read excluded it.
        # Self-flag stale now that the row exists; the next pass folds it (and
        # the stale-child defer carries the signal up to week/month/year).
        store.mark_period_stale(agent_id, period_start)
        logger.debug(
            "rollup re-probe: first %s summary %s saw a post-snapshot append; re-flagged stale",
            scale.value,
            period_start.isoformat(),
        )


def _build_summary(
    agent_id: str,
    scale: Scale,
    period_start: datetime,
    period_end: datetime,
    existing: PeriodSummary | None,
    snapshot: datetime,
    llm,
    report: RollupReport,
) -> PeriodSummary | None:
    """Recompute a period summary from its calendar-correct inputs.

    Preconditions:
        * ``[period_start, period_end)`` is a closed period at ``scale``.
    Postconditions:
        * Returns a fresh ``PeriodSummary`` (``stale=False``) assembled around
          the LLM digest, or ``None`` when (a) the period has no inputs and no
          existing summary (empty-history no-op), or (b) an aggregate period is
          *deferred* because a child summary it consumes is still ``stale``
          (``report.deferred[scale]`` is bumped and the parent is left
          missing/stale for a later pass). ``version`` is left at the existing
          value (monotonicity is owned by ``upsert_summary``); ``id`` and
          ``created_at`` are preserved on recompute.
    """
    if scale is Scale.DAY:
        events = store.fetch_events_for_period(
            agent_id, period_start, period_end, snapshot=snapshot
        )
        if not events and existing is None:
            return None
        text = _render_events_text(events)
        source_count = len(events)
        covers_through = max((e.occurred_at for e in events), default=None)
        body = _summarize(text, _DAY_SYSTEM_PROMPT, f"{scale.value} memory events", llm)
    else:
        children = store.fetch_summaries_in_window(
            agent_id, _CHILD_SCALE[scale], period_start, period_end
        )
        # Defer if any consumed child is still stale. Building now would fold
        # the child's pre-amend content and then clear THIS parent's stale flag;
        # because a child recompute/amend does not re-stale its parents, nothing
        # would re-mark the parent when the child later resolves — so the late
        # event would be lost from week/month/year permanently. Leaving the
        # parent missing/stale makes a later pass rebuild it once all children
        # are current (children are processed before parents within a pass, so a
        # still-stale child here is one this pass could not resolve).
        stale_children = [c for c in children if c.stale]
        if stale_children:
            logger.debug(
                "rollup defer: %s %s has %d still-stale child summaries; leaving parent for a later pass",
                scale.value,
                period_start.isoformat(),
                len(stale_children),
            )
            report.deferred[scale.value] = report.deferred.get(scale.value, 0) + 1
            return None
        if not children and existing is None:
            return None
        text = _render_children_text(children)
        source_count = sum(c.source_count for c in children)
        covers_through = _max_covers_through(children)
        body = _summarize(text, _AGG_SYSTEM_PROMPT, f"{scale.value} child summaries", llm)

    return PeriodSummary(
        id=existing.id if existing else uuid.uuid4().hex,
        agent_id=agent_id,
        scale=scale,
        period_start=period_start,
        period_end=period_end,
        summary=body.summary,
        highlights=list(body.highlights),
        source_count=source_count,
        covers_through=covers_through,
        version=existing.version if existing else 1,
        stale=False,
        created_at=existing.created_at if existing else snapshot,
    )


def revise_summary(
    base: PeriodSummary,
    late_events: list[MemoryEvent],
    *,
    llm,
    snapshot: datetime,
) -> PeriodSummary:
    """Incrementally amend a pruned period's summary with late events.

    Preconditions:
        * ``base.events_pruned`` is ``True`` (its raw events are gone) and
          ``late_events`` is non-empty; ``snapshot`` captured before the read.
    Postconditions:
        * Returns a ``PeriodSummary`` with the same id/scale/period
          bounds/created_at as ``base``, an LLM-extended summary that
          preserves the base's captured history, ``covers_through`` extended to
          the latest late event, ``source_count`` increased by the late count,
          and ``stale=False``. ``version`` is left at ``base.version`` (advance
          is owned by ``mark_period_stale`` / ``upsert_summary``).
    Invariants:
        * Never rebuilds from the late events alone — the base summary text is
          the authoritative starting point.
    """
    assert base.events_pruned, "revise_summary: base summary must be pruned (regime b)"
    assert late_events, "revise_summary: late_events must be non-empty"

    bounded = compact_text(
        _render_events_text(late_events), _input_char_budget(), llm, "late memory events"
    )
    prompt = (
        "--- BASE SUMMARY (preserve) ---\n"
        f"{base.summary}\n"
        f"BASE HIGHLIGHTS: {'; '.join(str(h) for h in base.highlights)}\n\n"
        "--- LATE EVENTS ---\n"
        f"{bounded}\n\n"
        f"{_TASK_INSTRUCTION}"
    )
    body = complete_validated(
        llm,
        prompt,
        schema=_RollupResult,
        system_prompt=_AMEND_SYSTEM_PROMPT,
        temperature=0.0,
        correction_attempts=1,
    )

    late_max = max((e.occurred_at for e in late_events), default=None)
    covers = base.covers_through
    if late_max is not None:
        covers = late_max if covers is None else max(covers, late_max)

    return PeriodSummary(
        id=base.id,
        agent_id=base.agent_id,
        scale=base.scale,
        period_start=base.period_start,
        period_end=base.period_end,
        summary=body.summary,
        highlights=list(body.highlights),
        source_count=base.source_count + len(late_events),
        covers_through=covers,
        version=base.version,
        stale=False,
        created_at=base.created_at,
    )


def _summarize(text: str, system_prompt: str, label: str, llm) -> _RollupResult:
    """Compact the input block then ask the LLM for a structured digest.

    Preconditions: ``text`` is the rendered inputs; ``label`` describes them.
    Postconditions: returns a validated ``_RollupResult``; the input is bounded
    to the configured char budget before the call so large periods don't
    overflow the model context.
    """
    bounded = compact_text(text, _input_char_budget(), llm, content_description=label)
    prompt = f"{bounded}\n\n{_TASK_INSTRUCTION}"
    return complete_validated(
        llm,
        prompt,
        schema=_RollupResult,
        system_prompt=system_prompt,
        temperature=0.0,
        correction_attempts=1,
    )


def _flag_dependent_evidence(agent_id: str, summary: PeriodSummary, report: RollupReport) -> None:
    """Flag rules/proposals whose evidence cites a now-superseded version.

    A derived rule or pending proposal records evidence as ``{"summary_id":
    <id>, "version": <int>}`` refs (the cross-step contract). When this period
    is (re)summarized, any such row referencing this ``summary_id`` at a
    version below the current one is no longer fresh: pending proposals get
    ``stale_evidence`` and active derived rules get ``needs_review`` so the
    operator/ reflection layer revisits them.

    Preconditions: ``agent_id`` non-empty; ``summary`` has been upserted.
    Postconditions: ``report.evidence_flagged`` is increased by the number of
    proposal + rule rows flagged (zero when nothing cites this summary).
    """
    flagged = store.flag_stale_proposals(agent_id, summary.id, summary.version)
    flagged += store.flag_rules_needing_review(agent_id, summary.id, summary.version)
    report.evidence_flagged += flagged


# ---------------------------------------------------------------------------
# Rendering helpers (pure)
# ---------------------------------------------------------------------------
def _render_events_text(events: list[MemoryEvent]) -> str:
    """One line per event: timestamp, kind, salience, content."""
    return "\n".join(
        f"[{e.occurred_at.isoformat()}] {e.kind.value} (salience={e.salience:.2f}): {e.content}"
        for e in events
    )


def _render_children_text(children: list[PeriodSummary]) -> str:
    """One block per child summary: period start, summary, highlights."""
    lines: list[str] = []
    for c in children:
        suffix = f" | highlights: {'; '.join(str(h) for h in c.highlights)}" if c.highlights else ""
        lines.append(f"[{c.period_start.date().isoformat()}] {c.summary}{suffix}")
    return "\n".join(lines)


def _max_covers_through(children: list[PeriodSummary]) -> datetime | None:
    """Latest timestamp folded across children (``covers_through`` or end)."""
    stamps = [(c.covers_through or c.period_end) for c in children]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Env-backed tunables
# ---------------------------------------------------------------------------
def _input_char_budget() -> int:
    """Char budget for ``compact_text`` (env ``AGENT_COGNITION_ROLLUP_INPUT_CHARS``)."""
    return _read_positive_int("AGENT_COGNITION_ROLLUP_INPUT_CHARS", _DEFAULT_INPUT_CHARS)


def _max_lookback_days() -> int:
    """Cold-start backfill bound (env ``AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS``)."""
    return _read_positive_int(
        "AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS", _DEFAULT_MAX_LOOKBACK_DAYS
    )


def _read_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default`` (floor 1).

    Postconditions: returns ``max(1, parsed)``; unset/garbage/non-positive
    values fall back to ``default``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default
