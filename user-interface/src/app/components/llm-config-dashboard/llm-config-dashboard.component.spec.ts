import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { CdkDragDrop } from '@angular/cdk/drag-drop';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import { LlmConfigDashboardComponent } from './llm-config-dashboard.component';
import type {
  LlmConfigResponse,
  LlmProviderEntry,
  LlmProviderListResponse,
  OllamaModelsResponse,
} from '../../models/llm-config.model';

const BASE_CONFIG: LlmConfigResponse = {
  provider: 'ollama',
  model: 'deepseek-v4-pro:cloud',
  ollama_model: '',
  claude_model: '',
  ollama_base_url: 'https://ollama.com',
  claude_api_key_configured: false,
  ollama_api_key_configured: false,
  storage_available: true,
  storage_status: 'available',
  provider_options: ['ollama', 'claude'],
  claude_model_options: ['claude-opus-4-8'],
  ollama_model_suggestions: ['llama3.1'],
};

const FALLBACK_MODELS: OllamaModelsResponse = {
  models: ['llama3.1'],
  base_url: 'https://ollama.com',
  source: 'fallback',
};

function entry(over: Partial<LlmProviderEntry> = {}): LlmProviderEntry {
  return {
    id: 1,
    label: 'Anthropic',
    provider: 'claude',
    model: 'claude-opus-4-8',
    base_url: '',
    sort_order: 0,
    api_key_configured: true,
    limit_exceeded: false,
    limit_type: '',
    reset_at: null,
    ...over,
  };
}

function listResponse(providers: LlmProviderEntry[]): LlmProviderListResponse {
  return { providers, storage_available: true, storage_status: 'available' };
}

function dropEvent(previousIndex: number, currentIndex: number): CdkDragDrop<LlmProviderEntry[]> {
  return { previousIndex, currentIndex } as CdkDragDrop<LlmProviderEntry[]>;
}

