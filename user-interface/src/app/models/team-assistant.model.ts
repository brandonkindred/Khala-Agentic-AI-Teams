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
