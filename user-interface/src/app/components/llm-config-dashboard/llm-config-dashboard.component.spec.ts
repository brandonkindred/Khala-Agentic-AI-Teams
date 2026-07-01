import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { CdkDragDrop } from '@angular/cdk/drag-drop';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
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

  beforeEach(async () => {
    apiSpy = {
      listProviders: vi.fn(),
      createProvider: vi.fn(),
      updateProvider: vi.fn(),
      deleteProvider: vi.fn(),
      reorderProviders: vi.fn(),
    };
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
