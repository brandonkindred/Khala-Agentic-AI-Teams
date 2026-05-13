/**
 * Models for the Agent Provisioning Team API.
 *
 * Permission tiers were removed (#456): every sandbox is provisioned
 * with full access on every backing service.
 */

/** Phases in the provisioning workflow. */
export type ProvisioningPhase =
  | 'setup'
  | 'credential_generation'
  | 'account_provisioning'
  | 'access_audit'
  | 'documentation'
  | 'deliver';

/** Phase definition for UI display. */
export interface ProvisioningPhaseDefinition {
  id: ProvisioningPhase;
  label: string;
  icon: string;
  progress: number;
}

/** Provisioning phases for stepper display. */
export const PROVISIONING_PHASES: ProvisioningPhaseDefinition[] = [
  { id: 'setup', label: 'Setup', icon: 'build', progress: 5 },
  { id: 'credential_generation', label: 'Credentials', icon: 'key', progress: 20 },
  { id: 'account_provisioning', label: 'Accounts', icon: 'person_add', progress: 50 },
  { id: 'access_audit', label: 'Audit', icon: 'verified_user', progress: 70 },
  { id: 'documentation', label: 'Documentation', icon: 'description', progress: 85 },
  { id: 'deliver', label: 'Deliver', icon: 'check_circle', progress: 100 },
];

/** Request to start a provisioning job. */
export interface ProvisionRequest {
  agent_id: string;
  manifest_path?: string;
  workspace_path?: string;
}

/** Response when starting a provisioning job. */
export interface ProvisionJobResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Generated credentials for a tool. */
export interface GeneratedCredentials {
  tool_name: string;
  username?: string;
  password?: string;
  token?: string;
  ssh_private_key?: string;
  ssh_public_key?: string;
  connection_string?: string;
  extra?: Record<string, unknown>;
}

/** Environment information. */
export interface EnvironmentInfo {
  container_id: string;
  container_name: string;
  ssh_host: string;
  ssh_port: number;
  workspace_path: string;
  status: string;
}

/** Tool provision result. */
export interface ToolProvisionResult {
  tool_name: string;
  success: boolean;
  credentials?: GeneratedCredentials;
  permissions: string[];
  error?: string;
  details?: Record<string, unknown>;
}

/** Tool onboarding information. */
export interface ToolOnboardingInfo {
  name: string;
  description: string;
  env_var?: string;
  getting_started: string;
  permissions: string[];
}

/** Onboarding packet with all tool information. */
export interface OnboardingPacket {
  summary: string;
  tools: ToolOnboardingInfo[];
  environment_variables: Record<string, string>;
}

/** Complete provisioning result. */
export interface ProvisioningResult {
  agent_id: string;
  current_phase: ProvisioningPhase;
  completed_phases: ProvisioningPhase[];
  environment?: EnvironmentInfo;
  credentials: Record<string, GeneratedCredentials>;
  tool_results: ToolProvisionResult[];
  onboarding?: OnboardingPacket;
  success: boolean;
  error?: string;
}

/** Job status response. */
export interface ProvisionStatusResponse {
  job_id: string;
  status: string;
  agent_id?: string;
  current_phase?: ProvisioningPhase;
  current_tool?: string;
  progress: number;
  tools_completed: number;
  tools_total: number;
  completed_phases: string[];
  error?: string;
  result?: ProvisioningResult;
}

/** Job summary for listing. */
export interface ProvisionJobSummary {
  job_id: string;
  agent_id: string;
  status: string;
  created_at?: string;
  current_phase?: string;
  progress: number;
}

/** Response for listing jobs. */
export interface ProvisionJobsListResponse {
  jobs: ProvisionJobSummary[];
}

/** Agent environment status. */
export interface AgentStatusResponse {
  agent_id: string;
  status: string;
  container_id?: string;
  container_name?: string;
  tools_provisioned: string[];
  created_at?: string;
}

/** Response for listing agents. */
export interface AgentListResponse {
  agents: AgentStatusResponse[];
}

/** Health check response. */
export interface ProvisioningHealthResponse {
  status: string;
  service: string;
}
