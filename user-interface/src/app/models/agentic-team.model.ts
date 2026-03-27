// ---------------------------------------------------------------------------
// Agentic Team Provisioning models
// ---------------------------------------------------------------------------

export type TriggerType = 'message' | 'event' | 'schedule' | 'manual';
export type StepType = 'action' | 'decision' | 'parallel_split' | 'parallel_join' | 'wait' | 'subprocess';
export type ProcessStatus = 'draft' | 'complete' | 'archived';

export interface ProcessStepAgent {
  agent_name: string;
  role: string;
}

export interface ProcessStep {
  step_id: string;
  name: string;
  description: string;
  step_type: StepType;
  agents: ProcessStepAgent[];
  next_steps: string[];
  condition?: string | null;
}

export interface ProcessTrigger {
  trigger_type: TriggerType;
  description: string;
}

export interface ProcessOutput {
  description: string;
  destination: string;
}

export interface ProcessDefinition {
  process_id: string;
  name: string;
  description: string;
  trigger: ProcessTrigger;
  steps: ProcessStep[];
  output: ProcessOutput;
  status: ProcessStatus;
}

/** Named agent in the team's roster (per agentic team architecture). */
export interface AgenticTeamAgent {
  agent_name: string;
  role: string;
  skills: string[];
  capabilities: string[];
  tools: string[];
  expertise: string[];
}

export interface RosterGap {
  category: string;
  detail: string;
  process_id?: string | null;
  step_id?: string | null;
  agent_name?: string | null;
}

export interface RosterValidationResult {
  is_fully_staffed: boolean;
  agent_count: number;
  process_count: number;
  gaps: RosterGap[];
  summary: string;
}

export interface AgenticTeam {
  team_id: string;
  name: string;
  description: string;
  agents: AgenticTeamAgent[];
  processes: ProcessDefinition[];
  created_at: string;
  updated_at: string;
}

// Request / response types

export interface CreateAgenticTeamRequest {
  name: string;
  description: string;
}

export interface CreateAgenticTeamResponse {
  team_id: string;
  name: string;
  description: string;
  created_at: string;
}

export interface AgenticTeamSummary {
  team_id: string;
  name: string;
  description: string;
  process_count: number;
  created_at: string;
  updated_at: string;
}

export interface AgenticTeamDetailResponse {
  team: AgenticTeam;
}

// Conversation types

export interface AgenticConversationMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface CreateAgenticConversationRequest {
  initial_message?: string | null;
  team_id: string;
}

export interface SendAgenticMessageRequest {
  message: string;
}

export interface AgenticConversationStateResponse {
  conversation_id: string;
  team_id: string;
  messages: AgenticConversationMessage[];
  current_process: ProcessDefinition | null;
  suggested_questions: string[];
}

export interface AgenticConversationSummary {
  conversation_id: string;
  team_id: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

/** Per-step agent sandbox status (Agent Provisioning team). */
export interface AgentEnvProvisionSummary {
  stable_key: string;
  process_id: string;
  step_id: string;
  agent_name: string;
  provisioning_agent_id: string;
  status: string;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}
