/** Shared models for the generic team assistant chat. */

export interface TeamAssistantMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface TeamAssistantArtifact {
  artifact_id: number;
  artifact_type: string;
  title: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface TeamAssistantConversationState {
  conversation_id: string;
  messages: TeamAssistantMessage[];
  context: Record<string, unknown>;
  artifacts: TeamAssistantArtifact[];
  suggested_questions: string[];
}

export interface TeamAssistantReadiness {
  ready: boolean;
  missing_fields: string[];
  context: Record<string, unknown>;
}

/** Summary of a team assistant conversation (for listing). */
export interface TeamConversationSummary {
  conversation_id: string;
  job_id?: string | null;
  created_at: string;
  brief: string;
}

/** Describes one field shown in the form panel alongside the chat. */
export interface TeamAssistantFieldSpec {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
}

/** Result of POST /api/{team}/assistant/launch — the team's real run endpoint
 *  was dispatched in-process. `job_id` is null when the team returns results
 *  synchronously (e.g. market research, deepthought, road trip planning). */
export interface TeamAssistantLaunchResponse {
  ok: boolean;
  job_id: string | null;
  conversation_id: string;
  upstream_status: number;
  upstream_body: Record<string, unknown>;
}
