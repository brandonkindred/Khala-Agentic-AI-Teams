import { TestBed } from '@angular/core/testing';
import { Router, RouterStateSnapshot, ActivatedRouteSnapshot } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { Observable, isObservable, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { LlmConfigApiService } from '../services/llm-config-api.service';
import { LlmProviderListResponse } from '../models/llm-config.model';
import { LlmSetupState, llmConfiguredGuard } from './llm-configured.guard';

function listResponse(
  providerCount: number,
  status: LlmProviderListResponse['storage_status'] = 'available',
): LlmProviderListResponse {
  const providers = Array.from({ length: providerCount }, (_v, i) => ({
    id: i + 1,
    label: 'e',
    provider: 'ollama' as const,
    model: '',
    base_url: '',
    sort_order: i,
    api_key_configured: false,
    limit_exceeded: false,
    limit_type: '',
    reset_at: null,
  }));
  return { providers, storage_available: status === 'available', storage_status: status };
}

describe('llmConfiguredGuard', () => {
  let apiSpy: { listProviders: ReturnType<typeof vi.fn> };
  let dialogSpy: { open: ReturnType<typeof vi.fn> };
  let routerSpy: { navigateByUrl: ReturnType<typeof vi.fn> };
  let afterClosed$: Observable<boolean>;

  beforeEach(() => {
    afterClosed$ = of(false);
    apiSpy = { listProviders: vi.fn() };
    dialogSpy = { open: vi.fn(() => ({ afterClosed: () => afterClosed$ })) };
    routerSpy = { navigateByUrl: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        { provide: LlmConfigApiService, useValue: apiSpy },
        { provide: MatDialog, useValue: dialogSpy },
        { provide: Router, useValue: routerSpy },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  function run(url: string): Observable<boolean> | boolean {
    const state = { url } as RouterStateSnapshot;
    return TestBed.runInInjectionContext(() =>
      llmConfiguredGuard({} as ActivatedRouteSnapshot, state),
    );
  }

  function resolve(result: Observable<boolean> | boolean): boolean {
    if (isObservable(result)) {
      let out = false;
      result.subscribe((v) => (out = v as boolean));
      return out;
    }
    return result;
  }

  it('allows and does not query when the target is the setup page', () => {
    const out = resolve(run('/llm-config'));
    expect(out).toBe(true);
    expect(apiSpy.listProviders).not.toHaveBeenCalled();
    expect(dialogSpy.open).not.toHaveBeenCalled();
  });

  it('allows without a dialog when a provider is configured, and stops re-querying', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse(1)));
    expect(resolve(run('/dashboard'))).toBe(true);
    expect(dialogSpy.open).not.toHaveBeenCalled();
    // Configured is cached: a second navigation does not re-query.
    apiSpy.listProviders.mockClear();
    expect(resolve(run('/blogging'))).toBe(true);
    expect(apiSpy.listProviders).not.toHaveBeenCalled();
  });

  it('opens the dialog and allows navigation when no providers are configured', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse(0)));
    const out = resolve(run('/dashboard'));
    expect(out).toBe(true); // never blocks
    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    const data = dialogSpy.open.mock.calls[0][1].data;
    expect(data.confirmLabel).toBe('Setup LLM');
  });

  it('routes to /llm-config when the operator clicks Setup LLM', () => {
    afterClosed$ = of(true); // "Setup LLM"
    apiSpy.listProviders.mockReturnValue(of(listResponse(0)));
    resolve(run('/dashboard'));
    expect(routerSpy.navigateByUrl).toHaveBeenCalledWith('/llm-config');
  });

  it('does not route when the dialog is dismissed', () => {
    afterClosed$ = of(false); // "Dismiss"
    apiSpy.listProviders.mockReturnValue(of(listResponse(0)));
    resolve(run('/dashboard'));
    expect(routerSpy.navigateByUrl).not.toHaveBeenCalled();
  });

  it('shows the dialog at most once per session', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse(0)));
    resolve(run('/dashboard'));
    apiSpy.listProviders.mockClear();
    resolve(run('/blogging'));
    expect(dialogSpy.open).toHaveBeenCalledTimes(1); // not re-opened
    expect(apiSpy.listProviders).not.toHaveBeenCalled(); // not re-queried
  });

  it('fails open (allows, no dialog) when the provider list read errors', () => {
    apiSpy.listProviders.mockReturnValue(throwError(() => ({ status: 503 })));
    const out = resolve(run('/dashboard'));
    expect(out).toBe(true);
    expect(dialogSpy.open).not.toHaveBeenCalled();
  });

  it('does not prompt when the store is transiently unreachable (empty list, 200)', () => {
    // Backend degrades a Postgres blip to 200 {providers:[], storage_status:'unreachable'}.
    apiSpy.listProviders.mockReturnValue(of(listResponse(0, 'unreachable')));
    const out = resolve(run('/dashboard'));
    expect(out).toBe(true);
    expect(dialogSpy.open).not.toHaveBeenCalled();
    // Not cached: a later navigation re-checks once the store recovers.
    apiSpy.listProviders.mockClear();
    apiSpy.listProviders.mockReturnValue(of(listResponse(0, 'available')));
    resolve(run('/blogging'));
    expect(apiSpy.listProviders).toHaveBeenCalled();
    expect(dialogSpy.open).toHaveBeenCalledTimes(1); // now genuinely empty → prompt
  });

  it('does not prompt when the store is unconfigured (empty list, 200)', () => {
    apiSpy.listProviders.mockReturnValue(of(listResponse(0, 'unconfigured')));
    resolve(run('/dashboard'));
    expect(dialogSpy.open).not.toHaveBeenCalled();
  });

  it('respects a pre-set configured flag (no query)', () => {
    TestBed.inject(LlmSetupState).configured = true;
    expect(resolve(run('/dashboard'))).toBe(true);
    expect(apiSpy.listProviders).not.toHaveBeenCalled();
  });
});
