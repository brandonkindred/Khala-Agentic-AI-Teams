/** A persona that can be directed to autonomously test another team. */
export interface PersonaInfo {
  id: string;
  name: string;
  description: string;
  icon: string;
  is_builtin: boolean;
  system_prompt: string;
  spec_generation_prompt: string;
  created_at: string;
  updated_at: string;
}

/** Body for POST /personas. */
export interface CreatePersonaRequest {
  name: string;
  description: string;
  icon: string;
  system_prompt: string;
  spec_generation_prompt: string;
}

/** Body for PUT /personas/{id}. All fields optional. */
export interface UpdatePersonaRequest {
  name?: string;
  description?: string;
  icon?: string;
  system_prompt?: string;
  spec_generation_prompt?: string;
}

/** A target team that personas can drive (from GET /testable-teams). */
export interface TestableTeam {
  team_key: string;
  display_name: string;
}

/** Summary of a persona test run (from GET /runs). */
export interface PersonaTestRun {
  run_id: string;
  status: string;
  se_job_id?: string;
  analysis_job_id?: string;
  target_team_key?: string;
  persona_id?: string;
  project_name?: string;
  created_at: string;
  updated_at: string;
  error?: string;
}

/** A single decision made by the persona during a test run. */
export interface PersonaDecision {
  decision_id: number;
  question_id: string;
  question_text: string;
  answer_text: string;
  rationale: string;
  timestamp: string;
}

/** Full detail of a persona test run including decisions (from GET /status/{run_id}). */
export interface PersonaTestRunDetail extends PersonaTestRun {
  spec_content?: string;
  repo_path?: string;
  decisions: PersonaDecision[];
}

/**
 * Body for POST /start.
 *
 * Properties are intentionally `snake_case` to mirror the backend JSON contract
 * 1:1 — these objects are serialized straight onto the wire, so a camelCase
 * model would need a transform layer for no benefit. Treat this interface as the
 * API DTO, not an app-domain type.
 */
export interface StartTestRequest {
  persona_id: string;
  target_team_key: string;
  project_name?: string;
  /**
   * Process the persona should drive, for agentic-team targets
   * (`target_team_key === 'agentic_team:<id>'`). Required by the backend for
   * those targets; ignored by the software-engineering target.
   */
  process_id?: string;
}

/** Artifacts produced during a persona test run (from GET /runs/{run_id}/artifacts). */
export interface RunArtifacts {
  run_id: string;
  se_job_id?: string;
  se_job_status?: Record<string, unknown>;
  repo_path?: string;
  spec_content?: string;
}

/** A chat message from the persona test run log or user interaction. */
export interface PersonaChatMessage {
  message_id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_type: 'chat' | 'question_received' | 'answer_given' | 'status_update';
  metadata?: Record<string, unknown>;
  timestamp: string;
}

/** Chat history response from GET/POST /runs/{run_id}/chat. */
export interface PersonaChatHistory {
  run_id: string;
  messages: PersonaChatMessage[];
}

/**
 * Terminal statuses for persona-test runs: once a run reports one of these it will not change
 * again, so polling can stop. Includes both the British ('cancelled') and American ('canceled')
 * spellings because the backend pipeline status string is not normalized at the source.
 */
export const PERSONA_RUN_TERMINAL_STATUSES: readonly string[] = [
  'completed',
  'failed',
  'cancelled',
  'canceled',
];

/** True once a persona-test run has reached a terminal (no-longer-changing) state. */
export function isPersonaRunTerminal(status: string | null | undefined): boolean {
  return !!status && PERSONA_RUN_TERMINAL_STATUSES.includes(status);
}
