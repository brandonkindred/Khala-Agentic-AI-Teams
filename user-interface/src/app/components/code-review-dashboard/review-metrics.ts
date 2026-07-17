/**
 * Pure derivation helpers for the Code Review page's per-review metrics — the Severity
 * and Duration cells rendered by `PrReviewDetailComponent`, plus the live-poll completion
 * timestamp stamped by `CodeReviewDashboardComponent`.
 *
 * Kept as standalone pure functions so the metric logic is unit-testable in isolation and
 * shared by both the parent dashboard (`terminalTimestamp`) and the detail child
 * (`severityEntries`, `reviewDuration`). Every function is pure except `reviewDuration`
 * (may `console.warn` on a clock-skew anomaly) and `terminalTimestamp` (reads the clock
 * only on its fallback path).
 */
import type { CodeReviewSummary, CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/** Fixed critical→info ordering for the per-row severity metric chips. */
const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'] as const;

/**
 * Minimal review-record shape `reviewDuration` needs. A `PrReviewRecord` structurally
 * satisfies it, so this module has no dependency on the component.
 */
export interface ReviewDurationInput {
  jobId: string;
  status: string;
  startedAt: number;
  completedAt?: number;
}

/**
 * Non-zero severity counts of a review, in fixed critical→info order, for the per-row
 * severity chips.
 *
 * Preconditions: `summary` may be undefined (review still running / no summary).
 * Postconditions: returns one `{ level, count }` entry per severity whose count is
 * greater than zero; returns [] when there is no summary, no `severity_counts`, or
 * every level is zero. Pure — no side effects.
 */
export function severityEntries(
  summary: CodeReviewSummary | undefined,
): { level: string; count: number }[] {
  const counts = summary?.severity_counts;
  if (!counts) return [];
  const entries: { level: string; count: number }[] = [];
  for (const level of SEVERITY_ORDER) {
    const count = counts[level] ?? 0;
    if (count > 0) entries.push({ level, count });
  }
  return entries;
}

/**
 * Human-readable elapsed time of a review run (e.g. "45s", "1m 23s", "2h 5m").
 *
 * Preconditions: `record` carries the run's status and start/completion timestamps.
 * Postconditions: returns a formatted duration when the run is terminal and carries a
 * `completedAt` no earlier than `startedAt`; otherwise null (the template renders "—").
 * A completion before the start (clock skew) is surfaced via `console.warn` rather than
 * silently swallowed. Pure aside from that warning.
 */
export function reviewDuration(record: ReviewDurationInput): string | null {
  if (!isCodingTeamTerminalStatus(record.status) || record.completedAt === undefined) return null;
  const ms = record.completedAt - record.startedAt;
  if (ms < 0) {
    console.warn(`Negative review duration for job ${record.jobId} (${ms}ms); showing no duration.`);
    return null;
  }
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/**
 * Best-effort completion time (ms since epoch) for a review whose live poll just
 * reached a terminal state.
 *
 * Preconditions: `status` is the terminal job-status payload for the review.
 * Postconditions: returns the server's terminal-update timestamp (`updated_at`, else
 * `last_activity_at`) parsed to ms when present and valid; otherwise the browser clock
 * (`Date.now()`). `updated_at` is preferred because it is stamped on the terminal
 * transition itself — including when the stale-job monitor fails a dead worker, which
 * bumps `updated_at` but leaves `last_activity_at` frozen at the last progress event
 * (that would understate a timed-out review). Reads the clock only on the fallback path.
 */
export function terminalTimestamp(
  status: Pick<CodingTeamJobStatus, 'updated_at' | 'last_activity_at'>,
): number {
  const serverTs = status.updated_at ?? status.last_activity_at;
  if (serverTs) {
    const parsed = Date.parse(serverTs);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return Date.now();
}
