import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatRadioModule } from '@angular/material/radio';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatAutocompleteModule } from '@angular/material/autocomplete';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import type {
  LlmConfigResponse,
  LlmConfigUpdate,
  LlmProvider,
} from '../../models/llm-config.model';

type OllamaMode = 'local' | 'cloud';

const OLLAMA_CLOUD_URL = 'https://ollama.com';
const OLLAMA_LOCAL_DEFAULT = 'http://localhost:11434';

/**
 * LLM Provider settings page. Lets an operator pick the provider (Ollama or
 * Claude), set the model, and store API keys — the UI mirror of the LLM_* env
 * vars. API keys are write-only: the server returns `*_configured` booleans, so
 * inputs are never pre-filled.
 */
@Component({
  selector: 'app-llm-config-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatRadioModule,
    MatIconModule,
    MatSelectModule,
    MatAutocompleteModule,
    MatProgressSpinnerModule,
  ],
  templateUrl: './llm-config-dashboard.component.html',
  styleUrl: './llm-config-dashboard.component.scss',
})
export class LlmConfigDashboardComponent implements OnInit {
  private readonly api = inject(LlmConfigApiService);

  loading = false;
  saving = false;
  error: string | null = null;
  success: string | null = null;

  provider: LlmProvider = 'ollama';
  model = '';
  ollamaMode: OllamaMode = 'cloud';
  ollamaBaseUrl = OLLAMA_CLOUD_URL;
  claudeApiKey = '';
  ollamaApiKey = '';

  claudeApiKeyConfigured = false;
  ollamaApiKeyConfigured = false;
  storageAvailable = true;

  providerOptions: LlmProvider[] = ['ollama', 'claude'];
  claudeModelOptions: string[] = [];
  ollamaModelSuggestions: string[] = [];

  ngOnInit(): void {
    this.loadConfig();
  }

  /** Load the current effective config and populate the form. */
  loadConfig(): void {
    this.loading = true;
    this.error = null;
    this.success = null;
    this.api.getConfig().subscribe({
      next: (cfg) => {
        this.applyConfig(cfg);
        this.loading = false;
      },
      error: (err) => {
        this.error = this.friendlyError(err, 'Failed to load LLM configuration.');
        this.loading = false;
      },
    });
  }

  private applyConfig(cfg: LlmConfigResponse): void {
    this.provider = cfg.provider === 'claude' ? 'claude' : 'ollama';
    this.model = cfg.model || '';
    this.ollamaBaseUrl = cfg.ollama_base_url || OLLAMA_CLOUD_URL;
    this.ollamaMode = this.isOllamaCloudUrl(this.ollamaBaseUrl) ? 'cloud' : 'local';
    this.claudeApiKeyConfigured = cfg.claude_api_key_configured;
    this.ollamaApiKeyConfigured = cfg.ollama_api_key_configured;
    this.storageAvailable = cfg.storage_available;
    this.providerOptions = cfg.provider_options?.length ? cfg.provider_options : ['ollama', 'claude'];
    this.claudeModelOptions = cfg.claude_model_options || [];
    this.ollamaModelSuggestions = cfg.ollama_model_suggestions || [];
    // Inputs are write-only; never echo a key back.
    this.claudeApiKey = '';
    this.ollamaApiKey = '';
  }

  /** React to the Ollama Local/Cloud toggle by defaulting the base URL. */
  onOllamaModeChange(mode: OllamaMode): void {
    this.ollamaMode = mode;
    this.ollamaBaseUrl = mode === 'cloud' ? OLLAMA_CLOUD_URL : OLLAMA_LOCAL_DEFAULT;
  }

  /** React to a provider switch by resetting the model to the new provider's default.
   *
   * Without this, a model id from the previous provider (e.g. an Ollama model)
   * would be left in the field and persisted/sent under the new provider, which
   * the API would reject. Defaults to the first curated option for the provider.
   */
  onProviderChange(provider: LlmProvider): void {
    this.provider = provider;
    this.model = this.modelOptions.length ? this.modelOptions[0] : '';
  }

  /** The model suggestions for the active provider. */
  get modelOptions(): string[] {
    return this.provider === 'claude' ? this.claudeModelOptions : this.ollamaModelSuggestions;
  }

  /** True iff the URL's host is exactly ollama.com (the Cloud endpoint).
   *
   * Uses the parsed hostname, not a substring, so a custom host that merely
   * contains 'ollama.com' (e.g. http://ollama.company.com) is treated as Local.
   */
  private isOllamaCloudUrl(url: string): boolean {
    const u = (url || '').trim();
    if (!u) {
      return true;
    }
    try {
      return new URL(u).hostname.toLowerCase() === 'ollama.com';
    } catch {
      // Scheme-less value (e.g. 'ollama.com' or 'ollama.com:443') — URL() throws,
      // so re-parse with a scheme to compare the real host, not the raw string.
      try {
        return new URL(`https://${u}`).hostname.toLowerCase() === 'ollama.com';
      } catch {
        return false;
      }
    }
  }

  /** Persist the configuration. */
  save(): void {
    this.error = null;
    this.success = null;

    if (!this.storageAvailable) {
      this.error =
        'Configuration storage is unavailable (Postgres is not configured). Set the provider via environment variables instead.';
      return;
    }

    const model = this.model.trim();
    if (!model) {
      // An empty model would be skipped by the backend, silently leaving the
      // previously stored (possibly cross-provider) model in place. Require one.
      this.error = 'Please select or enter a model for the chosen provider.';
      return;
    }

    const body: LlmConfigUpdate = { provider: this.provider, model };
    if (this.provider === 'ollama') {
      // Local mode with a cleared base URL falls back to the localhost default
      // rather than sending '' (which the backend skips, silently keeping the
      // previously stored Cloud URL).
      body.ollama_base_url =
        this.ollamaMode === 'cloud'
          ? OLLAMA_CLOUD_URL
          : this.ollamaBaseUrl.trim() || OLLAMA_LOCAL_DEFAULT;
      if (this.ollamaApiKey.trim()) {
        body.ollama_api_key = this.ollamaApiKey.trim();
      }
    } else if (this.claudeApiKey.trim()) {
      body.claude_api_key = this.claudeApiKey.trim();
    }

    this.saving = true;
    this.api.updateConfig(body).subscribe({
      next: (cfg) => {
        this.applyConfig(cfg);
        this.saving = false;
        this.success = 'LLM provider settings saved.';
      },
      error: (err) => {
        this.error = this.friendlyError(err, 'Failed to save LLM configuration.');
        this.saving = false;
      },
    });
  }

  private friendlyError(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail || fallback;
  }
}
