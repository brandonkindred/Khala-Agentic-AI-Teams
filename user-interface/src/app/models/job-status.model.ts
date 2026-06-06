/**
 * Single source of truth for job-status string values that are shared across more than one
 * dashboard/component. Keeping these here (rather than re-declaring literal arrays per component)
 * means a newly-introduced status only has to be classified in one place — the prior bug, where a
 * new partial-success status was treated as terminal in one view but polled forever in another,
 * came from each component carrying its own copy of the list.
 */

/** A run finished but some units of work failed (partial success). Distinct from a clean 'completed'. */
export const COMPLETED_WITH_FAILURES = 'completed_with_failures';

/**
 * Terminal statuses for coding-team-style jobs: once a job reports one of these it will not change
 * again, so polling can stop and the job is dismissable.
 */
export const CODING_TEAM_TERMINAL_STATUSES: readonly string[] = [
  'completed',
  COMPLETED_WITH_FAILURES,
  'failed',
  'cancelled',
];

/** True once a coding-team-style job has reached a terminal (no-longer-changing) state. */
export function isCodingTeamTerminalStatus(status: string | null | undefined): boolean {
  return !!status && CODING_TEAM_TERMINAL_STATUSES.includes(status);
}
