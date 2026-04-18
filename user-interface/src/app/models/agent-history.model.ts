/**
 * Models for Agent Console Phase 3 — saved inputs, run history, diff.
 */

export interface SavedInput {
  id: string;
  agent_id: string;
  name: string;
  input_data: unknown;
  author: string;
  description?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SavedInputCreate {
  name: string;
  input_data: unknown;
  description?: string | null;
}

export interface SavedInputUpdate {
  name?: string;
  input_data?: unknown;
  description?: string | null;
}

export type RunStatus = 'ok' | 'error';

export interface RunSummary {
  id: string;
  agent_id: string;
  team: string;
  saved_input_id?: string | null;
  status: RunStatus;
  duration_ms: number;
  trace_id: string;
  author: string;
  created_at: string;
}

export interface RunRecord extends RunSummary {
  input_data: unknown;
  output_data?: unknown | null;
  error?: string | null;
  logs_tail: string[];
  sandbox_url?: string | null;
}

export type DiffSideKind = 'run' | 'saved_input' | 'inline';

export interface DiffSide {
  kind: DiffSideKind;
  ref?: string | null;
  data?: unknown | null;
  side?: 'input' | 'output';
}

export interface DiffRequest {
  left: DiffSide;
  right: DiffSide;
}

export interface DiffResult {
  unified_diff: string;
  left_label: string;
  right_label: string;
  is_identical: boolean;
}
