/**
 * Job-activity staleness helpers shared by job status views.
 *
 * Stall detection reads ONLY `last_activity_at`, which the job service stamps
 * centrally on every real job update. It must never fall back to `updated_at`:
 * the 120s heartbeat refreshes `updated_at` even when the orchestrator thread
 * is hung, so that fallback would render a dead job as freshly active and
 * structurally suppress the warning — the exact masquerade this exists to
 * surface. Jobs predating `last_activity_at` get no label and no warning
 * (an honest "don't know" beats false reassurance).
 *
 * Ages are computed against the backend's `server_time` (sent with every
 * status response) when available, so the math is immune to browser clock
 * skew in both directions; `Date.now()` is only a fallback for older
 * responses.
 */

/** Minimal status shape these helpers read; both SE and coding-team statuses satisfy it. */
export interface ActivityTimestamps {
  status?: string;
  waiting_for_answers?: boolean;
  last_activity_at?: string | null;
  server_time?: string | null;
}

/**
 * No-activity threshold before an active job is flagged as possibly stalled.
 * 10 minutes: a single LLM call routinely runs 5–10 minutes with no job write,
 * and the LLM client's internal rate-limit backoff sleeps 300s — a shorter
 * threshold false-fires on healthy jobs and trains operators to ignore it.
 */
export const STALL_THRESHOLD_MS = 600_000;

/** Statuses eligible for the stall warning: active states where silence means trouble. */
const STALL_ELIGIBLE_STATUSES = new Set(['running', 'pending']);

/** The reference "now" for age math: the backend clock when available (skew immunity). */
function referenceNowMs(status: ActivityTimestamps | null | undefined, fallbackNow: number): number {
  const serverTime = status?.server_time;
  if (serverTime) {
    const parsed = Date.parse(serverTime);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallbackNow;
}

/**
 * Milliseconds since the job's last real activity, or null when the job has no
 * parseable `last_activity_at` (records predating central stamping) — callers
 * must treat null as "unknown", never as "fresh".
 */
export function lastActivityAgoMs(status: ActivityTimestamps | null | undefined, now: number = Date.now()): number | null {
  const raw = status?.last_activity_at;
  if (!raw) return null;
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return null;
  // A small negative (e.g. activity stamped after the response's server_time
  // was built) reads as "just now" rather than a bogus future age.
  return Math.max(referenceNowMs(status, now) - parsed, 0);
}

/** Human label for the last-activity age: "just now", "42s ago", "3m ago", "2h ago". */
export function lastActivityLabel(status: ActivityTimestamps | null | undefined, now: number = Date.now()): string {
  const ago = lastActivityAgoMs(status, now);
  if (ago === null) return '';
  const seconds = Math.floor(ago / 1000);
  if (seconds < 10) return 'just now';
  return `${formatDuration(ago)} ago`;
}

/**
 * Suffix-free duration ("42s", "3m", "2h") for embedding in sentences like
 * "No agent activity for 12m" — the suffixed label would read "for 12m ago".
 */
export function lastActivityDurationLabel(status: ActivityTimestamps | null | undefined, now: number = Date.now()): string {
  const ago = lastActivityAgoMs(status, now);
  if (ago === null) return '';
  return formatDuration(ago);
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}

/**
 * True when an active (running or pending) job has shown no real activity past
 * STALL_THRESHOLD_MS. Pending counts: a job stuck before its first status write
 * is exactly the silent hang this warning exists for. Never true while waiting
 * for user answers (idle-by-design), on terminal states, or when no activity
 * timestamp exists to judge by.
 */
export function isStalled(status: ActivityTimestamps | null | undefined, now: number = Date.now()): boolean {
  if (!status || !STALL_ELIGIBLE_STATUSES.has(status.status ?? '') || status.waiting_for_answers) return false;
  const ago = lastActivityAgoMs(status, now);
  return ago !== null && ago > STALL_THRESHOLD_MS;
}
