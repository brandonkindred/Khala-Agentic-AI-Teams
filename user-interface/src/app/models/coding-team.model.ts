export interface CodingTeamJobStatus {
  job_id: string;
  status: string;
  phase?: string;
  status_text?: string;
  error?: string;
  github_context?: {
    owner: string;
    repo: string;
    issue_number: number;
    issue_url: string;
  };
  github_pr_url?: string;
  task_graph_snapshot?: TaskSnapshot[];
}

export interface TaskSnapshot {
  id: string;
  title: string;
  status: string;
  assigned_agent_id?: string;
}
