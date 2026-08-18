export type LlmUsageWindow = '24h' | '7d' | '30d' | 'all';

export type LlmUsageStorageStatus = 'available' | 'unconfigured' | 'unreachable';

export interface LlmUsageModelBreakdown {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface LlmUsageSummary {
  team: string;
  window: LlmUsageWindow;
  window_hours: number;
  total_calls: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  avg_latency_ms: number;
  error_count: number;
  by_agent: Record<string, { calls: number; tokens: number }>;
  by_model: Record<string, LlmUsageModelBreakdown>;
  storage_available: boolean;
  storage_status: LlmUsageStorageStatus;
}

export interface LlmUsageCall {
  timestamp: number;
  team: string;
  agent_key: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  status: string;
}
