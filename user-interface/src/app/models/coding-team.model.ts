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
  /**
   * Set by the GitHub issue grooming flow: `score` once Phase A (complexity scoring)
   * completes, `sub_issues` added once Phase B (sub-issue split) runs. There is no
   * thread-mode grooming path to diverge from -- this is the sole surface for
   * grooming progress/stats on either execution engine.
   */
  grooming?: {
    score?: Record<string, unknown>;
    sub_issues?: Array<{ number: number; title: string }>;
  } | null;
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
   * Set only for a Temporal-native pause (`pause_strategy="return"`). Required to
   * echo on answer submit and to call `/resume`; absent for legacy block-mode pauses.
   */
  resume_token?: string | null;
  /**
   * Per-agent status roster (Tech Lead + one implementation worker per stack), derived server-side: who
   * is working now, each agent's status, and the task each is on. Absent on older records.
   */
  agents?: CodingTeamAgentStatus[];
}

/**
 * Live status of one coding-team agent, for the per-agent monitor cards. Mirrors the backend
 * `AgentStatusEntry`. The roster is the Tech Lead (coordinator) plus one implementation worker per stack.
 *
 * Properties are intentionally snake_case: like the other API-model interfaces in this codebase
 * (e.g. `CodingTeamJobStatus` above, `JobStatusResponse`), this type is the direct JSON shape of
 * the backend response, so the keys match the wire format and need no mapping layer.
 */
export interface CodingTeamAgentStatus {
  /** Stable agent id (engineer stack name, or 'tech_lead'). */
  agent_id: string;
  /** The agent's role in the team. */
  role: 'tech_lead' | 'implementation_worker' | 'senior_engineer';
  /** Human-readable label for the agent card, e.g. "Tech Lead" or "Implementation Worker - frontend". */
  display_name: string;
  /** Stack name for engineers; null for the Tech Lead. Always present (backend serializes null). */
  stack: string | null;
  /** Tools/services the engineer specializes in (empty for the Tech Lead). Always present. */
  tools_services: string[];
  /**
   * working/in_review/idle (engineer) or planning/reviewing/idle (Tech Lead). Kept as `string`
   * (not a union) because the backend types it as a free-form value and the component folds any
   * unrecognized status via `agentStatusClass`/`agentStatusLabel`.
   */
  status: string;
  // The fields below are always present in the response, carrying null when not applicable.
  /** Id of the task the agent is currently working, or null. */
  current_task_id: string | null;
  /** Title of the task the agent is currently working, or null. */
  current_task_title: string | null;
  /** Live sub-step from current_activity when this agent owns it, else null. */
  current_step: string | null;
  /** Human detail from current_activity when this agent owns it, else null. */
  activity_detail: string | null;
  /** 0.0-1.0 progress of the live sub-step when this agent owns it, else null. */
  activity_fraction: number | null;
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

/**
 * One occurrence of a combined proposal's underlying issue — the file/line
 * and per-finding description/suggestion of a single similar finding that
 * was grouped into a `PendingIssueProposal` with others like it.
 */
export interface PendingIssueProposalLocation {
  file_path: string;
  line: number | null;
  description: string;
  suggestion: string;
}

/**
 * One (possibly combined) pre-existing bug the reviewer flagged in code the
 * pull request did not change. It is NOT posted on the PR; instead it is
 * offered to the user on the Code Review page as a GitHub-issue candidate.
 * Once filed, `issue_url` / `issue_number` are populated. Similar findings
 * (same category, near-identical description — e.g. the same "bare import"
 * pattern flagged across several files) are combined into one proposal: when
 * `locations` has more than one entry, `file_path`/`line`/`description`/
 * `suggestion` mirror its first entry.
 */
export interface PendingIssueProposal {
  id: string;
  severity: string;
  category: string;
  file_path: string;
  line: number | null;
  description: string;
  suggestion: string;
  locations?: PendingIssueProposalLocation[];
  issue_number?: number | null;
  issue_url?: string | null;
}

/** Summary of a posted PR review (from the /review-pr flow), returned by the backend. */
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
  /** Count of posted PR findings by severity. Absent on reviews run before this
   * shipped; each level is optional and omitted-or-0 means "none at that level". */
  severity_counts?: {
    critical?: number;
    high?: number;
    medium?: number;
    low?: number;
    info?: number;
  };
  event: string;
  /** Pre-existing bugs the reviewer flagged in unchanged code, offered as
   * GitHub-issue candidates. Absent on reviews run before this feature. */
  pending_issue_proposals?: PendingIssueProposal[];
}

export interface TaskSnapshot {
  id: string;
  title: string;
  status: string;
  assigned_agent_id?: string;
}
