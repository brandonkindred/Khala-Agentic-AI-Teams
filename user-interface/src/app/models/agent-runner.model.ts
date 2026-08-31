/**
 * Models for the Agent Console Runner (Phase 2).
 */

export type SandboxStatus = 'cold' | 'warming' | 'warm' | 'error';

export interface SandboxHandle {
  agent_id: string;
  team: string;
  status: SandboxStatus;
  url: string | null;
  service_name: string;
  container_name: string;
  host_port: number;
  created_at?: string | null;
  last_used_at?: string | null;
  idle_seconds?: number | null;
  error?: string | null;
}

export interface InvokeEnvelope {
  output: unknown;
  duration_ms: number;
  trace_id: string;
  logs_tail: string[];
  error?: string | null;
  sandbox?: { agent_id: string; url: string | null };
}

/**
 * Type guard for `InvokeEnvelope`. Used to distinguish a 422 response whose
 * `detail` is a wrapped invocation envelope (trace_id + logs_tail) from a
 * plain validation-error string or array.
 */
export function isInvokeEnvelope(value: unknown): value is InvokeEnvelope {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Record<string, unknown>)['trace_id'] === 'string' &&
    Array.isArray((value as Record<string, unknown>)['logs_tail'])
  );
}

export interface InvokeWarmingResponse {
  status: SandboxStatus;
  message: string;
  sandbox: { agent_id: string; status: SandboxStatus };
}
