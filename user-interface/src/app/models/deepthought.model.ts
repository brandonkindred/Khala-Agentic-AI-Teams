// ---------------------------------------------------------------------------
// Deepthought Recursive Agent System — Frontend Models
// ---------------------------------------------------------------------------

export type DecompositionStrategy =
  | 'auto'
  | 'by_discipline'
  | 'by_concern'
  | 'by_option'
  | 'by_perspective'
  | 'none';

export type AgentEventType =
  | 'agent_spawned'
  | 'agent_analysing'
  | 'agent_answering'
  | 'agent_decomposing'
  | 'agent_deliberating'
  | 'agent_synthesising'
  | 'agent_complete'
  | 'budget_warning'
  | 'knowledge_reused';

export interface AgentEvent {
  event_type: AgentEventType;
  agent_id: string;
  agent_name: string;
  depth: number;
  detail: string;
}

export interface KnowledgeEntry {
  agent_id: string;
  agent_name: string;
  focus_question: string;
  finding: string;
  confidence: number;
  tags: string[];
}

export interface AgentResult {
  agent_id: string;
  agent_name: string;
  depth: number;
  focus_question: string;
  answer: string;
  confidence: number;
  child_results: AgentResult[];
  was_decomposed: boolean;
  deliberation_notes: string | null;
  reused_from_cache: boolean;
}

export interface DeepthoughtRequest {
  message: string;
  max_depth?: number;
  conversation_history?: { role: string; content: string }[];
  decomposition_strategy?: DecompositionStrategy;
}

export interface DeepthoughtResponse {
  answer: string;
  agent_tree: AgentResult;
  total_agents_spawned: number;
  max_depth_reached: number;
  knowledge_entries: KnowledgeEntry[];
  events: AgentEvent[];
}

// ---------------------------------------------------------------------------
// UI-only types
// ---------------------------------------------------------------------------

export type AgentNodeStatus =
  | 'spawned'
  | 'analysing'
  | 'answering'
  | 'decomposing'
  | 'deliberating'
  | 'synthesising'
  | 'complete'
  | 'budget_warning'
  | 'knowledge_reused';

export interface LiveAgentNode {
  agent_id: string;
  agent_name: string;
  depth: number;
  status: AgentNodeStatus;
  detail: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  agentTree?: AgentResult;
  totalAgents?: number;
}

export interface DecompositionStrategyOption {
  value: DecompositionStrategy;
  label: string;
  description: string;
}

export const DECOMPOSITION_STRATEGIES: DecompositionStrategyOption[] = [
  { value: 'auto', label: 'Auto', description: 'Automatically choose the best strategy' },
  { value: 'by_discipline', label: 'By Discipline', description: 'Decompose by knowledge domain' },
  { value: 'by_concern', label: 'By Concern', description: 'Decompose by feasibility, cost, risk' },
  { value: 'by_option', label: 'By Option', description: 'Evaluate each option separately' },
  { value: 'by_perspective', label: 'By Perspective', description: 'Decompose by stakeholder viewpoint' },
  { value: 'none', label: 'None', description: 'Direct answer without decomposition' },
];
