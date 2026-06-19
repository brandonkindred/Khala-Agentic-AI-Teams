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
    this.ollamaMode = this.ollamaBaseUrl.includes('ollama.com') ? 'cloud' : 'local';
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

  /** The model suggestions for the active provider. */
  get modelOptions(): string[] {
    return this.provider === 'claude' ? this.claudeModelOptions : this.ollamaModelSuggestions;
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

    const body: LlmConfigUpdate = { provider: this.provider, model: this.model.trim() };
    if (this.provider === 'ollama') {
      body.ollama_base_url =
        this.ollamaMode === 'cloud' ? OLLAMA_CLOUD_URL : this.ollamaBaseUrl.trim();
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
