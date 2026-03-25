/** Chat-based startup advisor models. */

export interface StartupAdvisorMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface StartupAdvisorArtifact {
  artifact_id: number;
  artifact_type: string;
  title: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface StartupAdvisorConversationState {
  conversation_id: string;
  messages: StartupAdvisorMessage[];
  context: Record<string, unknown>;
  artifacts: StartupAdvisorArtifact[];
  suggested_questions: string[];
}

export interface StartupAdvisorSendMessageRequest {
  message: string;
}
