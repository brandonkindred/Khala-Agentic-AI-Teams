import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { CdkDragDrop } from '@angular/cdk/drag-drop';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { vi } from 'vitest';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import { LlmConfigDashboardComponent } from './llm-config-dashboard.component';
import type { LlmProviderEntry, LlmProviderListResponse } from '../../models/llm-config.model';

function entry(over: Partial<LlmProviderEntry> = {}): LlmProviderEntry {
  return {
    id: 1,
    label: 'Anthropic',
    provider: 'claude',
    model: 'claude-opus-4-8',
    base_url: '',
    sort_order: 0,
    endpoint_id: '',
    api_key_configured: true,
    limit_exceeded: false,
    limit_type: '',
    reset_at: null,
    ...over,
  };
}

function listResponse(
  providers: LlmProviderEntry[],
  over: Partial<LlmProviderListResponse> = {},
): LlmProviderListResponse {
  return { providers, storage_available: true, storage_status: 'available', ...over };
}

function dropEvent(previousIndex: number, currentIndex: number): CdkDragDrop<LlmProviderEntry[]> {
  return { previousIndex, currentIndex } as CdkDragDrop<LlmProviderEntry[]>;
}

describe('LlmConfigDashboardComponent', () => {
  let component: LlmConfigDashboardComponent;
  let fixture: ComponentFixture<LlmConfigDashboardComponent>;
  let apiSpy: {
    listProviders: ReturnType<typeof vi.fn>;
    createProvider: ReturnType<typeof vi.fn>;
    updateProvider: ReturnType<typeof vi.fn>;
    deleteProvider: ReturnType<typeof vi.fn>;
    reorderProviders: ReturnType<typeof vi.fn>;
  };
  let snackBar: { open: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      listProviders: vi.fn(),
      createProvider: vi.fn(),
      updateProvider: vi.fn(),
      deleteProvider: vi.fn(),
      reorderProviders: vi.fn(),
    };
    apiSpy.listProviders.mockReturnValue(of(listResponse([])));
    snackBar = { open: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [LlmConfigDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: LlmConfigApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackBar },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LlmConfigDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('loads only the provider list on init (no single-provider config call)', () => {
    expect(apiSpy.listProviders).toHaveBeenCalled();
    expect(component.providers).toEqual([]);
    // The single-provider GET /api/llm-config surface was removed entirely.
    expect('getConfig' in apiSpy).toBe(false);
  });

  it('renders no single-provider config controls (form removed)', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('mat-radio-group')).toBeNull();
    expect(el.querySelector('[name="claudeApiKey"]')).toBeNull();
    expect(el.querySelector('[name="ollamaApiKey"]')).toBeNull();
    expect(el.querySelector('[name="ollamaBaseUrl"]')).toBeNull();
    // The providers card is present.
    expect(el.querySelector('.providers-card')).not.toBeNull();
  });

  it('applies storage status from the provider list response', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([], { storage_available: false, storage_status: 'unreachable' })),
    );
    component.loadProviders();
    expect(component.storageStatus).toBe('unreachable');
    expect(component.storageAvailable).toBe(false);
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

  it('shows the Endpoint ID field and hides Base URL for a RunPod add', () => {
    component.startAdd();
    fixture.detectChanges();
    let el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[name="addEndpointId"]')).toBeNull();
    expect(el.querySelector('[name="addBaseUrl"]')).not.toBeNull();

    component.addForm!.provider = 'runpod';
    fixture.detectChanges();
    el = fixture.nativeElement;
    expect(el.querySelector('[name="addEndpointId"]')).not.toBeNull();
    expect(el.querySelector('[name="addBaseUrl"]')).toBeNull();
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
    // Transient confirmation via snackbar (not a persistent banner).
    expect(snackBar.open).toHaveBeenCalledWith('Provider added.', 'Dismiss', { duration: 3000 });
  });

  it('omits the ollama base_url for a claude add', () => {
    apiSpy.createProvider.mockReturnValue(of(listResponse([])));
    component.startAdd();
    component.addForm!.label = 'C';
    component.addForm!.provider = 'claude';
    component.addForm!.api_key = 'sk';
    component.addForm!.base_url = 'http://should-be-dropped';
    component.submitAdd();
    expect(apiSpy.createProvider.mock.calls[0][0].base_url).toBe('');
  });

  it('blocks a Claude add with no API key (required field)', () => {
    component.startAdd();
    component.addForm!.label = 'Anthropic';
    component.addForm!.provider = 'claude';
    component.addForm!.api_key = '   ';
    component.submitAdd();
    expect(apiSpy.createProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('API key is required');
  });

  it('adds a RunPod provider with endpoint_id and api_key, sending an empty base_url', () => {
    apiSpy.createProvider.mockReturnValue(of(listResponse([entry({ id: 6, provider: 'runpod' })])));
    component.startAdd();
    component.addForm!.label = 'RunPod';
    component.addForm!.provider = 'runpod';
    component.addForm!.api_key = 'sk-runpod';
    component.addForm!.endpoint_id = 'abc123';
    component.addForm!.base_url = 'http://should-be-dropped';
    component.submitAdd();
    expect(apiSpy.createProvider).toHaveBeenCalledWith(
      expect.objectContaining({ provider: 'runpod', api_key: 'sk-runpod', endpoint_id: 'abc123', base_url: '' }),
    );
  });

  it('blocks a RunPod add with no endpoint_id (required field)', () => {
    component.startAdd();
    component.addForm!.label = 'RunPod';
    component.addForm!.provider = 'runpod';
    component.addForm!.api_key = 'sk-runpod';
    component.addForm!.endpoint_id = '   ';
    component.submitAdd();
    expect(apiSpy.createProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('endpoint ID is required');
  });

  it('blocks a RunPod add with no API key (required field, mirrors Claude)', () => {
    component.startAdd();
    component.addForm!.label = 'RunPod';
    component.addForm!.provider = 'runpod';
    component.addForm!.endpoint_id = 'abc123';
    component.addForm!.api_key = '   ';
    component.submitAdd();
    expect(apiSpy.createProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('API key is required');
  });

  it('allows an ollama add with no API key (key not required)', () => {
    apiSpy.createProvider.mockReturnValue(of(listResponse([])));
    component.startAdd();
    component.addForm!.label = 'Local';
    component.addForm!.provider = 'ollama';
    component.submitAdd();
    expect(apiSpy.createProvider).toHaveBeenCalled();
  });

  it('reports unsaved changes when an add form has typed content', () => {
    expect(component.hasUnsavedChanges()).toBe(false);
    component.startAdd();
    expect(component.hasUnsavedChanges()).toBe(false); // pristine defaults
    component.addForm!.api_key = 'sk-secret';
    expect(component.hasUnsavedChanges()).toBe(true);
    component.cancelAdd();
    expect(component.hasUnsavedChanges()).toBe(false);
  });

  it('reports unsaved changes when the add form switches provider off the default', () => {
    component.startAdd();
    expect(component.hasUnsavedChanges()).toBe(false); // defaults to ollama
    component.addForm!.provider = 'claude';
    expect(component.hasUnsavedChanges()).toBe(true);
  });

  it('reports unsaved changes when the add form has a typed endpoint_id', () => {
    component.startAdd();
    component.addForm!.provider = 'runpod';
    expect(component.hasUnsavedChanges()).toBe(true); // provider switch alone already counts
    component.cancelAdd();
    component.startAdd();
    component.addForm!.endpoint_id = 'abc123';
    expect(component.hasUnsavedChanges()).toBe(true);
  });

  it('does not report unsaved changes for a pristine RunPod edit form', () => {
    apiSpy.listProviders.mockReturnValue(
      of(
        listResponse([
          entry({
            id: 3,
            provider: 'runpod',
            base_url: 'https://api.runpod.ai/v2/abc123/openai/v1',
            endpoint_id: 'abc123',
          }),
        ]),
      ),
    );
    component.loadProviders();
    component.startEdit(component.providers[0]);
    expect(component.hasUnsavedChanges()).toBe(false);
    component.editForm.endpoint_id = 'different';
    expect(component.hasUnsavedChanges()).toBe(true);
  });

  it('reports unsaved changes when an edit changes a field or types a key', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 3, label: 'Orig' })])));
    component.loadProviders();
    component.startEdit(component.providers[0]);
    expect(component.hasUnsavedChanges()).toBe(false); // matches the entry
    component.editForm.label = 'Changed';
    expect(component.hasUnsavedChanges()).toBe(true);
    component.cancelEdit();
    expect(component.hasUnsavedChanges()).toBe(false);
  });

  it('blocks an edit that switches to Claude with no key (none stored, not clearing)', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 3, provider: 'ollama', api_key_configured: false })])),
    );
    component.loadProviders();
    component.startEdit(component.providers[0]);
    component.editForm.provider = 'claude';
    component.editForm.api_key = '';
    component.submitEdit();
    expect(apiSpy.updateProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('API key is required');
  });

  it('allows an edit that keeps an already-configured Claude key (blank field)', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 3, provider: 'claude', api_key_configured: true })])),
    );
    component.loadProviders();
    apiSpy.updateProvider.mockReturnValue(of(listResponse([entry({ id: 3 })])));
    component.startEdit(component.providers[0]);
    component.editForm.label = 'Renamed';
    component.submitEdit();
    expect(apiSpy.updateProvider).toHaveBeenCalled();
  });

  it('pre-fills the endpoint ID when editing an existing RunPod entry', () => {
    apiSpy.listProviders.mockReturnValue(
      of(
        listResponse([
          entry({
            id: 3,
            provider: 'runpod',
            base_url: 'https://api.runpod.ai/v2/abc123/openai/v1',
            endpoint_id: 'abc123',
            api_key_configured: true,
          }),
        ]),
      ),
    );
    component.loadProviders();
    component.startEdit(component.providers[0]);
    expect(component.editForm.endpoint_id).toBe('abc123');
  });

  it('allows an edit that keeps an existing RunPod entry with endpoint_id left blank', () => {
    apiSpy.listProviders.mockReturnValue(
      of(
        listResponse([
          entry({
            id: 3,
            provider: 'runpod',
            base_url: 'https://api.runpod.ai/v2/abc123/openai/v1',
            endpoint_id: 'abc123',
            api_key_configured: true,
          }),
        ]),
      ),
    );
    component.loadProviders();
    apiSpy.updateProvider.mockReturnValue(of(listResponse([entry({ id: 3, provider: 'runpod' })])));
    component.startEdit(component.providers[0]);
    expect(component.editForm.endpoint_id).toBe('abc123'); // pre-filled from entry.endpoint_id
    component.editForm.label = 'Renamed';
    component.editForm.endpoint_id = ''; // operator clears it without retyping
    component.submitEdit();
    expect(apiSpy.updateProvider).toHaveBeenCalledWith(3, expect.objectContaining({ endpoint_id: '' }));
  });

  it('blocks switching an existing entry to RunPod with no endpoint_id typed', () => {
    apiSpy.listProviders.mockReturnValue(
      of(listResponse([entry({ id: 3, provider: 'ollama', api_key_configured: false })])),
    );
    component.loadProviders();
    component.startEdit(component.providers[0]);
    component.editForm.provider = 'runpod';
    component.editForm.api_key = 'sk-runpod';
    component.editForm.endpoint_id = '';
    component.submitEdit();
    expect(apiSpy.updateProvider).not.toHaveBeenCalled();
    expect(component.providersError).toContain('endpoint ID is required');
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

  it('clears a stale providersError when opening or dismissing a form', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 9 })])));
    component.loadProviders();

    component.providersError = 'stale error';
    component.startAdd();
    expect(component.providersError).toBeNull();

    component.providersError = 'stale error';
    component.cancelAdd();
    expect(component.providersError).toBeNull();

    component.providersError = 'stale error';
    component.startEdit(component.providers[0]);
    expect(component.providersError).toBeNull();

    component.providersError = 'stale error';
    component.cancelEdit();
    expect(component.providersError).toBeNull();
  });

  it('ignores startAdd/startEdit while a save is already in flight (no cross-entity context switch)', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 1 }), entry({ id: 2 })])));
    component.loadProviders();
    component.providersSaving = true; // a previous mutation is still pending
    component.providersError = 'entry 1 failed to save';

    component.startAdd();
    expect(component.addForm).toBeNull(); // form did not open
    expect(component.providersError).toBe('entry 1 failed to save'); // not cleared

    component.startEdit(component.providers[1]);
    expect(component.editingId).toBeNull(); // did not switch to entry 2
    expect(component.providersError).toBe('entry 1 failed to save'); // not cleared
  });

  it('refreshes `now` every 30s while a provider is limited, so its badge stays live', () => {
    // The tick is gated on a limited provider existing (see next test) — set one up
    // before the fixture's ngOnInit runs.
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 1, limit_exceeded: true })])));
    vi.useFakeTimers();
    try {
      // Re-create the component under fake timers so its ngOnInit interval is captured.
      fixture = TestBed.createComponent(LlmConfigDashboardComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();
      const before = component.now;
      vi.advanceTimersByTime(30_000);
      expect(component.now).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('skips the tick when no provider is currently limited (avoids a needless CD pass)', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 1, limit_exceeded: false })])));
    vi.useFakeTimers();
    try {
      fixture = TestBed.createComponent(LlmConfigDashboardComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();
      const before = component.now;
      vi.advanceTimersByTime(30_000);
      expect(component.now).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops the reset-time timer on destroy', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse([entry({ id: 1, limit_exceeded: true })])));
    vi.useFakeTimers();
    try {
      fixture = TestBed.createComponent(LlmConfigDashboardComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();
      fixture.destroy();
      const before = component.now;
      vi.advanceTimersByTime(60_000);
      expect(component.now).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });
});
