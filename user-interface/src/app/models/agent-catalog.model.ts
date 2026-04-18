/**
 * Models for the Agent Console catalog — mirror of
 * `backend/agents/agent_registry/models.py`.
 */

import type { AccessTier } from './agent-provisioning.model';

export type InvokeKind = 'http' | 'function' | 'temporal';

export interface IOSchema {
  schema_ref?: string | null;
  description?: string | null;
}

export interface InvokeSpec {
  kind: InvokeKind;
  method?: string | null;
  path?: string | null;
  workflow?: string | null;
  callable_ref?: string | null;
}

export interface SandboxSpec {
  manifest_path?: string | null;
  access_tier?: AccessTier;
}

export interface SourceInfo {
  entrypoint: string;
  anatomy_ref?: string | null;
}

export interface AgentManifest {
  schema_version: number;
  id: string;
  team: string;
  name: string;
  summary: string;
  description?: string | null;
  tags: string[];
  inputs?: IOSchema | null;
  outputs?: IOSchema | null;
  invoke?: InvokeSpec | null;
  sandbox?: SandboxSpec | null;
  source: SourceInfo;
}

export interface AgentSummary {
  id: string;
  team: string;
  name: string;
  summary: string;
  tags: string[];
  has_input_schema: boolean;
  has_output_schema: boolean;
  has_invoke: boolean;
  has_sandbox: boolean;
}

export interface AgentDetail {
  manifest: AgentManifest;
  anatomy_markdown?: string | null;
}

export interface TeamGroup {
  team: string;
  display_name: string;
  agent_count: number;
  tags: string[];
}

export interface AgentCatalogQuery {
  team?: string;
  tag?: string;
  q?: string;
}
