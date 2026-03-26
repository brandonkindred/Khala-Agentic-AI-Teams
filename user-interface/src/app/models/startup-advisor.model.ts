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

export interface StartupAdvisorUpdateContextRequest {
  context: Record<string, string>;
}

/** Standard profile fields the manual form exposes. */
export const STARTUP_ADVISOR_PROFILE_FIELDS: { key: string; label: string; placeholder: string }[] = [
  { key: 'company_name', label: 'Company Name', placeholder: 'e.g. Acme Inc.' },
  { key: 'industry', label: 'Industry', placeholder: 'e.g. FinTech, HealthTech, SaaS' },
  { key: 'stage', label: 'Stage', placeholder: 'e.g. Pre-seed, Seed, Series A' },
  { key: 'target_market', label: 'Target Market', placeholder: 'e.g. SMBs in North America' },
  { key: 'business_model', label: 'Business Model', placeholder: 'e.g. B2B SaaS, Marketplace' },
  { key: 'team_size', label: 'Team Size', placeholder: 'e.g. 3 co-founders, 2 engineers' },
  { key: 'funding_status', label: 'Funding Status', placeholder: 'e.g. Bootstrapped, $500K raised' },
  { key: 'main_challenge', label: 'Main Challenge', placeholder: 'What are you struggling with most?' },
];
