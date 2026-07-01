/**
 * Models for the LLM Provider list API (`/api/llm-config/providers`).
 *
 * The ordered provider list is the sole source of LLM configuration. The backend
 * never returns API keys — only `api_key_configured` — so the UI shows "configured"
 * state and lets the operator overwrite a key, never read it.
 */

export type LlmProvider = 'ollama' | 'claude';

/**
 * Runtime-store state for the settings page:
 * - `available`   — Postgres configured AND reachable (mutations enabled).
 * - `unconfigured`— POSTGRES_HOST unset (providers can't be saved).
 * - `unreachable` — configured but the DB did not answer a probe (transient).
 */
export type LlmStorageStatus = 'available' | 'unconfigured' | 'unreachable';

/**
 * One configured provider in the ordered fallback list
 * (`/api/llm-config/providers`). API keys are never returned — only
 * `api_key_configured`. `limit_exceeded` + `reset_at` drive the usage-limit badge.
 */
export interface LlmProviderEntry {
  id: number;
  label: string;
  provider: LlmProvider;
  model: string;
  base_url: string;
  /** 0-based fallback position; lower = more preferred. */
  sort_order: number;
  api_key_configured: boolean;
  /** True while this provider is usage-limited and being skipped. */
  limit_exceeded: boolean;
  /** Lightweight label for the limit (e.g. 'rate', 'weekly'); '' when not limited. */
  limit_type: string;
  /** When the limit is expected to reset (ISO 8601, UTC); null when not limited. */
  reset_at: string | null;
}

/** Response for the provider-list endpoints — ordered most→least preferred. */
export interface LlmProviderListResponse {
  providers: LlmProviderEntry[];
  storage_available: boolean;
  storage_status: LlmStorageStatus;
}

/** Request body to add a provider to the fallback list. */
export interface LlmProviderCreate {
  label: string;
  provider: LlmProvider;
  model?: string;
  base_url?: string;
  api_key?: string;
}

/** Request body to edit a provider; omitted/empty fields keep the stored value. */
export interface LlmProviderUpdate {
  label?: string;
  provider?: LlmProvider;
  model?: string;
  base_url?: string;
  /** New API key; empty leaves the stored key unchanged. */
  api_key?: string;
  /**
   * Remove the stored API key (e.g. switching to a keyless local Ollama). A
   * non-empty `api_key` takes precedence over this flag on the server.
   */
  clear_api_key?: boolean;
}
