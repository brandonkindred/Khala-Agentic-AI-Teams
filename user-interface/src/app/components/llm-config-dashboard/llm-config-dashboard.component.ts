import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatRadioModule } from '@angular/material/radio';
import { MatIconModule } from '@angular/material/icon';
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
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatRadioModule,
    MatIconModule,
    MatAutocompleteModule,
    MatProgressSpinnerModule,
  ],
  templateUrl: './llm-config-dashboard.component.html',
  styleUrl: './llm-config-dashboard.component.scss',
})
export class LlmConfigDashboardComponent implements OnInit {
  private readonly api = inject(LlmConfigApiService);
  private readonly destroyRef = inject(DestroyRef);

  loading = false;
  saving = false;
  error: string | null = null;
  success: string | null = null;

  provider: LlmProvider = 'ollama';
  model = '';
  // Each provider's stored model, so toggling provider restores its own saved
  // model instead of clobbering it with the default suggestion on save.
  private ollamaModel = '';
  private claudeModel = '';
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
    this.api
      .getConfig()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (cfg) => {
          this.applyConfig(cfg);
          this.loading = false;
        },
        error: (err) => {
          this.error = this.friendlyError(err, 'Failed to load LLM configuration.');
          this.loading = false;
          // The config load failed, so we can't confirm the store is reachable;
          // mark storage unavailable to disable Save rather than letting the user
          // attempt a write that would fail.
          this.storageAvailable = false;
        },
      });
  }

  /** Populate form fields from the loaded LLM configuration. */
  private applyConfig(cfg: LlmConfigResponse): void {
    this.provider = cfg.provider === 'claude' ? 'claude' : 'ollama';
    this.model = cfg.model || '';
    this.ollamaModel = cfg.ollama_model || '';
    this.claudeModel = cfg.claude_model || '';
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

  /** React to a provider switch by loading that provider's own stored model.
   *
   * Restores the provider's saved model so a switch is lossless (saving no longer
   * overwrites it with a default), and never leaves a cross-provider model id in
   * the field. Falls back to the first curated suggestion only when the provider
   * has no stored model yet.
   */
  onProviderChange(provider: LlmProvider): void {
    this.provider = provider;
    const saved = provider === 'claude' ? this.claudeModel : this.ollamaModel;
    this.model = saved || (this.modelOptions.length ? this.modelOptions[0] : '');
  }

  /** The model suggestions for the active provider. */
  get modelOptions(): string[] {
    return this.provider === 'claude' ? this.claudeModelOptions : this.ollamaModelSuggestions;
  }

  /** True iff the URL's host is exactly ollama.com (the Cloud endpoint).
   *
   * An empty or blank URL returns true: the app treats a missing endpoint as
   * Cloud because ollama.com is the default endpoint when none is configured, so
   * the component surfaces the Cloud-key prompt rather than silently assuming
   * Local. This empty=Cloud default is relied on elsewhere in the component, so
   * it is intentional, not a bug.
   *
   * Uses the parsed hostname, not a substring, so a custom host that merely
   * contains 'ollama.com' (e.g. http://ollama.company.com) is treated as Local.
   * Has a scheme-less fallback: a value like 'ollama.com' or 'ollama.com:443'
   * makes the bare URL() parse throw, so it retries parsing with an 'https://'
   * prefix to extract the real hostname rather than comparing the raw string.
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
    this.api
      .updateConfig(body)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
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

  /** Extract a human-readable error detail from an API error response, falling back to a default message. */
  private friendlyError(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail || fallback;
  }
}
