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
 * Clock-skew handling: every status response carries `server_time`, and pollers
 * stamp the browser receipt time via `markStatusReceived`. The pair yields a
 * clock OFFSET (server minus browser at receipt), so ages are computed against
 * `Date.now() + offset` — immune to skew in both directions AND continuously
 * advancing. Anchoring directly on the response's `server_time` snapshot is
 * deliberately avoided: a snapshot freezes the age between polls and freezes it
 * forever once polling stops on an error, silently suppressing the stall
 * warning exactly when the backend dies. Without a receipt stamp the math
 * degrades to the raw browser clock — still advancing; the worst case is a
 * spurious warning under extreme skew, never a warning that cannot fire.
 */

/** Minimal status shape these helpers read; both SE and coding-team statuses satisfy it. */
export interface ActivityTimestamps {
  status?: string;
  waiting_for_answers?: boolean;
  last_activity_at?: string | null;
  server_time?: string | null;
  /** Browser Date.now() when this status was received — client-side only, set via markStatusReceived. */
  client_received_at_ms?: number;
}

/**
 * Stamp the browser receipt time onto a freshly received status. Pollers call
 * this in their response handler; it is what turns `server_time` into a usable
 * clock offset (see the module header).
 */
export function markStatusReceived<T extends ActivityTimestamps>(status: T, now: number = Date.now()): T {
  status.client_received_at_ms = now;
  return status;
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

/**
 * The reference "now" for age math: the browser clock corrected by the
 * server-minus-browser offset captured at receipt (skew immunity), or the raw
 * browser clock when no offset can be derived. Always advances in real time —
 * never a frozen server snapshot (see the module header for why).
 */
function referenceNowMs(status: ActivityTimestamps | null | undefined, fallbackNow: number): number {
  const serverTime = status?.server_time;
  const receivedAt = status?.client_received_at_ms;
  if (serverTime && typeof receivedAt === 'number' && Number.isFinite(receivedAt)) {
    const serverMs = Date.parse(serverTime);
    if (!Number.isNaN(serverMs)) return fallbackNow + (serverMs - receivedAt);
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
