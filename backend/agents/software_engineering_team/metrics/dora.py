"""DORA metrics for the Software Engineering team, derived from ``se_events``.

Four metrics over a configurable time window:

- **Deployment frequency** — merges-to-main per day.
- **Lead time for change** — median(task_merged − task_created) per task.
- **Change-failure rate** — gate re-entries after merge / merged tasks.
- **MTTR** — median(crash_resolved − crash_detected).

Plus cost (total + per job) read from ``se_agent_traces``.

The math lives in the pure :func:`compute_from_events` (a list of event dicts) so
it is unit-testable without a database; :func:`compute_dora` wires it to the
Postgres event/trace stores.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from software_engineering_team.shared import se_events


def _median(values: list[float]) -> Optional[float]:
    """Median of ``values``; ``None`` for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class DoraMetrics:
    """Computed DORA metrics + cost over a window. ``None`` medians mean no samples."""

    window_days: float
    computed_at: str
    deployment_count: int = 0
    deployment_frequency_per_day: float = 0.0
    lead_time_seconds_median: Optional[float] = None
    lead_time_sample_count: int = 0
    merged_count: int = 0
    gate_reentry_count: int = 0
    change_failure_rate: float = 0.0
    mttr_seconds_median: Optional[float] = None
    crash_resolved_count: int = 0
    total_cost_usd: float = 0.0
    cost_by_job: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_ts(event: dict[str, Any]) -> Optional[datetime]:
    ts = event.get("ts")
    return ts if isinstance(ts, datetime) else None


def _created_ts_from_detail(event: dict[str, Any]) -> Optional[datetime]:
    """Return the task creation time carried on a ``task_merged`` event's detail.

    The emitter stamps ``detail.created_ts`` (ISO-8601) on the merge event so lead
    time survives even when the matching ``task_created`` event predates the query
    window. Returns ``None`` when absent or unparseable.
    """
    detail = event.get("detail")
    raw = detail.get("created_ts") if isinstance(detail, dict) else None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def compute_from_events(
    events: list[dict[str, Any]],
    window_days: float,
    *,
    cost: Optional[dict[str, Any]] = None,
) -> DoraMetrics:
    """Compute DORA metrics from a list of ``se_events`` rows.

    Preconditions:
        - ``window_days > 0``.
        - Each event dict carries ``event_type`` and a timezone-aware ``ts``;
          task/crash events carry ``task_id``/``job_id`` where relevant.
    Postconditions:
        - All counts and rates are ``>= 0``; division-by-zero is impossible
          (empty window → zeros, ``None`` medians, ``change_failure_rate`` 0.0).
        - ``change_failure_rate`` is clamped to ``[0.0, 1.0]``.
        - ``merged_count`` and lead-time samples are deduplicated by ``task_id``
          (a task merged twice — e.g. re-queued after repair — counts once).
        - Lead time prefers a ``created_ts`` carried on the ``task_merged`` event
          detail, so a task whose ``task_created`` fell outside the window is not
          dropped; it falls back to the in-window ``task_created`` otherwise.
        - MTTR pairs ``crash_resolved`` with the oldest unmatched
          ``crash_detected`` for the same ``(job_id, task_id)``.
    """
    if window_days <= 0:
        raise ValueError("window_days must be > 0")

    metrics = DoraMetrics(
        window_days=window_days,
        computed_at=datetime.now(tz=timezone.utc).isoformat(),
    )

    ordered = sorted((e for e in events if _event_ts(e)), key=lambda e: e["ts"])

    # Earliest creation time per task → lead time.
    created_by_task: dict[str, datetime] = {}
    # Crash detections pending resolution, keyed by (job_id, task_id) so concurrent
    # backend/frontend crashes in one job are not mis-paired.
    detected: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    merged_task_ids: set[str] = set()
    lead_times: list[float] = []
    mttrs: list[float] = []

    for event in ordered:
        etype = event.get("event_type")
        task_id = event.get("task_id") or ""
        job_id = event.get("job_id") or ""
        ts = event["ts"]

        if etype == se_events.TASK_CREATED:
            created_by_task.setdefault(task_id, ts)
        elif etype == se_events.MERGE_TO_MAIN:
            metrics.deployment_count += 1
        elif etype == se_events.GATE_REENTRY:
            metrics.gate_reentry_count += 1
        elif etype == se_events.CRASH_DETECTED:
            detected[(job_id, task_id)].append(ts)
        elif etype == se_events.CRASH_RESOLVED:
            pending = detected.get((job_id, task_id))
            if pending:
                start = pending.pop(0)
                mttrs.append((ts - start).total_seconds())
                metrics.crash_resolved_count += 1

        if etype == se_events.TASK_MERGED:
            # Dedup by task_id (empty task_id is never deduped — count each).
            if task_id and task_id in merged_task_ids:
                continue
            if task_id:
                merged_task_ids.add(task_id)
            metrics.merged_count += 1
            created = _created_ts_from_detail(event) or created_by_task.get(task_id)
            if created is not None:
                lead = (ts - created).total_seconds()
                if lead >= 0:
                    lead_times.append(lead)

    metrics.deployment_frequency_per_day = round(metrics.deployment_count / window_days, 4)
    metrics.lead_time_seconds_median = _median(lead_times)
    metrics.lead_time_sample_count = len(lead_times)
    metrics.mttr_seconds_median = _median(mttrs)
    if metrics.merged_count > 0:
        # Clamp to [0,1]: re-entries can in principle exceed merges at a window edge.
        metrics.change_failure_rate = round(
            min(1.0, metrics.gate_reentry_count / metrics.merged_count), 4
        )

    if cost:
        metrics.total_cost_usd = round(float(cost.get("total_cost_usd", 0.0) or 0.0), 6)
        metrics.cost_by_job = {
            str(k): round(float(v or 0.0), 6) for k, v in (cost.get("by_job") or {}).items()
        }

    return metrics


def compute_dora(window_days: float) -> DoraMetrics:
    """Compute DORA metrics over the last ``window_days`` from Postgres.

    Preconditions: ``window_days > 0``.
    Postconditions: returns a :class:`DoraMetrics`; an all-zero result (no
        ``None`` medians become numbers) when Postgres is disabled or empty.
    """
    if window_days <= 0:
        raise ValueError("window_days must be > 0")
    from datetime import timedelta

    from software_engineering_team.shared import trace_store

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    events = se_events.fetch_events_since(cutoff)
    cost = trace_store.fetch_cost_since(cutoff)
    return compute_from_events(events, window_days, cost=cost)


__all__ = ["DoraMetrics", "compute_from_events", "compute_dora"]
