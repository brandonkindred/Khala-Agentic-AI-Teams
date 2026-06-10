/**
 * Job-activity staleness helpers shared by job status views.
 *
 * The job's heartbeat thread keeps `last_heartbeat_at`/`updated_at` fresh even
 * when the orchestrator thread is hung, so stall detection must read
 * `last_activity_at` (written only on real orchestrator updates) and fall back
 * to `updated_at` for older jobs that predate the field.
 */

/** Minimal status shape these helpers read; both SE and coding-team statuses satisfy it. */
export interface ActivityTimestamps {
  status?: string;
  waiting_for_answers?: boolean;
  last_activity_at?: string;
  updated_at?: string;
}

/**
 * No-activity threshold before a running job is flagged as possibly stalled.
 * 3 minutes tolerates the 15s UI poll, slow LLM streaming gaps, and clock skew.
 */
export const STALL_THRESHOLD_MS = 180_000;

/**
 * Milliseconds since the job's last real activity, or null when no parseable
 * timestamp is available (e.g. jobs predating last_activity_at/updated_at).
 */
export function lastActivityAgoMs(status: ActivityTimestamps | null | undefined, now: number = Date.now()): number | null {
  const raw = status?.last_activity_at ?? status?.updated_at;
  if (!raw) return null;
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return null;
  // A small negative (clock skew) reads as "just now" rather than a bogus future age.
  return Math.max(now - parsed, 0);
}

/** Human label for the last-activity age: "just now", "42s ago", "3m ago", "2h ago". */
export function lastActivityLabel(status: ActivityTimestamps | null | undefined, now: number = Date.now()): string {
  const ago = lastActivityAgoMs(status, now);
  if (ago === null) return '';
  const seconds = Math.floor(ago / 1000);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

/**
 * True when a running job has shown no real activity past STALL_THRESHOLD_MS.
 * Never true while waiting for user answers (idle-by-design), on terminal
 * states, or when no activity timestamp exists to judge by.
 */
export function isStalled(status: ActivityTimestamps | null | undefined, now: number = Date.now()): boolean {
  if (!status || status.status !== 'running' || status.waiting_for_answers) return false;
  const ago = lastActivityAgoMs(status, now);
  return ago !== null && ago > STALL_THRESHOLD_MS;
}
