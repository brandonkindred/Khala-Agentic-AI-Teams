import type { CodeReviewSummary } from '../../models/coding-team.model';

/**
 * One code-review run on a pull request. Held in memory and kept live by a
 * per-job poller; the authoritative copy is persisted backend-side (the
 * `code_review_runs` table) and re-hydrated on load so history survives reloads.
 */
export interface PrReviewRecord {
  jobId: string;
  prNumber: number;
  /** Repository the review ran against — PR numbers collide across repositories. */
  owner: string;
  repo: string;
  /** Milliseconds since epoch when the review started (for the table timestamp). */
  startedAt: number;
  /** Milliseconds since epoch when the review reached a terminal state, if known.
   * Drives the row's duration; absent while the review is still running. */
  completedAt?: number;
  status: string;
  statusText?: string;
  reviewSummary?: CodeReviewSummary;
  prUrl?: string;
  error?: string;
}
