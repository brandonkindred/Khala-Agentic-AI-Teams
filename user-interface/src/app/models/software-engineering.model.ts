/** Request for POST /run-team. */
export interface RunTeamRequest {
  repo_path: string;
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
  /** ISO timestamp when job was created. */
  created_at?: string;
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
  /** Parent initiative ID from planning hierarchy. */
  initiative_id?: string;
  /** Parent epic ID from planning hierarchy. */
  epic_id?: string;
  /** Parent story ID from planning hierarchy. */
  story_id?: string;
}

/** Fine-grained activity of the currently running sub-agent (e.g. code review sub-steps). */
export interface CurrentActivityEntry {
  /** Sub-agent reporting the activity: code_review or tech_lead_review. */
  agent?: string;
  /** Current step: preparing, reviewing, waiting_retry, parsing, finalizing, or done. */
  step?: string;
  /** Human-readable detail (e.g. 'chunk 2/5: src/app.py' or 'attempt 2/3'). */
  detail?: string;
  /** 0.0-1.0 progress through the sub-agent's own process. */
  fraction?: number;
  task_id?: string;
  task_title?: string;
}

/** Per-team progress when multiple teams run in parallel. */
export interface TeamProgressEntry {
  current_phase?: string;
  progress?: number;
  current_task_id?: string;
  current_microtask?: string;
  /**
   * One of MICROTASK_PHASES' ids, "completed", or "qa_security_testing" (not
   * in MICROTASK_PHASES) -- the backend reports the latter while QA and
   * Security run concurrently; neither has a confirmed outcome yet, so it
   * must not be inferred as "qa_testing passed".
   */
  current_microtask_phase?: string;
  phase_detail?: string;
  current_microtask_index?: number;
  microtasks_completed?: number;
  microtasks_total?: number;
}

// ---------------------------------------------------------------------------
// Phase Definitions for Subprocess Tracking
// ---------------------------------------------------------------------------

/** Phase definition for subprocess steppers. */
export interface PhaseDefinition {
  id: string;
  label: string;
  icon: string;
}

/** Planning subprocess phases (the Planning team's workflow). */
export const PLANNING_PHASES: PhaseDefinition[] = [
  { id: 'intake', label: 'Intake', icon: 'input' },
  { id: 'discovery', label: 'Discovery', icon: 'search' },
  { id: 'requirements', label: 'Requirements', icon: 'checklist' },
  { id: 'synthesis', label: 'Synthesis', icon: 'merge_type' },
  { id: 'document_production', label: 'Document Production', icon: 'description' },
  { id: 'sub_agent_provisioning', label: 'Sub-Agent Provisioning', icon: 'smart_toy' },
];

/** Code team phases (backend-code-v2, frontend-code-v2). */
export const CODE_TEAM_PHASES: PhaseDefinition[] = [
  { id: 'setup', label: 'Setup', icon: 'settings' },
  { id: 'planning', label: 'Planning', icon: 'event_note' },
  { id: 'execution', label: 'Execution', icon: 'code' },
  { id: 'documentation', label: 'Documentation', icon: 'article' },
  { id: 'deliver', label: 'Deliver', icon: 'local_shipping' },
];

/** Microtask lifecycle phases within execution. */
export const MICROTASK_PHASES: PhaseDefinition[] = [
  { id: 'coding', label: 'Coding', icon: 'code' },
  { id: 'code_review', label: 'Code Review', icon: 'rate_review' },
  { id: 'qa_testing', label: 'QA Testing', icon: 'bug_report' },
  { id: 'security_testing', label: 'Security', icon: 'security' },
  { id: 'documentation', label: 'Documentation', icon: 'description' },
];

/** Product Analysis subprocesses (spec_review, communicate, spec_update, spec_cleanup). */
export const PRODUCT_ANALYSIS_PHASES: PhaseDefinition[] = [
  { id: 'spec_review', label: 'Spec Review', icon: 'description' },
  { id: 'communicate', label: 'User Questions', icon: 'question_answer' },
  { id: 'spec_update', label: 'Spec Update', icon: 'edit_note' },
  { id: 'spec_cleanup', label: 'Cleanup', icon: 'cleaning_services' },
];

// ---------------------------------------------------------------------------
// Planning Hierarchy Types for Work Breakdown Tree
// ---------------------------------------------------------------------------

/** Initiative item in planning hierarchy. */
export interface PlanningInitiative {
  id: string;
  title: string;
  description?: string;
}

/** Epic item in planning hierarchy. */
export interface PlanningEpic {
  id: string;
  title: string;
  description?: string;
  initiative_id: string;
}

/** Story item in planning hierarchy. */
export interface PlanningStory {
  id: string;
  title: string;
  description?: string;
  epic_id: string;
  initiative_id: string;
}

/** Planning hierarchy for work breakdown tree display. */
export interface PlanningHierarchy {
  initiatives: PlanningInitiative[];
  epics: PlanningEpic[];
  stories: PlanningStory[];
}

