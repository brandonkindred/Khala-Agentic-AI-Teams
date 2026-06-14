import { CurrentActivityEntry } from './software-engineering.model';

export interface CodingTeamJobStatus {
  job_id: string;
  status: string;
  phase?: string;
  status_text?: string;
  /** Most recent agent reasoning ("thinking") tokens, for live display. */
  thinking?: string;
  error?: string;
  github_context?: {
    owner: string;
    repo: string;
    issue_number?: number;
    issue_url?: string;
    pr_number?: number;
    pr_url?: string;
  };
  github_pr_url?: string;
  /** Set by the PR-review flow with the posted-review stats. */
  review_summary?: CodeReviewSummary;
  task_graph_snapshot?: TaskSnapshot[];
  /** Fine-grained activity of the currently running sub-agent (e.g. code review sub-steps). */
  current_activity?: CurrentActivityEntry;
  /** ISO timestamp of the last real orchestrator update (heartbeats excluded). */
  last_activity_at?: string;
  /** ISO timestamp of the last job update. */
  updated_at?: string;
  /** ISO timestamp of the last heartbeat (liveness of the worker process). */
  last_heartbeat_at?: string;
  /** Overall job progress (0-100) derived from terminal tasks in the graph. */
  progress?: number | null;
  /** Server UTC time when the response was built; staleness math uses this, not the browser clock. */
  server_time?: string | null;
}

/** Summary of a posted PR review (from the /review-pr flow). */
export interface CodeReviewSummary {
  total_issues: number;
  inline_comments: number;
  /** Findings posted as their own standalone PR conversation comments. */
  comment_findings: number;
  /** Legacy name for `comment_findings`, kept so review rows persisted before the
   * rename still render a count. Prefer `comment_findings`. */
  body_findings?: number;
  /** Findings that could not be posted as their own comment (review still submitted). */
  comments_failed?: number;
  event: string;
}

export interface TaskSnapshot {
  id: string;
  title: string;
  status: string;
  assigned_agent_id?: string;
}
