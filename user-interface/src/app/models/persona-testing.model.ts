/** Available persona for SE team testing. */
export interface PersonaInfo {
  id: string;
  name: string;
  description: string;
  icon: string;
}

/** Summary of a persona test run (from GET /runs). */
export interface PersonaTestRun {
  run_id: string;
  status: string;
  se_job_id?: string;
  analysis_job_id?: string;
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