/** Response from GET /run-team/{job_id}. */
export interface JobStatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  requirements_title?: string;
  architecture_overview?: string;
  current_task?: string;
  /** Human-readable status message describing current activity. */
  status_text?: string;
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
  /** Questions awaiting user response before job can proceed. */
  pending_questions?: PendingQuestion[];
  /** True when job is blocked waiting for user to answer pending questions. */
  waiting_for_answers?: boolean;
  /** True when cancellation has been requested for this job. */
  cancel_requested?: boolean;
  /** Current subprocess within planning phase (planning, implementation, etc.). */
  planning_subprocess?: string;
  /** Completed subprocesses within the planning phase. */
  planning_completed_phases?: string[];
  /** Current subprocess within product_analysis phase (spec_review, communicate, spec_update, spec_cleanup). */
  analysis_subprocess?: string;
  /** Completed subprocesses within the product_analysis phase. */
  analysis_completed_phases?: string[];
  /** Planning hierarchy with initiatives, epics, stories for work breakdown tree display. */
  planning_hierarchy?: PlanningHierarchy;
  /** Fine-grained activity of the currently running sub-agent (e.g. code review sub-steps). */
  current_activity?: CurrentActivityEntry;
  /** ISO timestamp of the last real orchestrator update (heartbeats excluded). */
  last_activity_at?: string;
  /** ISO timestamp of the last job update. */
  updated_at?: string;
  /** ISO timestamp of the last heartbeat (liveness of the worker process). */
  last_heartbeat_at?: string;
  /** Server UTC time when the response was built; staleness math uses this, not the browser clock. */
  server_time?: string | null;
}

/** Response from POST /run-team/{job_id}/retry-failed. */
export interface RetryResponse {
  job_id: string;
  status: string;
  retrying_tasks: string[];
  message: string;
}

// ---------------------------------------------------------------------------
// Pending Questions (Structured Q&A)
// ---------------------------------------------------------------------------

/** A selectable option for a pending question. */
export interface QuestionOption {
  id: string;
  label: string;
  rationale?: string;
  confidence?: number;
  is_default?: boolean;
}

/** A question awaiting user response during job execution. */
export interface PendingQuestion {
  id: string;
  question_text: string;
  context?: string;
  /** Agent recommendation: which option to choose and why. */
  recommendation?: string;
  options: QuestionOption[];
  required: boolean;
  source: string;
  /** Whether multiple options can be selected for this question. */
  allow_multiple?: boolean;
  /** Category of the question (e.g., architecture, security, ux). */
  category?: string;
  /** Priority of the question (high, medium, low). */
  priority?: string;
}

/** A user's answer to a pending question. */
export interface AnswerSubmission {
  question_id: string;
  /** Selected option ID (for single-select questions). */
  selected_option_id: string | null;
  /** Selected option IDs (for multi-select questions). */
  selected_option_ids?: string[];
  other_text: string | null;
}

/** Request for POST /run-team/{job_id}/answers (and sibling HITL answer endpoints). */
export interface SubmitAnswersRequest {
  answers: AnswerSubmission[];
  /**
   * Echo of a Temporal-native pause's `resume_token` from status/pause notification.
   * Required by coding-team when the job carries a resume_token; ignored elsewhere.
   */
  resume_token?: string | null;
}

// ---------------------------------------------------------------------------
// Auto-Answer
// ---------------------------------------------------------------------------

/** Request for POST /run-team/{job_id}/auto-answer/{question_id}. */
export interface AutoAnswerRequest {
  spec_context?: string;
}

/** Response from auto-answer endpoints. */
export interface AutoAnswerResponse {
  question_id: string;
  selected_option_id: string;
  selected_answer: string;
  rationale: string;
  confidence: number;
  risks: string[];
  applied: boolean;
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
  decisions: Record<string, unknown>[];
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
  /** Short human-readable status (e.g. what is being worked on right now). */
  status_text?: string;
}

// ---------------------------------------------------------------------------
// Frontend-Code-V2
// ---------------------------------------------------------------------------

/** Task input for frontend-code-v2. */
export interface FrontendCodeV2TaskInput {
  id?: string;
  title: string;
  description: string;
  requirements?: string;
  acceptance_criteria?: string[];
}

/** Request for POST /frontend-code-v2/run. */
export interface FrontendCodeV2RunRequest {
  task: FrontendCodeV2TaskInput;
  repo_path: string;
  spec_content?: string;
  architecture?: string;
}

/** Response from POST /frontend-code-v2/run. */
export interface FrontendCodeV2RunResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Response from GET /frontend-code-v2/status/{job_id}. */
export interface FrontendCodeV2StatusResponse {
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
  /** Short human-readable status (e.g. what is being worked on right now). */
  status_text?: string;
}

// ---------------------------------------------------------------------------
// Product Analysis
// ---------------------------------------------------------------------------

/** Request for POST /product-analysis/run. */
export interface ProductAnalysisRunRequest {
  repo_path: string;
  spec_content?: string;
}

/** Request for POST /product-analysis/start-from-spec (create project from spec and start PRA). */
export interface StartFromSpecRequest {
  project_name: string;
  spec_content: string;
}

/** Response from POST /product-analysis/run. */
export interface ProductAnalysisRunResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Response from GET /product-analysis/status/{job_id}. */
export interface ProductAnalysisStatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  current_phase?: string;
  /** Human-readable status message describing current activity. */
  status_text?: string;
  progress: number;
  iterations: number;
  pending_questions?: PendingQuestion[];
  waiting_for_answers?: boolean;
  error?: string;
  summary?: string;
  validated_spec_path?: string;
}
