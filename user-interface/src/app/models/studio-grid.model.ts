/** Models for the StudioGrid design-system workflow API. */

export interface StartRunRequest {
  project_name: string;
  intake: Record<string, unknown>;
}

export interface StartRunResponse {
  project_id: string;
  run_id: string;
  status: string;
  phase: string;
}

export interface RunStatus {
  project_id: string;
  run_id: string;
  phase: string;
  status: string;
  contract_version?: number;
  updated_at?: string;
  waiting_decision_id?: string;
}

export interface DecisionOption {
  key: string;
  label: string;
  description?: string;
}

export interface Decision {
  decision_id: string;
  run_id: string;
  title: string;
  context: string;
  options: DecisionOption[];
  status: 'OPEN' | 'CHOSEN';
  selected_option_key?: string;
}

export interface DecisionListResponse {
  run_id: string;
  decisions: Decision[];
}

export interface ArtifactRef {
  artifact_id: string;
  artifact_type: string;
  version: number;
  format: string;
  uri: string;
}

export interface AgentInfo {
  agent_id: string;
  skills?: string[];
  tools?: string[];
  keywords?: string[];
  actions?: string[];
}

export interface AgentListResponse {
  agents: AgentInfo[];
}

export interface FindAgentsRequest {
  problem: string;
  skills: string[];
  limit?: number;
}

export interface FindAgentsResponse {
  problem: string;
  required_skills: string[];
  assisting_agents: AgentInfo[];
  should_spawn_sub_agents: boolean;
}
