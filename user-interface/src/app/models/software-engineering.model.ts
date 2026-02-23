/** Request for POST /run-team. */
export interface RunTeamRequest {
  repo_path: string;
  clarification_session_id?: string;
}

/** Response from POST /run-team. */
export interface RunTeamResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Summary of a single job for the running jobs list (GET /run-team/jobs). */
export interface RunningJobSummary {
  job_id: string;
  status: string;
  repo_path?: string;
  job_type: string;
}

/** Response from GET /run-team/jobs. */
export interface RunningJobsResponse {
  jobs: RunningJobSummary[];
}

/** Detail about a single failed task. */
export interface FailedTaskDetail {
  task_id: string;
  title?: string;
  reason?: string;
}

/** Per-task execution state for tracking panel / graph. */
export interface TaskStateEntry {
  status: string;
  assignee: string;
  title?: string;
  dependencies?: string[];
  started_at?: string;
  finished_at?: string;
  error?: string;
}

/** Per-team progress when multiple teams run in parallel. */
export interface TeamProgressEntry {
  current_phase?: string;
  progress?: number;
  current_task_id?: string;
}

/** Response from GET /run-team/{job_id}. */
export interface JobStatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  requirements_title?: string;
  architecture_overview?: string;
  current_task?: string;
  task_results: unknown[];
  task_ids: string[];
  progress?: number;
  error?: string;
  failed_tasks: FailedTaskDetail[];
  /** Job-level phase: planning, execution, or completed. */
  phase?: string;
  /** Per-task state for execution tracking graph. */
  task_states?: Record<string, TaskStateEntry>;
  /** Per-team progress when multiple teams run in parallel. */
  team_progress?: Record<string, TeamProgressEntry>;
}

/** Response from POST /run-team/{job_id}/retry-failed. */
export interface RetryResponse {
  job_id: string;
  status: string;
  retrying_tasks: string[];
  message: string;
}

/** Request for POST /run-team/{job_id}/re-plan-with-clarifications. */
export interface RePlanWithClarificationsRequest {
  clarification_session_id: string;
}

/** Request for POST /clarification/sessions. */
export interface ClarificationCreateRequest {
  spec_text: string;
}

/** Request for POST /clarification/sessions/{id}/messages. */
export interface ClarificationMessageRequest {
  message: string;
}

/** Response from clarification endpoints. */
export interface ClarificationResponse {
  session_id: string;
  assistant_message: string;
  open_questions: string[];
  assumptions: string[];
  done_clarifying: boolean;
  refined_spec?: string;
}

/** Response from GET /clarification/sessions/{id}. */
export interface ClarificationSessionResponse {
  session_id: string;
  spec_text: string;
  status: string;
  created_at: string;
  clarification_round: number;
  max_rounds: number;
  confidence_score: number;
  open_questions: string[];
  assumptions: string[];
  refined_spec?: string;
  turns: Array<{ role: string; message: string; timestamp?: string }>;
}

/** Request for POST /architect/design. */
export interface ArchitectDesignRequest {
  spec: string;
  use_llm?: boolean;
}

/** Architecture component from architect/design response. */
export interface ArchitectComponent {
  name: string;
  type: string;
  description?: string;
  technology?: string;
  dependencies?: string[];
  interfaces?: string[];
}

/** Response from POST /architect/design. */
export interface ArchitectDesignResponse {
  overview: string;
  architecture_document: string;
  components: ArchitectComponent[];
  diagrams: Record<string, string>;
  decisions: Array<Record<string, unknown>>;
  tenancy_model: string;
  reliability_model: string;
  summary: string;
}

// ---------------------------------------------------------------------------
// Backend-Code-V2
// ---------------------------------------------------------------------------

/** Task input for backend-code-v2. */
export interface BackendCodeV2TaskInput {
  id?: string;
  title: string;
  description: string;
  requirements?: string;
  acceptance_criteria?: string[];
}

/** Request for POST /backend-code-v2/run. */
export interface BackendCodeV2RunRequest {
  task: BackendCodeV2TaskInput;
  repo_path: string;
  spec_content?: string;
  architecture?: string;
}

/** Response from POST /backend-code-v2/run. */
export interface BackendCodeV2RunResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Response from GET /backend-code-v2/status/{job_id}. */
export interface BackendCodeV2StatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  current_phase?: string;
  current_microtask?: string;
  progress: number;
  microtasks_completed: number;
  microtasks_total: number;
  completed_phases: string[];
  error?: string;
  summary?: string;
}

// ---------------------------------------------------------------------------
// Planning-V2
// ---------------------------------------------------------------------------

/** Request for POST /planning-v2/run. */
export interface PlanningV2RunRequest {
  spec_content: string;
  repo_path: string;
  inspiration_content?: string;
}

/** Response from POST /planning-v2/run. */
export interface PlanningV2RunResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Response from GET /planning-v2/status/{job_id}. */
export interface PlanningV2StatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  current_phase?: string;
  progress: number;
  completed_phases: string[];
  active_roles?: string[];
  error?: string;
  summary?: string;
}

/** Response from GET /planning-v2/result/{job_id}. */
export interface PlanningV2ResultResponse {
  job_id: string;
  status: string;
  phase_results: Record<string, unknown>;
  summary?: string;
  error?: string;
}
