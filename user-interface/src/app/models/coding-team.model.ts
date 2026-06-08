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
}

/** Summary of a posted PR review (from the /review-pr flow). */
export interface CodeReviewSummary {
  total_issues: number;
  inline_comments: number;
  body_findings: number;
  event: string;
}

export interface TaskSnapshot {
  id: string;
  title: string;
  status: string;
  assigned_agent_id?: string;
}