describe('LlmConfigDashboardComponent', () => {
  let component: LlmConfigDashboardComponent;
  let fixture: ComponentFixture<LlmConfigDashboardComponent>;
  let apiSpy: {
    getConfig: ReturnType<typeof vi.fn>;
    updateConfig: ReturnType<typeof vi.fn>;
    getOllamaModels: ReturnType<typeof vi.fn>;
    listProviders: ReturnType<typeof vi.fn>;
    createProvider: ReturnType<typeof vi.fn>;
    updateProvider: ReturnType<typeof vi.fn>;
    deleteProvider: ReturnType<typeof vi.fn>;
    reorderProviders: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getConfig: vi.fn(),
      updateConfig: vi.fn(),
      getOllamaModels: vi.fn(),
      listProviders: vi.fn(),
      createProvider: vi.fn(),
      updateProvider: vi.fn(),
      deleteProvider: vi.fn(),
      reorderProviders: vi.fn(),
    };
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    apiSpy.getOllamaModels.mockReturnValue(of({ ...FALLBACK_MODELS }));
    apiSpy.listProviders.mockReturnValue(of(listResponse([])));

    await TestBed.configureTestingModule({
      imports: [LlmConfigDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: LlmConfigApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(LlmConfigDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create and load config on init', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getConfig).toHaveBeenCalled();
    expect(component.provider).toBe('ollama');
    expect(component.loading).toBe(false);
  });

  it('derives Ollama cloud mode from base URL', () => {
    expect(component.ollamaMode).toBe('cloud');
  });

  it('derives Ollama local mode from a non-cloud base URL', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }),
    );
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });

  it('sets error when load fails', () => {
    apiSpy.getConfig.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.loadConfig();
    expect(component.error).toBe('boom');
    expect(component.loading).toBe(false);
    // A failed load can't confirm the store is reachable, so Save is disabled and the
    // status points at connectivity (the API itself didn't answer).
    expect(component.storageAvailable).toBe(false);
    expect(component.storageStatus).toBe('unreachable');
  });

  it('reflects the unreachable storage status from the API', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unreachable' }),
    );
    component.loadConfig();
    expect(component.storageAvailable).toBe(false);
    expect(component.storageStatus).toBe('unreachable');
  });

  it('save guard message differs for unreachable vs unconfigured storage', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unreachable' }),
    );
    component.loadConfig();
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('unreachable');

    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unconfigured' }),
    );
    component.loadConfig();
    component.save();
    expect(component.error).toContain('not configured');
  });

  it('onOllamaModeChange defaults the base URL', () => {
    component.onOllamaModeChange('local');
    expect(component.ollamaBaseUrl).toContain('localhost');
    component.onOllamaModeChange('cloud');
    expect(component.ollamaBaseUrl).toContain('ollama.com');
  });

  it('modelOptions follow the selected provider', () => {
    component.provider = 'claude';
    expect(component.modelOptions).toEqual(['claude-opus-4-8']);
    component.provider = 'ollama';
    expect(component.modelOptions).toEqual(['llama3.1']);
  });

  it('saves claude config with the api key when provided', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    component.provider = 'claude';
    component.model = 'claude-opus-4-8';
    component.claudeApiKey = 'sk-secret';
    component.save();
    expect(apiSpy.updateConfig).toHaveBeenCalledWith(
      expect.objectContaining({ provider: 'claude', model: 'claude-opus-4-8', claude_api_key: 'sk-secret' }),
    );
    expect(component.success).toBeTruthy();
    expect(component.saving).toBe(false);
  });

  it('omits a blank api key on save (preserve existing)', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    component.provider = 'claude';
    component.claudeApiKey = '';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.claude_api_key).toBeUndefined();
  });

  it('sends ollama base URL only for ollama provider', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.provider = 'ollama';
    component.onOllamaModeChange('local');
    component.ollamaBaseUrl = 'http://localhost:11434';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.ollama_base_url).toBe('http://localhost:11434');
  });

  it('blocks save and reports when storage is unavailable', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unconfigured' }),
    );
    component.loadConfig();
    expect(component.storageAvailable).toBe(false);
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('storage');
  });

  it('sets error when save fails', () => {
    apiSpy.updateConfig.mockReturnValue(throwError(() => ({ error: { detail: 'save failed' } })));
    component.save();
    expect(component.error).toBe('save failed');
    expect(component.saving).toBe(false);
  });

  it('onProviderChange falls back to the first suggestion when no model is stored', () => {
    // BASE_CONFIG has empty per-provider models, so the switch uses the default.
    component.model = 'deepseek-v4-pro:cloud';
    component.onProviderChange('claude');
    expect(component.provider).toBe('claude');
    expect(component.model).toBe('claude-opus-4-8'); // first claude option
    component.onProviderChange('ollama');
    expect(component.model).toBe('llama3.1'); // first ollama suggestion
  });

  it('onProviderChange restores each provider\'s saved model (lossless switch)', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_model: 'qwen3-coder:480b-cloud', claude_model: 'claude-sonnet-4-6' }),
    );
    component.loadConfig();
    component.onProviderChange('claude');
    expect(component.model).toBe('claude-sonnet-4-6'); // saved, not the first suggestion
    component.onProviderChange('ollama');
    expect(component.model).toBe('qwen3-coder:480b-cloud'); // saved, not the first suggestion
  });

  it('does not send a stale cross-provider model after a provider switch', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    // Start on Ollama with an Ollama model, then switch to Claude via the handler.
    component.model = 'deepseek-v4-pro:cloud';
    component.onProviderChange('claude');
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.model).toBe('claude-opus-4-8'); // not the stale Ollama model
  });

  it('treats a custom host containing ollama.com as Local, not Cloud', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_base_url: 'http://ollama.company.com:11434' }),
    );
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });

  it('defaults a cleared Local base URL to localhost instead of sending empty', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.provider = 'ollama';
    component.ollamaMode = 'local';
    component.ollamaBaseUrl = '';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.ollama_base_url).toBe('http://localhost:11434');
  });

  it('clears a stale success banner when reloading', () => {
    component.success = 'previously saved';
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.loadConfig();
    expect(component.success).toBeNull();
  });

  it('blocks save with an error when the model is empty', () => {
    component.provider = 'claude';
    component.model = '   ';
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('model');
  });

  it('treats a scheme-less ollama.com base URL as Cloud', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'ollama.com' }));
    component.loadConfig();
    expect(component.ollamaMode).toBe('cloud');
  });

  it('treats a scheme-less custom host as Local', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'my-ollama.internal:11434' }));
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });

  it('does not fetch live models for keyless Ollama Cloud, shows a hint instead', () => {
    // BASE_CONFIG is Ollama + Cloud + no key configured.
    expect(apiSpy.getOllamaModels).not.toHaveBeenCalled();
    expect(component.modelNote).toContain('Save your Ollama Cloud API key');
  });

  it('fetches and populates live models for Local Ollama on load', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }));
    apiSpy.getOllamaModels.mockReturnValue(
      of({ models: ['llama3.2', 'mistral'], base_url: 'http://localhost:11434', source: 'live' }),
    );
    component.loadConfig();
    expect(apiSpy.getOllamaModels).toHaveBeenCalled();
    expect(component.ollamaModelSuggestions).toEqual(['llama3.2', 'mistral']);
    expect(component.modelNote).toBeNull();
    expect(component.ollamaModelsLoading).toBe(false);
  });

  it('fetches live models for Cloud once a key is configured', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_api_key_configured: true }));
    apiSpy.getOllamaModels.mockReturnValue(
      of({ models: ['cloud-a', 'cloud-b'], base_url: 'https://ollama.com', source: 'live' }),
    );
    component.loadConfig();
    expect(apiSpy.getOllamaModels).toHaveBeenCalled();
    expect(component.ollamaModelSuggestions).toEqual(['cloud-a', 'cloud-b']);
  });

  it('keeps curated suggestions and notes when the fetch falls back', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }));
    apiSpy.getOllamaModels.mockReturnValue(of({ ...FALLBACK_MODELS, source: 'fallback' }));
    component.loadConfig();
    expect(component.ollamaModelSuggestions).toEqual(['llama3.1']); // unchanged curated list
    expect(component.modelNote).toContain('default suggestions');
  });

  it('keeps suggestions and notes softly (no error banner) when the fetch errors', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }));
    apiSpy.getOllamaModels.mockReturnValue(throwError(() => ({ error: { detail: 'down' } })));
    component.loadConfig();
    expect(component.modelNote).toContain("Couldn't load Ollama models");
    expect(component.error).toBeNull(); // soft note, not the blocking banner
    expect(component.ollamaModelsLoading).toBe(false);
  });

  it('fetches live models when switching to Local mode', () => {
    apiSpy.getOllamaModels.mockClear();
    component.onOllamaModeChange('local');
    expect(apiSpy.getOllamaModels).toHaveBeenCalled();
  });

  it('clears the Ollama note when switching to Claude', () => {
    expect(component.modelNote).toContain('Save your Ollama Cloud API key'); // keyless cloud hint
    apiSpy.getOllamaModels.mockClear();
    component.onProviderChange('claude');
    expect(component.modelNote).toBeNull();
    expect(apiSpy.getOllamaModels).not.toHaveBeenCalled();
  });

  it('refetches live models after a successful save', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }));
    apiSpy.getOllamaModels.mockClear();
    apiSpy.getOllamaModels.mockReturnValue(
      of({ models: ['saved-model'], base_url: 'http://localhost:11434', source: 'live' }),
    );
    component.provider = 'ollama';
    component.onOllamaModeChange('local');
    component.model = 'llama3.1';
    component.save();
    // applyConfig runs in the save success handler and refreshes the model list.
    expect(apiSpy.getOllamaModels).toHaveBeenCalled();
    expect(component.ollamaModelSuggestions).toEqual(['saved-model']);
  });

  // --- Multi-provider fallback list ---------------------------------------

  it('loads the provider list on init', () => {
    expect(apiSpy.listProviders).toHaveBeenCalled();
    expect(component.providers).toEqual([]);
  });

  it('sorts the loaded list by sort_order defensively', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 2, sort_order: 1 }), entry({ id: 1, sort_order: 0 })])),
    );
    component.loadProviders();
    expect(component.providers.map((p) => p.id)).toEqual([1, 2]);
  });

  it('sets providersError when the list load fails', () => {
    apiSpy.listProviders.mockReturnValue(throwError(() => ({ error: { detail: 'list down' } })));
    component.loadProviders();
    expect(component.providersError).toBe('list down');
    expect(component.providersLoading).toBe(false);
  });

  it('adds a provider via the add form', () => {
    apiSpy.createProvider.mockReturnValue(of(listResponse([entry({ id: 5, label: 'New' })])));
    component.startAdd();
    expect(component.addForm).not.toBeNull();
    component.addForm!.label = 'New';
    component.addForm!.provider = 'claude';
    component.addForm!.api_key = 'sk';
    component.submitAdd();
    expect(apiSpy.createProvider).toHaveBeenCalledWith(
      expect.objectContaining({ label: 'New', provider: 'claude', api_key: 'sk' }),
    );
    expect(component.addForm).toBeNull(); // closed on success
    expect(component.providers.map((p) => p.id)).toEqual([5]);
    expect(component.success).toBeTruthy();
  });

  it('omits the ollama base_url for a claude add', () => {
    apiSpy.createProvider.mockReturnValue(of(listResponse([])));
    component.startAdd();
    component.addForm!.label = 'C';
    component.addForm!.provider = 'claude';
    component.addForm!.base_url = 'http://should-be-dropped';
    component.submitAdd();
    expect(apiSpy.createProvider.mock.calls[0][0].base_url).toBe('');
  });

  it('rejects an add with a blank label', () => {
    component.startAdd();
    component.addForm!.label = '   ';
    component.submitAdd();
    expect(apiSpy.createProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('label');
  });

  it('edits a provider without resending the key when left blank', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.loadProviders();
    apiSpy.updateProvider.mockReturnValue(of(listResponse([entry({ id: 3, label: 'Renamed' })])));
    component.startEdit(component.providers[0]);
    expect(component.editForm.api_key).toBe(''); // key never pre-filled
    component.editForm.label = 'Renamed';
    component.submitEdit();
    expect(apiSpy.updateProvider).toHaveBeenCalledWith(3, expect.objectContaining({ label: 'Renamed', api_key: '' }));
    expect(component.editingId).toBeNull();
  });

  it('sends clear_api_key when the operator clears the stored key', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.loadProviders();
    apiSpy.updateProvider.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.startEdit(component.providers[0]);
    component.editForm.clear_api_key = true;
    component.submitEdit();
    expect(apiSpy.updateProvider).toHaveBeenCalledWith(3, expect.objectContaining({ clear_api_key: true }));
  });

  it('lets a typed key win over the clear flag (clear_api_key false)', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.loadProviders();
    apiSpy.updateProvider.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.startEdit(component.providers[0]);
    component.editForm.clear_api_key = true;
    component.editForm.api_key = 'sk-new';
    component.submitEdit();
    expect(apiSpy.updateProvider).toHaveBeenCalledWith(
      3,
      expect.objectContaining({ api_key: 'sk-new', clear_api_key: false }),
    );
  });

  it('removes a provider', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 7 })])));
    component.loadProviders();
    apiSpy.deleteProvider.mockReturnValue(of(listResponse([])));
    component.removeProvider(component.providers[0]);
    expect(apiSpy.deleteProvider).toHaveBeenCalledWith(7);
    expect(component.providers).toEqual([]);
  });

  it('persists a new order on drag-drop', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 1, sort_order: 0 }), entry({ id: 2, sort_order: 1 })])),
    );
    component.loadProviders();
    apiSpy.reorderProviders.mockReturnValue(of(listResponse([entry({ id: 2 }), entry({ id: 1 })])));
    component.onProviderDrop(dropEvent(0, 1));
    expect(apiSpy.reorderProviders).toHaveBeenCalledWith([2, 1]);
  });

  it('ignores a no-op drag-drop', () => {
    component.onProviderDrop(dropEvent(1, 1));
    expect(apiSpy.reorderProviders).not.toHaveBeenCalled();
  });

  it('ignores a drag-drop while a save is already in flight (no concurrent reorder)', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 1, sort_order: 0 }), entry({ id: 2, sort_order: 1 })])),
    );
    component.loadProviders();
    component.providersSaving = true; // a previous mutation is still pending
    component.onProviderDrop(dropEvent(0, 1));
    expect(apiSpy.reorderProviders).not.toHaveBeenCalled();
    expect(component.providers.map((p) => p.id)).toEqual([1, 2]); // order unchanged
  });

  it('reports a mutation failure without reloading (no race)', () => {
    apiSpy.createProvider.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
    apiSpy.listProviders.mockClear();
    component.startAdd();
    component.addForm!.label = 'X';
    component.submitAdd();
    expect(component.providersError).toBe('nope');
    // No resync reload on error — that would race and clear the just-set error.
    expect(apiSpy.listProviders).not.toHaveBeenCalled();
  });

  it('reverts the optimistic reorder and keeps the error on deferred failure', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 1, sort_order: 0 }), entry({ id: 2, sort_order: 1 })])),
    );
    component.loadProviders();
    apiSpy.listProviders.mockClear();
    // A Subject lets the reorder fail LATER (after the optimistic move), proving the
    // error survives and the order reverts — the race the old reload-on-error had.
    const result$ = new Subject<LlmProviderListResponse>();
    apiSpy.reorderProviders.mockReturnValue(result$);
    component.onProviderDrop(dropEvent(0, 1));
    expect(component.providers.map((p) => p.id)).toEqual([2, 1]); // optimistic move applied
    result$.error({ error: { detail: 'reorder failed' } });
    expect(component.providersError).toBe('reorder failed');
    expect(component.providers.map((p) => p.id)).toEqual([1, 2]); // reverted
    expect(apiSpy.listProviders).not.toHaveBeenCalled(); // no resync reload
  });

  it('formats the reset estimate for a limited provider', () => {
    const base = Date.UTC(2026, 5, 30, 12, 0, 0);
    const at = (ms: number) => new Date(base + ms).toISOString();
    // nowMs is passed explicitly for deterministic assertions (no field override).
    expect(
      component.resetInfo(entry({ limit_exceeded: true, reset_at: at(2 * 3600 * 1000) }), base),
    ).toBe('resets in ~2h');
    expect(
      component.resetInfo(entry({ limit_exceeded: true, reset_at: at(30 * 60 * 1000) }), base),
    ).toBe('resets in ~30m');
    expect(
      component.resetInfo(entry({ limit_exceeded: true, reset_at: at(3 * 24 * 3600 * 1000) }), base),
    ).toBe('resets in ~3d');
  });

  it('shows "<1m" instead of "~0m" when under 30 seconds remain', () => {
    const base = Date.UTC(2026, 5, 30, 12, 0, 0);
    const at = (ms: number) => new Date(base + ms).toISOString();
    expect(component.resetInfo(entry({ limit_exceeded: true, reset_at: at(20 * 1000) }), base)).toBe(
      'resets in <1m',
    );
  });

  it('returns no reset estimate when not limited or no reset_at', () => {
    expect(component.resetInfo(entry({ limit_exceeded: false }))).toBe('');
    expect(component.resetInfo(entry({ limit_exceeded: true, reset_at: null }))).toBe('');
    expect(component.resetInfo(entry({ limit_exceeded: true, reset_at: 'now' }))).toBe('');
  });

  it('reports a past reset_at as resetting now', () => {
    const base = Date.UTC(2026, 5, 30, 12, 0, 0);
    const past = new Date(base - 1000).toISOString();
    expect(component.resetInfo(entry({ limit_exceeded: true, reset_at: past }), base)).toBe(
      'resetting now',
    );
  });

  it('startAdd closes any open edit and startEdit closes the add form', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 9 })])));
    component.loadProviders();
    component.startAdd();
    component.startEdit(component.providers[0]);
    expect(component.addForm).toBeNull();
    expect(component.editingId).toBe(9);
    component.startAdd();
    expect(component.editingId).toBeNull();
    expect(component.addForm).not.toBeNull();
    component.cancelAdd();
    expect(component.addForm).toBeNull();
    component.startEdit(component.providers[0]);
    component.cancelEdit();
    expect(component.editingId).toBeNull();
  });
});
