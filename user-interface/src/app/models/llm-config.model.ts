/**
 * Models for the LLM Provider settings API (`/api/llm-config`).
 *
 * The backend never returns API keys — only `*_configured` booleans — so the UI
 * shows "configured" state and lets the operator overwrite a key, never read it.
 */

export type LlmProvider = 'ollama' | 'claude';

/** Response shape for GET / PUT `/api/llm-config`. */
export interface LlmConfigResponse {
  provider: LlmProvider;
  model: string;
  /** Effective Ollama model — lets the UI restore it when toggling provider. */
  ollama_model: string;
  /** Effective Claude model — lets the UI restore it when toggling provider. */
  claude_model: string;
  ollama_base_url: string;
  claude_api_key_configured: boolean;
  ollama_api_key_configured: boolean;
  /** False when POSTGRES_HOST is unset — PUT returns 503 and config is env-only. */
  storage_available: boolean;
  provider_options: LlmProvider[];
  claude_model_options: string[];
  ollama_model_suggestions: string[];
}

/** Response shape for GET `/api/llm-config/ollama-models`. */
export interface OllamaModelsResponse {
  /** Available model ids — live from `/api/tags`, or the curated fallback. */
  models: string[];
  /** Effective Ollama base URL the list was fetched from. */
  base_url: string;
  /** 'live' when fetched from the endpoint, 'fallback' for the curated list. */
  source: 'live' | 'fallback';
}

/** Request body for PUT `/api/llm-config`. Empty fields leave stored values untouched. */
export interface LlmConfigUpdate {
  provider: LlmProvider;
  model?: string;
  ollama_base_url?: string;
  claude_api_key?: string;
  ollama_api_key?: string;
}
