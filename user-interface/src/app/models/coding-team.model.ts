import type { CurrentActivityEntry, PendingQuestion } from './software-engineering.model';

/** GitHub issue/PR metadata attached to a coding-team job started from an issue. */
export interface CodingTeamGitHubContext {
  owner: string;
  repo: string;
  issue_number?: number;
  issue_url?: string;
  pr_number?: number;
  pr_url?: string;
}

export interface CodingTeamJobStatus {
  job_id: string;
  status: string;
  phase?: string;
  status_text?: string;
  /** Most recent agent reasoning ("thinking") tokens, for live display. */
  thinking?: string;
  error?: string;
  github_context?: CodingTeamGitHubContext;
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
  /** Decisions awaiting a user answer before the job can proceed. */
  pending_questions?: PendingQuestion[];
  /** True when the job is paused waiting for the user to answer pending questions. */
  waiting_for_answers?: boolean;
  /**
   * Per-agent status roster (Tech Lead + one Senior SWE per stack), derived server-side: who
   * is working now, each agent's status, and the task each is on. Absent on older records.
   */
  agents?: CodingTeamAgentStatus[];
}

/**
 * Live status of one coding-team agent, for the per-agent monitor cards. Mirrors the backend
 * `AgentStatusEntry`. The roster is the Tech Lead (coordinator) plus one Senior SWE per stack.
 *
 * Properties are intentionally snake_case: like the other API-model interfaces in this codebase
 * (e.g. `CodingTeamJobStatus` above, `JobStatusResponse`), this type is the direct JSON shape of
 * the backend response, so the keys match the wire format and need no mapping layer.
 */
export interface CodingTeamAgentStatus {
  /** Stable agent id (engineer stack name, or 'tech_lead'). */
  agent_id: string;
  /** 'tech_lead' or 'senior_engineer'. */
  role: string;
  /** Human-readable label for the agent card, e.g. "Tech Lead" or "Senior Engineer — frontend". */
  display_name: string;
  /** Stack name for engineers; null/undefined for the Tech Lead. */
  stack?: string | null;
  /** Tools/services the engineer specializes in (empty for the Tech Lead). */
  tools_services?: string[];
  /** working/in_review/idle (engineer) or planning/reviewing/idle (Tech Lead). */
  status: string;
  /** Id of the task the agent is currently working, when any. */
  current_task_id?: string | null;
  /** Title of the task the agent is currently working, when any. */
  current_task_title?: string | null;
  /** Live sub-step from current_activity, when this agent owns it. */
  current_step?: string | null;
  /** Human detail from current_activity, when this agent owns it. */
  activity_detail?: string | null;
  /** 0.0-1.0 progress of the live sub-step, when this agent owns it. */
  activity_fraction?: number | null;
}

/** One row of GET /jobs — enough to spot active GitHub-issue runs without per-job status calls. */
export interface CodingTeamJobListItem {
  job_id: string;
  status: string;
  repo_path?: string;
  phase?: string;
  status_text?: string;
  updated_at?: string;
  waiting_for_answers?: boolean;
  github_context?: CodingTeamGitHubContext;
}

/** Summary of a posted PR review (from the /review-pr flow). */
export interface CodeReviewSummary {
  total_issues: number;
  inline_comments: number;
  /** Findings posted as their own standalone PR conversation comments. Optional:
   * review rows persisted before the rename carry `body_findings` instead, so
   * read this through `commentFindings()` rather than directly. */
  comment_findings?: number;
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
