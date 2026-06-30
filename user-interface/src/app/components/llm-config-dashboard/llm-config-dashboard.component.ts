import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable } from 'rxjs';
import { FormsModule } from '@angular/forms';
import { CdkDragDrop, DragDropModule, moveItemInArray } from '@angular/cdk/drag-drop';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatRadioModule } from '@angular/material/radio';
import { MatSelectModule } from '@angular/material/select';
import { MatIconModule } from '@angular/material/icon';
import { MatAutocompleteModule } from '@angular/material/autocomplete';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import type {
  LlmConfigResponse,
  LlmConfigUpdate,
  LlmProvider,
  LlmProviderCreate,
  LlmProviderEntry,
  LlmProviderListResponse,
  LlmProviderUpdate,
  LlmStorageStatus,
  OllamaModelsResponse,
} from '../../models/llm-config.model';

type OllamaMode = 'local' | 'cloud';

const OLLAMA_CLOUD_URL = 'https://ollama.com';
const OLLAMA_LOCAL_DEFAULT = 'http://localhost:11434';

/** Editable form fields for a provider list entry (add or edit). */
interface ProviderForm {
  label: string;
  provider: LlmProvider;
  model: string;
  base_url: string;
  api_key: string;
}

function emptyProviderForm(): ProviderForm {
  return { label: '', provider: 'ollama', model: '', base_url: '', api_key: '' };
}

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
    DragDropModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatRadioModule,
    MatSelectModule,
    MatIconModule,
    MatAutocompleteModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
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
  /** True while the live Ollama model list is being fetched. */
  ollamaModelsLoading = false;
  /** Soft, non-blocking note shown beside the Model field (e.g. fetch fell back). */
  modelNote: string | null = null;

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
  // Why the store is (un)writable, so the banner can say *why* Save is disabled
  // rather than always blaming "not configured". The single source of truth —
  // `storageAvailable` is derived from it so the two can never drift.
  storageStatus: LlmStorageStatus = 'available';

  /** Save is allowed only when the store is configured AND reachable. */
  get storageAvailable(): boolean {
    return this.storageStatus === 'available';
  }

  providerOptions: LlmProvider[] = ['ollama', 'claude'];
  claudeModelOptions: string[] = [];
  ollamaModelSuggestions: string[] = [];

  // --- Multi-provider fallback list ---------------------------------------
  /** Ordered providers (most→least preferred). Empty = use the single default below. */
  providers: LlmProviderEntry[] = [];
  providersLoading = false;
  providersError: string | null = null;
  providersSaving = false;
  /** The add-provider form is shown when this is non-null. */
  addForm: ProviderForm | null = null;
  /** The id of the entry being edited inline, or null. */
  editingId: number | null = null;
  editForm: ProviderForm = emptyProviderForm();

  ngOnInit(): void {
    this.loadConfig();
    this.loadProviders();
  }

  /** Load the ordered provider list. */
  loadProviders(): void {
    this.providersLoading = true;
    this.providersError = null;
    this.api
      .listProviders()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          this.applyProviderList(res);
          this.providersLoading = false;
        },
        error: (err) => {
          this.providersError = this.friendlyError(err, 'Failed to load provider list.');
          this.providersLoading = false;
        },
      });
  }

  private applyProviderList(res: LlmProviderListResponse): void {
    // Trust the server order; sort defensively in case a client reorders the array.
    this.providers = [...(res.providers ?? [])].sort((a, b) => a.sort_order - b.sort_order);
    if (res.storage_status) {
      this.storageStatus = res.storage_status;
    }
  }

  /** Reorder the list on drag-drop and persist the new order. */
  onProviderDrop(event: CdkDragDrop<LlmProviderEntry[]>): void {
    // Ignore a drop while a save is already in flight: two concurrent reorder
    // requests could resolve out of order and corrupt the persisted order. The drop
    // list is also disabled in the template while saving (belt-and-suspenders).
    if (this.providersSaving) {
      return;
    }
    if (event.previousIndex === event.currentIndex) {
      return;
    }
    // Reorder is the only optimistic update — snapshot the previous order so the
    // error path can revert it (the list is updated locally before the server confirms).
    const previous = [...this.providers];
    moveItemInArray(this.providers, event.previousIndex, event.currentIndex);
    const ids = this.providers.map((p) => p.id);
    this.persistProviders(this.api.reorderProviders(ids), 'Provider order saved.', {
      revert: () => {
        this.providers = previous;
      },
    });
  }

  /** Open the add-provider form with sensible defaults. */
  startAdd(): void {
    this.editingId = null;
    this.addForm = { ...emptyProviderForm(), base_url: OLLAMA_LOCAL_DEFAULT };
  }

  cancelAdd(): void {
    this.addForm = null;
  }

  /** Submit the add-provider form. */
  submitAdd(): void {
    if (!this.addForm) {
      return;
    }
    const form = this.addForm;
    if (!form.label.trim()) {
      this.providersError = 'Please enter a label for the provider.';
      return;
    }
    const body: LlmProviderCreate = {
      label: form.label.trim(),
      provider: form.provider,
      model: form.model.trim(),
      base_url: form.provider === 'ollama' ? form.base_url.trim() : '',
      api_key: form.api_key.trim(),
    };
    this.persistProviders(this.api.createProvider(body), 'Provider added.', {
      onSuccess: () => {
        this.addForm = null;
      },
    });
  }

  /** Begin inline editing of an entry (keys are never pre-filled). */
  startEdit(entry: LlmProviderEntry): void {
    this.addForm = null;
    this.editingId = entry.id;
    this.editForm = {
      label: entry.label,
      provider: entry.provider,
      model: entry.model,
      base_url: entry.base_url,
      api_key: '',
    };
  }

  cancelEdit(): void {
    this.editingId = null;
  }

  /** Persist an inline edit. An empty api_key leaves the stored key untouched. */
  submitEdit(): void {
    if (this.editingId === null) {
      return;
    }
    const form = this.editForm;
    if (!form.label.trim()) {
      this.providersError = 'Please enter a label for the provider.';
      return;
    }
    const body: LlmProviderUpdate = {
      label: form.label.trim(),
      provider: form.provider,
      model: form.model.trim(),
      base_url: form.provider === 'ollama' ? form.base_url.trim() : '',
      api_key: form.api_key.trim(),
    };
    this.persistProviders(this.api.updateProvider(this.editingId, body), 'Provider updated.', {
      onSuccess: () => {
        this.editingId = null;
      },
    });
  }

  /** Remove a provider from the list. */
  removeProvider(entry: LlmProviderEntry): void {
    this.persistProviders(this.api.deleteProvider(entry.id), 'Provider removed.');
  }

  /** Run a list-mutating call and surface success/error.
   *
   * The mutating calls apply the server's authoritative list on success
   * (`applyProviderList`); only reorder updates the local array optimistically, so on
   * error the caller's `revert` restores it. We deliberately do NOT reload on error —
   * a reload would clear/overwrite the just-set error message (its HTTP response races
   * the error), and add/edit/delete never touched the local list, so there is nothing
   * to resync.
   */
  private persistProviders(
    call: Observable<LlmProviderListResponse>,
    successMsg: string,
    opts?: { onSuccess?: () => void; revert?: () => void },
  ): void {
    this.providersError = null;
    this.providersSaving = true;
    this.success = null;
    call.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.applyProviderList(res);
        this.providersSaving = false;
        this.success = successMsg;
        opts?.onSuccess?.();
      },
      error: (err) => {
        this.providersSaving = false;
        opts?.revert?.();
        this.providersError = this.friendlyError(err, 'Failed to save the provider list.');
      },
    });
  }

  /** Human-readable reset estimate for a usage-limited provider (e.g. "~2h").
   *
   * ``nowMs`` defaults to the current time; tests pass a fixed value for
   * deterministic assertions (no private-field override needed).
   */
  resetInfo(entry: LlmProviderEntry, nowMs: number = Date.now()): string {
    if (!entry.limit_exceeded || !entry.reset_at) {
      return '';
    }
    const reset = new Date(entry.reset_at).getTime();
    if (Number.isNaN(reset)) {
      return '';
    }
    const ms = reset - nowMs;
    if (ms <= 0) {
      return 'resetting now';
    }
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) {
      // Under ~30s rounds to 0 minutes — avoid the confusing "resets in ~0m".
      return 'resets in <1m';
    }
    if (minutes < 60) {
      return `resets in ~${minutes}m`;
    }
    const hours = Math.round(minutes / 60);
    if (hours < 48) {
      return `resets in ~${hours}h`;
    }
    return `resets in ~${Math.round(hours / 24)}d`;
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
          // mark it unreachable (the API itself didn't answer) so the banner points
          // at connectivity and Save is disabled (storageAvailable derives from this).
          this.storageStatus = 'unreachable';
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
    // storageAvailable derives from storageStatus; older backends that omit
    // storage_status fall back from the storage_available boolean.
    this.storageStatus = cfg.storage_status ?? (cfg.storage_available ? 'available' : 'unconfigured');
    this.providerOptions = cfg.provider_options?.length ? cfg.provider_options : ['ollama', 'claude'];
    this.claudeModelOptions = cfg.claude_model_options || [];
    this.ollamaModelSuggestions = cfg.ollama_model_suggestions || [];
    // Inputs are write-only; never echo a key back.
    this.claudeApiKey = '';
    this.ollamaApiKey = '';
    // Refresh the live Ollama model list for the freshly-applied config (load or
    // post-save). No-op for Claude; gated for keyless Cloud.
    this.maybeLoadOllamaModels();
  }

  /** React to the Ollama Local/Cloud toggle by defaulting the base URL. */
  onOllamaModeChange(mode: OllamaMode): void {
    this.ollamaMode = mode;
    this.ollamaBaseUrl = mode === 'cloud' ? OLLAMA_CLOUD_URL : OLLAMA_LOCAL_DEFAULT;
    this.maybeLoadOllamaModels();
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
    // Switching to Ollama should surface its live models (or the keyless-Cloud
    // hint); switching to Claude clears any Ollama note.
    this.maybeLoadOllamaModels();
  }

  /** The model suggestions for the active provider. */
  get modelOptions(): string[] {
    return this.provider === 'claude' ? this.claudeModelOptions : this.ollamaModelSuggestions;
  }

  /** Decide whether to fetch live Ollama models for the current state, and do so.
   *
   * Local mode always fetches (no key needed). Cloud mode fetches only when a key
   * is already configured; otherwise it shows a hint to save the key first and
   * makes no request — the backend reads the key from the store, so an unsaved
   * key could not authenticate the listing anyway. A no-op for Claude.
   */
  private maybeLoadOllamaModels(): void {
    this.modelNote = null;
    if (this.provider !== 'ollama') {
      return;
    }
    if (this.ollamaMode === 'cloud' && !this.ollamaApiKeyConfigured) {
      this.modelNote = 'Save your Ollama Cloud API key to load available models.';
      return;
    }
    this.loadOllamaModels();
  }

  /** Fetch the live Ollama model list and populate the Model dropdown.
   *
   * On success with a non-empty list, replaces the dropdown suggestions with the
   * live models. A `fallback` source (endpoint unreachable) or a request error
   * leaves the current curated suggestions intact and surfaces a soft inline note
   * — never the blocking error banner — so the page never regresses.
   */
  private loadOllamaModels(): void {
    this.ollamaModelsLoading = true;
    this.api
      .getOllamaModels()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res: OllamaModelsResponse) => {
          if (res.models?.length) {
            this.ollamaModelSuggestions = res.models;
          }
          this.modelNote =
            res.source === 'fallback'
              ? "Couldn't reach the Ollama endpoint — showing default suggestions."
              : null;
          this.ollamaModelsLoading = false;
        },
        error: () => {
          // Keep the existing curated suggestions; soft note only.
          this.modelNote = "Couldn't load Ollama models — showing default suggestions.";
          this.ollamaModelsLoading = false;
        },
      });
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
        this.storageStatus === 'unreachable'
          ? 'Configuration storage is unreachable (Postgres is configured but not responding). Restore the database connection and try again.'
          : 'Configuration storage is unavailable (Postgres is not configured). Set the provider via environment variables instead.';
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
