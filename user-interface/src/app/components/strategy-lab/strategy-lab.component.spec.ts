import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NEVER, Subject, of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { NotificationService } from '../../core/notification.service';
import { StrategyLabRunService } from '../../services/strategy-lab-run.service';
import { StrategyLabComponent } from './strategy-lab.component';
import { createRunServiceStub, type RunServiceStub } from '../../testing/strategy-lab-run-service.stub';
import type {
  PaperTradingSession,
  RunStrategyLabRequest,
  StrategyLabRecord,
  StrategyLabRunStartResponse,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
} from '../../models';

/**
 * Focused coverage for the asset-category selection feature. The component is
 * instantiated without `detectChanges()` so `ngOnInit` (and its data-loading
 * API calls) never fire — we exercise the category state and `runNewStrategy`
 * payload directly.
 */
describe('StrategyLabComponent — asset categories', () => {
  let component: StrategyLabComponent;
  let fixture: ComponentFixture<StrategyLabComponent>;
  let runService: RunServiceStub;
  let apiSpy: {
    runStrategyLab: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getStrategyLabConfig: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    getActiveRuns: ReturnType<typeof vi.fn>;
  };
  let integrationsSpy: { getTradingViewConfig: ReturnType<typeof vi.fn> };

  const startResponse: StrategyLabRunStartResponse = {
    run_id: 'run-1',
    status: 'running',
    total_cycles: 10,
    message: 'started',
  };

  beforeEach(async () => {
    apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(of(startResponse)),
      // NEVER: emits nothing and never completes, so the stream-complete cascade
      // (loadResults / loadPaperTradingResults) stays out of these focused tests.
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      // ngOnInit also loads results / paper-trading / active-runs; safe empties.
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
    };
    integrationsSpy = {
      getTradingViewConfig: vi.fn().mockReturnValue(
        of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
      ),
    };
    runService = createRunServiceStub();

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    })
      // Component injects MatDialog (Material provides it at component level);
      // override so construction resolves without opening real dialogs.
      .overrideProvider(MatDialog, {
        useValue: { open: vi.fn().mockReturnValue({ afterClosed: () => of(true) }) },
      })
      // StrategyLabRunService is provided at the component level (see its
      // `providers` array); override that provider with the test double.
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: runService }] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(StrategyLabComponent);
    component = fixture.componentInstance;
  });

  it('defaults to every category selected', () => {
    expect(component.selectedCategories()).toEqual([
      'stocks',
      'crypto',
      'forex',
      'futures',
      'commodities',
    ]);
    expect(component.categoriesValid).toBe(true);
  });

  it('categoriesValid is false when no category is selected', () => {
    component.selectedCategories.set([]);
    expect(component.categoriesValid).toBe(false);
  });

  it('sends the selected categories in canonical order on the run request', () => {
    // Select out of canonical order; the payload must be reordered canonically.
    component.selectedCategories.set(['forex', 'stocks']);

    component.runNewStrategy();

    expect(apiSpy.runStrategyLab).toHaveBeenCalledTimes(1);
    const payload = apiSpy.runStrategyLab.mock.calls[0][0] as RunStrategyLabRequest;
    expect(payload.allowed_asset_classes).toEqual(['stocks', 'forex']);
    expect(component.running()).toBe(true);
    expect(runService.startRun).toHaveBeenCalledWith('run-1', expect.objectContaining({ run_id: 'run-1' }));
  });

  it('omits allowed_asset_classes when every category is selected', () => {
    // All selected == "no constraint": the field is omitted rather than sending
    // the full list (functionally equivalent server-side, smaller payload).
    component.runNewStrategy();

    const payload = apiSpy.runStrategyLab.mock.calls[0][0] as RunStrategyLabRequest;
    expect(payload.allowed_asset_classes).toBeUndefined();
  });

  it('does not start a run when no category is selected', () => {
    component.selectedCategories.set([]);

    component.runNewStrategy();

    expect(apiSpy.runStrategyLab).not.toHaveBeenCalled();
    expect(component.running()).toBe(false);
    expect(component.error()).toContain('at least one asset category');
  });

  it('ignores a re-entrant call while a run is already in progress', () => {
    runService.running.set(true);

    component.runNewStrategy();

    expect(apiSpy.runStrategyLab).not.toHaveBeenCalled();
  });

  // The config tests drive the behavior through the public `ngOnInit` (config is
  // applied synchronously via `of(...)`), then tear down to cancel the
  // active-run polling timer `ngOnInit` schedules — no private-method access.
  const initAndDestroy = (): void => {
    component.ngOnInit();
    component.ngOnDestroy();
  };

  // ---------------------------------------------------------------------
  // TradingView data-source notice
  // ---------------------------------------------------------------------

  it('flags the TradingView notice as visible when the source is not configured', () => {
    initAndDestroy();
    expect(integrationsSpy.getTradingViewConfig).toHaveBeenCalled();
    expect(component.tradingViewStatusKnown()).toBe(true);
    expect(component.tradingViewConfigured()).toBe(false);
  });

  it('hides the notice once TradingView is configured and enabled', () => {
    integrationsSpy.getTradingViewConfig.mockReturnValue(
      of({ enabled: true, mcp_server_url: 'https://tv/mcp', tool_name: 'get_ohlcv', auth_token_configured: true }),
    );
    initAndDestroy();
    expect(component.tradingViewStatusKnown()).toBe(true);
    expect(component.tradingViewConfigured()).toBe(true);
  });

  it('keeps the notice hidden when the status call fails (never nag on unknown)', () => {
    integrationsSpy.getTradingViewConfig.mockReturnValue(throwError(() => new Error('offline')));
    initAndDestroy();
    expect(component.tradingViewStatusKnown()).toBe(false);
    expect(component.tradingViewConfigured()).toBe(false);
  });

  it('adopts the backend category list and resets the selection to all', () => {
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50, asset_categories: ['forex', 'crypto'] }),
    );

    initAndDestroy();

    expect(component.categoryOptions().map((c) => c.value)).toEqual(['forex', 'crypto']);
    expect(component.categoryOptions()[0].label).toBe('Forex');
    expect(component.selectedCategories()).toEqual(['forex', 'crypto']);
  });

  it('preserves an explicit user selection when the backend list arrives late', () => {
    // Simulate the user narrowing the selection (sets the userAdjusted flag)
    // before the config response lands.
    component.onCategoriesChanged(['forex']);
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({
        batch_count_min: 1,
        batch_count_max: 50,
        asset_categories: ['stocks', 'crypto', 'forex', 'futures', 'commodities'],
      }),
    );

    initAndDestroy();

    // Their explicit choice survives (it is not clobbered back to "all selected").
    expect(component.selectedCategories()).toEqual(['forex']);
  });

  it('falls back to all categories when an explicit selection no longer exists', () => {
    component.onCategoriesChanged(['stocks']);
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50, asset_categories: ['forex', 'crypto'] }),
    );

    initAndDestroy();

    // 'stocks' is gone; rather than leave zero categories, default to all.
    expect(component.selectedCategories()).toEqual(['forex', 'crypto']);
  });

  it('keeps the fallback categories when the backend omits the list', () => {
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50 }),
    );

    initAndDestroy();

    expect(component.categoryOptions().map((c) => c.value)).toEqual([
      'stocks',
      'crypto',
      'forex',
      'futures',
      'commodities',
    ]);
  });

  it('cancels a pending auto-scroll timer on destroy', () => {
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');
    // Simulate a scroll timer left pending when the view is torn down.
    (component as unknown as { autoScrollTimeoutId: ReturnType<typeof setTimeout> | null })
      .autoScrollTimeoutId = setTimeout(() => undefined, 10_000);

    component.ngOnDestroy();

    expect(clearSpy).toHaveBeenCalled();
    expect(
      (component as unknown as { autoScrollTimeoutId: ReturnType<typeof setTimeout> | null })
        .autoScrollTimeoutId,
    ).toBeNull();
    clearSpy.mockRestore();
  });
});

describe('StrategyLabComponent — publishability gating', () => {
  let component: StrategyLabComponent;
  let apiSpy: { runPaperTrading: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      runPaperTrading: vi.fn().mockReturnValue(of({ session: { session_id: 'pt-1', status: 'running' } })),
    };
    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        {
          provide: InvestmentApiService,
          useValue: {
            runStrategyLab: vi.fn(),
            streamRunStatus: vi.fn().mockReturnValue(NEVER),
            getStrategyLabConfig: vi.fn().mockReturnValue(
              of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
            ),
            getStrategyLabResults: vi.fn().mockReturnValue(
              of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
            ),
            getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
            getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
            runPaperTrading: apiSpy.runPaperTrading,
          },
        },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({
                enabled: false,
                mcp_server_url: '',
                tool_name: 'get_ohlcv',
                auth_token_configured: false,
              }),
            ),
          },
        },
      ],
    })
      .overrideProvider(MatDialog, {
        useValue: { open: vi.fn().mockReturnValue({ afterClosed: () => of(true) }) },
      })
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: createRunServiceStub() }] },
      })
      .compileComponents();

    component = TestBed.createComponent(StrategyLabComponent).componentInstance;
  });

  it('publishabilitySkipLabel prefers publishability_skip_reason', () => {
    expect(
      component.publishabilitySkipLabel({
        lab_record_id: 'lab-1',
        is_winning: true,
        is_publishable: false,
        publishability_skip_reason: 'realism_failed',
        paper_trading_skipped_reason: 'realism_failed,alignment_unresolved',
        strategy_rationale: '',
        analysis_narrative: '',
        created_at: '',
        strategy: {} as never,
        backtest: {} as never,
      }),
    ).toBe('realism_failed');
  });

  it('runPaperTrading no-ops and sets error when not publishable', () => {
    component.runPaperTrading({
      lab_record_id: 'lab-legacy',
      is_winning: true,
      is_publishable: false,
      publishability_skip_reason: 'realism_failed',
      strategy_rationale: '',
      analysis_narrative: '',
      created_at: '',
      strategy: {} as never,
      backtest: {} as never,
    });
    expect(apiSpy.runPaperTrading).not.toHaveBeenCalled();
    expect(component.error()).toContain('not publishable');
    expect(component.error()).toContain('realism_failed');
  });
});

describe('StrategyLabComponent — destructive confirmations', () => {
  let component: StrategyLabComponent;
  let apiSpy: {
    deleteStrategyLabRecord: ReturnType<typeof vi.fn>;
    clearStrategyLabStorage: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
  };
  let notifySpy: { saved: ReturnType<typeof vi.fn> };
  let dialogSpy: { open: ReturnType<typeof vi.fn> };
  let runService: RunServiceStub;
  // Read lazily by the dialog stub's afterClosed(), so a test can set the
  // outcome before invoking the action under test.
  let confirmResult: boolean;

  const record = {
    lab_record_id: 'rec-1',
    strategy: { hypothesis: 'Buy dips in a strong uptrend' },
  } as unknown as Parameters<typeof component.deleteRecord>[0];

  beforeEach(async () => {
    confirmResult = true;
    apiSpy = {
      deleteStrategyLabRecord: vi.fn().mockReturnValue(of({})),
      clearStrategyLabStorage: vi.fn().mockReturnValue(of({})),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
    };
    notifySpy = { saved: vi.fn() };
    dialogSpy = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(confirmResult) }),
    };
    runService = createRunServiceStub();

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
            ),
          },
        },
        { provide: NotificationService, useValue: notifySpy },
      ],
    })
      .overrideProvider(MatDialog, { useValue: dialogSpy })
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: runService }] },
      })
      .compileComponents();

    // No detectChanges(): keeps ngOnInit's data loads out of these focused tests.
    component = TestBed.createComponent(StrategyLabComponent).componentInstance;
  });

  it('opens a danger confirm dialog and deletes when confirmed', () => {
    confirmResult = true;

    component.deleteRecord(record);

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(dialogSpy.open.mock.calls[0][1].data).toMatchObject({ variant: 'danger' });
    expect(apiSpy.deleteStrategyLabRecord).toHaveBeenCalledWith('rec-1');
    expect(apiSpy.getStrategyLabResults).toHaveBeenCalled(); // loadResults() ran
    expect(notifySpy.saved).toHaveBeenCalledWith('Strategy lab run deleted.');
  });

  it('does not delete when the dialog is cancelled', () => {
    confirmResult = false;

    component.deleteRecord(record);

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(apiSpy.deleteStrategyLabRecord).not.toHaveBeenCalled();
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });

  it('opens a danger confirm dialog and clears all data when confirmed', () => {
    confirmResult = true;

    component.clearAllLabData();

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(dialogSpy.open.mock.calls[0][1].data).toMatchObject({ variant: 'danger' });
    expect(apiSpy.clearStrategyLabStorage).toHaveBeenCalled();
    expect(apiSpy.getStrategyLabResults).toHaveBeenCalled(); // loadResults() ran
    expect(runService.clearPaperTradingSessions).toHaveBeenCalled();
    expect(notifySpy.saved).toHaveBeenCalledWith('Strategy lab data cleared.');
  });

  it('does not clear data when the dialog is cancelled', () => {
    confirmResult = false;

    component.clearAllLabData();

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(apiSpy.clearStrategyLabStorage).not.toHaveBeenCalled();
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });

  it('surfaces an error and skips the toast when delete fails after confirm', () => {
    confirmResult = true;
    apiSpy.deleteStrategyLabRecord.mockReturnValueOnce(
      throwError(() => ({ error: { detail: 'boom' } })),
    );

    component.deleteRecord(record);

    expect(component.error()).toBe('boom');
    expect(component.deletingLabRecordId()).toBeNull();
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });

  it('surfaces an error and skips the toast when clear-all fails after confirm', () => {
    confirmResult = true;
    apiSpy.clearStrategyLabStorage.mockReturnValueOnce(
      throwError(() => ({ error: { detail: 'kaboom' } })),
    );

    component.clearAllLabData();

    expect(component.error()).toBe('kaboom');
    expect(component.clearingAll()).toBe(false);
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });

  it('does not open a second confirmation while one dialog is still pending', () => {
    // A dialog that has not resolved yet (afterClosed has not emitted), so the
    // re-entrancy guard should stay engaged across a rapid second activation.
    const closed$ = new Subject<boolean>();
    dialogSpy.open.mockReturnValue({ afterClosed: () => closed$.asObservable() });

    component.deleteRecord(record);
    component.deleteRecord(record); // rapid second activation before the first closes

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(apiSpy.deleteStrategyLabRecord).not.toHaveBeenCalled();

    // Closing the first dialog releases the guard so later actions work again.
    closed$.next(false);
    closed$.complete();
    component.clearAllLabData();
    expect(dialogSpy.open).toHaveBeenCalledTimes(2);
  });
});

/**
 * OnPush's classic failure mode is a binding that quietly stops reflecting
 * its source once change detection stops running unconditionally — these
 * tests drive state through `StrategyLabRunService`'s signals (never
 * touching the component directly) and confirm the rendered DOM follows,
 * proving the template is actually bound to the service rather than a
 * stale/disconnected snapshot.
 */
describe('StrategyLabComponent — rendered template under OnPush', () => {
  let fixture: ComponentFixture<StrategyLabComponent>;
  let runService: RunServiceStub;
  let apiSpy: {
    runStrategyLab: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getStrategyLabConfig: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    getActiveRuns: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    runService = createRunServiceStub();
    apiSpy = {
      runStrategyLab: vi.fn(),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
    };

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
            ),
          },
        },
      ],
    })
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: runService }] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // initial render — runs ngOnInit
  });

  it('renders the in-progress section once the service reports a running run', () => {
    expect(fixture.nativeElement.querySelector('.in-progress-section')).toBeNull();

    runService.running.set(true);
    runService.runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2026-01-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 2,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    const section: HTMLElement = fixture.nativeElement.querySelector('.in-progress-section');
    expect(section).toBeTruthy();
    expect(section.textContent).toContain('Strategy 3 of 5');
  });

  it('removes the in-progress section once the service reports the run finished', () => {
    runService.running.set(true);
    runService.runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2026-01-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 2,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.in-progress-section')).toBeTruthy();

    runService.running.set(false);
    runService.runStatus.set(null);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.in-progress-section')).toBeNull();
  });

  it('reflects a paper-trading session the service reports, keyed by lab_record_id', () => {
    const record = { lab_record_id: 'rec-1', is_winning: true } as unknown as Parameters<
      typeof fixture.componentInstance.getPaperSession
    >[0];
    expect(fixture.componentInstance.getPaperSession(record)).toBeNull();

    runService.paperTradingSessions.set({
      'rec-1': { session_id: 'pt-1', status: 'running' } as unknown as PaperTradingSession,
    });

    expect(fixture.componentInstance.getPaperSession(record)?.session_id).toBe('pt-1');
  });

  it('refreshes results exactly once when running() transitions from true to false', () => {
    apiSpy.getStrategyLabResults.mockClear();

    runService.running.set(true);
    fixture.detectChanges();
    expect(apiSpy.getStrategyLabResults).not.toHaveBeenCalled(); // becoming true triggers nothing

    runService.running.set(false);
    fixture.detectChanges();

    expect(apiSpy.getStrategyLabResults).toHaveBeenCalledTimes(1);
  });

  it('does not refresh results again while running() stays false', () => {
    apiSpy.getStrategyLabResults.mockClear();
    runService.running.set(true);
    fixture.detectChanges();
    runService.running.set(false);
    fixture.detectChanges();
    expect(apiSpy.getStrategyLabResults).toHaveBeenCalledTimes(1);

    fixture.detectChanges(); // an unrelated CD pass — must not re-trigger

    expect(apiSpy.getStrategyLabResults).toHaveBeenCalledTimes(1);
  });
});

/**
 * `handleStreamEvent` reacts to events runService emits on `events$` for the
 * side effects that aren't StrategyLabRunStatus fields (that folding is
 * reduce()'s job, covered by strategy-lab-run.reducer.spec.ts). These tests
 * push events directly through the stub's `events$` Subject.
 */
describe('StrategyLabComponent — SSE event side effects (events$ wiring)', () => {
  let fixture: ComponentFixture<StrategyLabComponent>;
  let component: StrategyLabComponent;
  let runService: RunServiceStub;
  let apiSpy: {
    runStrategyLab: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getStrategyLabConfig: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    getActiveRuns: ReturnType<typeof vi.fn>;
  };

  const baseRunStatus: StrategyLabRunStatus = {
    run_id: 'run-1',
    status: 'running',
    started_at: '2026-01-01T00:00:00Z',
    total_cycles: 5,
    completed_cycles: 0,
    skipped_cycles: 0,
    completed_record_ids: [],
  };

  beforeEach(async () => {
    runService = createRunServiceStub();
    apiSpy = {
      runStrategyLab: vi.fn(),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
    };

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
            ),
          },
        },
      ],
    })
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: runService }] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(StrategyLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges(); // runs ngOnInit, subscribes to events$/errors$
  });

  describe('progress', () => {
    it('adds an activity-log entry and resets the log on a new cycle_index, but only when runStatus is set', () => {
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
      expect(component.activityLog()).toEqual([]); // no runStatus yet — guarded no-op

      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
      expect(component.activityLog()).toEqual([
        { time: expect.any(String), status: 'active', message: 'Ideating new trading strategy & generating code...' },
      ]);

      runService.events$.next({
        type: 'progress',
        cycle_index: 0,
        phase: 'ideating',
        sub_phase: 'completed',
        strategy: { asset_class: 'stocks', hypothesis: 'x' },
      });
      // Same cycle_index: log is not reset, previous active entry closes, new one appends.
      // sub_phase 'completed' is itself terminal, so the new entry is also 'done'.
      const log = component.activityLog();
      expect(log).toHaveLength(2);
      expect(log[0].status).toBe('done');
      expect(log[1]).toEqual({
        time: expect.any(String),
        status: 'done',
        message: 'Strategy ideated — stocks asset class',
      });

      runService.events$.next({ type: 'progress', cycle_index: 1, phase: 'coding', sub_phase: 'started' });
      // New cycle_index: log resets to just the new entry.
      expect(component.activityLog()).toEqual([
        { time: expect.any(String), status: 'active', message: 'Validating strategy spec and code safety...' },
      ]);
    });

    it('drives the phase-stepper template bindings once a current_cycle is present', () => {
      // The stub's events$ is a plain notification channel — it doesn't fold
      // through reduce() the way the real service does (that folding is
      // reduce()'s own contract, covered by strategy-lab-run.reducer.spec.ts).
      // Set the post-fold runStatus directly, then push the same event so
      // handleStreamEvent's own side effects (activity log) also run.
      runService.runStatus.set({
        ...baseRunStatus,
        current_cycle: { cycle_index: 0, phase: 'backtesting', sub_phase: 'running_code' },
      });
      runService.running.set(true);
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'running_code' });
      fixture.detectChanges();

      // The phase-by-phase completed/current/pending logic itself is unit-tested
      // in isolation in phase-stepper.component.spec.ts; this only proves the
      // host wires `[currentPhase]` to the right value.
      const currentStep: HTMLElement = fixture.nativeElement.querySelector('.phase-step.current .phase-label');
      expect(currentStep?.textContent?.trim()).toBe('Backtest');
      const completedSteps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step.completed'));
      expect(completedSteps.length).toBe(2); // Ideate, Code
    });
  });

  describe('cycle_complete', () => {
    it('resets the activity log and reloads results, only when runStatus is set', () => {
      apiSpy.getStrategyLabResults.mockClear();
      runService.events$.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });
      expect(apiSpy.getStrategyLabResults).not.toHaveBeenCalled(); // guarded no-op

      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating' });
      runService.events$.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });

      expect(component.activityLog()).toEqual([]);
      expect(apiSpy.getStrategyLabResults).toHaveBeenCalledTimes(1);
    });
  });

  describe('batch_warning', () => {
    it('does not set a warning when runStatus is unset (guarded no-op)', () => {
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' });
      expect(component.completionWarning()).toBeNull();
    });

    it('shows the specific message for a signal-brief failure', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' });
      expect(component.completionWarning()).toBe(
        'Signal brief unavailable for a batch; strategies continued without it.',
      );
    });

    it('shows the reason text for any other non-empty reason', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'disk_full' });
      expect(component.completionWarning()).toBe('disk_full');
    });

    it('falls back to a generic message for an empty reason', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: '' });
      expect(component.completionWarning()).toBe('A non-fatal warning occurred during a batch.');
    });
  });

  describe('complete', () => {
    it('sets a completion warning when cycles errored', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 4, skipped_count: 0, errored_count: 1, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(component.completionWarning()).toBe('Run finished with 1 cycle(s) errored. See details below.');
    });

    it('sets a completion warning combining errored and skipped counts', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed_with_errors',
        completed_count: 3, skipped_count: 2, errored_count: 1, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(component.completionWarning()).toBe('Run finished with 1 cycle(s) errored and 2 cycle(s) skipped. See details below.');
    });

    it('sets no completion warning for a clean finish', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 5, skipped_count: 0, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(component.completionWarning()).toBeNull();
    });

    it('sets no completion warning for a skip-only, zero-error finish (the sighted banner stays scoped to genuine errors)', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 3, skipped_count: 2, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(component.completionWarning()).toBeNull();
    });
  });

  describe('error', () => {
    it('surfaces the detail message', () => {
      runService.events$.next({ type: 'error', detail: 'Sandbox crashed' });
      expect(component.error()).toBe('Sandbox crashed');
    });

    it('falls back to a connection-lost message, not "Run failed", for the shared-infra reclaim shape', () => {
      // Regression: a subscription-reclaim event (only .error, never
      // .detail — a connection-level event, not necessarily a job failure)
      // used to fall through to the generic "Run failed" default, wrongly
      // announcing a definite failure for what may just be a reconnectable
      // connection loss.
      runService.events$.next({ type: 'error', error: 'subscription reclaimed' });
      expect(component.error()).toBe('Strategy Lab lost track of the run — status unavailable.');
    });

    it('shows the external-stop detail in the error banner for an interrupted run', () => {
      // An externally-interrupted run is published as an 'error' with a
      // terminal_status; the visible banner still shows the detail text (the
      // interrupted-vs-failed distinction is exercised in the announcement's
      // a11y spec).
      runService.events$.next({
        type: 'error',
        detail: 'Run was marked interrupted externally.',
        terminal_status: 'interrupted',
      });
      expect(component.error()).toBe('Run was marked interrupted externally.');
    });

    it('keeps the terminal error banner visible through the run-finish results refresh', () => {
      // Regression: finishRun() flips running() false on the same transition a
      // terminal 'error' arrives, firing refreshResultsOnRunFinish ->
      // loadResults(), whose first act is error.set(null). The failure banner
      // must survive that refresh, not flash and vanish, so sighted users
      // retain a visible reason the run stopped.
      runService.runStatus.set(baseRunStatus);
      runService.running.set(true);
      fixture.detectChanges(); // effect records wasRunning = true

      runService.events$.next({ type: 'error', detail: 'Sandbox crashed' });
      apiSpy.getStrategyLabResults.mockClear();
      runService.running.set(false);
      fixture.detectChanges(); // effect fires: loadResults() clears error, then it is re-asserted

      // loadResults() genuinely ran on this transition (so error WAS cleared)…
      expect(apiSpy.getStrategyLabResults).toHaveBeenCalled();
      // …yet the terminal banner is still shown.
      expect(component.error()).toBe('Sandbox crashed');
    });

    it('does NOT resurrect an unrelated ambient error after a cleanly-completed run', () => {
      // Regression: the run-finish refresh must re-assert only a terminal RUN
      // error (terminalErrorBanner), never whatever error() happens to hold. A
      // mid-run ambient error (e.g. an errors$ paper-trading poll failure) that
      // is still showing when the run completes cleanly must be cleared by the
      // refresh, not pinned onto the successful run.
      runService.runStatus.set(baseRunStatus);
      runService.running.set(true);
      fixture.detectChanges(); // effect records wasRunning = true

      // An unrelated ambient error surfaces mid-run (the errors$ subscription).
      runService.errors$.next('Paper trading polling failed.');
      expect(component.error()).toBe('Paper trading polling failed.');

      // The strategy run then completes cleanly — no 'error' event.
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 5, skipped_count: 0, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      runService.running.set(false);
      fixture.detectChanges(); // effect fires: loadResults() clears; nothing re-asserts

      expect(component.error()).toBeNull();
    });
  });

  describe('cancelled', () => {
    it('surfaces a cancellation in the non-error warning banner, not the red error banner', () => {
      // A deliberate user cancellation is a notice, not an error — it must not
      // set the red error banner now that it has its own event type.
      runService.events$.next({ type: 'cancelled', detail: 'Run cancelled by user' });
      expect(component.completionWarning()).toBe('Run cancelled by user');
      expect(component.error()).toBeNull();
    });

    it('falls back to a default cancellation notice when detail is empty', () => {
      runService.events$.next({ type: 'cancelled', detail: '' });
      expect(component.completionWarning()).toBe('Run cancelled by user.');
      expect(component.error()).toBeNull();
    });
  });

  it('does nothing for a done event', () => {
    runService.runStatus.set(baseRunStatus);
    runService.events$.next({ type: 'done' });
    expect(component.activityLog()).toEqual([]);
    expect(component.completionWarning()).toBeNull();
    expect(component.error()).toBeNull();
  });
});

describe('StrategyLabComponent — pure helpers and remaining error paths', () => {
  let component: StrategyLabComponent;
  let runService: RunServiceStub;
  let apiSpy: {
    runStrategyLab: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getStrategyLabConfig: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    getActiveRuns: ReturnType<typeof vi.fn>;
    runPaperTrading: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    runService = createRunServiceStub();
    apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(of({ run_id: 'run-1', status: 'running', total_cycles: 10, message: 'started' })),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
      runPaperTrading: vi.fn().mockReturnValue(of({ session: { session_id: 'pt-1', status: 'running' } })),
    };

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
            ),
          },
        },
      ],
    })
      .overrideProvider(MatDialog, {
        useValue: { open: vi.fn().mockReturnValue({ afterClosed: () => of(true) }) },
      })
      .overrideComponent(StrategyLabComponent, {
        set: { providers: [{ provide: StrategyLabRunService, useValue: runService }] },
      })
      .compileComponents();

    component = TestBed.createComponent(StrategyLabComponent).componentInstance;
  });

  it('toggleCard opens then closes a card', () => {
    expect(component.isCardExpanded('rec-1')).toBe(false);
    component.toggleCard('rec-1');
    expect(component.isCardExpanded('rec-1')).toBe(true);
    component.toggleCard('rec-1');
    expect(component.isCardExpanded('rec-1')).toBe(false);
  });

  it('loadConfig warns and keeps the fallback list when the config request fails', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    apiSpy.getStrategyLabConfig.mockReturnValue(throwError(() => new Error('offline')));

    component.ngOnInit();
    component.ngOnDestroy();

    expect(warnSpy).toHaveBeenCalledWith(
      'Failed to load strategy lab config; using fallback categories',
      expect.any(Error),
    );
    expect(component.categoryOptions().map((c) => c.value)).toEqual([
      'stocks', 'crypto', 'forex', 'futures', 'commodities',
    ]);
    warnSpy.mockRestore();
  });

  it('runNewStrategy surfaces an error and clears startingRun when the POST fails', () => {
    apiSpy.runStrategyLab.mockReturnValue(throwError(() => ({ error: { detail: 'capacity exceeded' } })));

    component.runNewStrategy();

    expect(component.error()).toBe('capacity exceeded');
    expect(component.running()).toBe(false);
    expect(runService.startRun).not.toHaveBeenCalled();
  });

  it('runButtonLabel adapts to multi-batch mode', () => {
    component.batchSize = 5;
    component.batchCount = 3;
    expect(component.runButtonLabel()).toBe('Run 5 × 3 = 15 strategies');
  });

  it('onFilterChange narrows displayedItems to winning or losing records', () => {
    const winning = { lab_record_id: 'w', is_winning: true } as unknown as StrategyLabRecord;
    const losing = { lab_record_id: 'l', is_winning: false } as unknown as StrategyLabRecord;
    apiSpy.getStrategyLabResults.mockReturnValue(
      of({ items: [winning, losing], count: 2, winning_count: 1, losing_count: 1 }),
    );
    component.loadResults();

    component.onFilterChange('winning');
    expect(component.displayedItems()).toEqual([winning]);

    component.onFilterChange('losing');
    expect(component.displayedItems()).toEqual([losing]);

    component.onFilterChange('all');
    expect(component.displayedItems()).toEqual([winning, losing]);
  });

  it('returnColor classifies annualized return into winning/neutral/losing', () => {
    expect(component.returnColor(9)).toBe('winning');
    expect(component.returnColor(3)).toBe('neutral');
    expect(component.returnColor(-1)).toBe('losing');
  });

  it('getAssetClassIcon falls back to a default icon for an unknown class', () => {
    expect(component.getAssetClassIcon('stocks')).toBe('show_chart');
    expect(component.getAssetClassIcon('unknown-class')).toBe('trending_up');
  });

  it('progressPercent handles a zero-total-cycles run and a normal one', () => {
    runService.runStatus.set({
      run_id: 'run-1', status: 'running', started_at: '', total_cycles: 0,
      completed_cycles: 0, skipped_cycles: 0, completed_record_ids: [],
    });
    expect(component.progressPercent()).toBe(0);

    runService.runStatus.set({
      run_id: 'run-1', status: 'running', started_at: '', total_cycles: 4,
      completed_cycles: 1, skipped_cycles: 0, completed_record_ids: [],
    });
    expect(component.progressPercent()).toBe(25);
  });

  it('erroredTooltip formats recent errored cycles, with and without a batch index', () => {
    expect(component.erroredTooltip()).toBe('');

    runService.runStatus.set({
      run_id: 'run-1', status: 'running', started_at: '', total_cycles: 4,
      completed_cycles: 0, skipped_cycles: 0, completed_record_ids: [],
      errored_details: [
        { cycle_index: 1, error: 'boom' },
        { cycle_index: 2, batch_index: 3, error: 'kaboom' },
      ],
    });

    expect(component.erroredTooltip()).toBe('#1: boom\n#2 (batch 3): kaboom');
  });

  it('buildLogMessage covers coding/backtesting/analyzing sub-phases via addLogEntry', () => {
    // events$ is only subscribed to from ngOnInit — needed for push() below to reach handleStreamEvent.
    component.ngOnInit();
    runService.runStatus.set({
      run_id: 'run-1', status: 'running', started_at: '', total_cycles: 4,
      completed_cycles: 0, skipped_cycles: 0, completed_record_ids: [],
    });
    const push = (event: StrategyLabStreamEvent) => runService.events$.next(event);
    const messages = (): string[] => component.activityLog().map((e) => e.message);

    push({ type: 'progress', cycle_index: 0, phase: 'coding', sub_phase: 'failed', checks_total: 5, checks_passed: 3 });
    expect(messages().at(-1)).toBe('Validation failed (2 critical issue(s))');

    push({ type: 'progress', cycle_index: 0, phase: 'coding', sub_phase: 'refining', refinement_round: 1, failure_phase: 'backtesting' });
    expect(messages().at(-1)).toBe('Refining code (round 2/10) — fixing backtesting...');

    push({ type: 'progress', cycle_index: 0, phase: 'coding', sub_phase: 'refined', changes_made: 'tightened stop loss' });
    expect(messages().at(-1)).toBe('Code refined — tightened stop loss');

    push({ type: 'progress', cycle_index: 0, phase: 'coding', sub_phase: 'unmapped' });
    expect(messages().at(-1)).toBe('Coding...');

    push({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'fetching_data' });
    expect(messages().at(-1)).toBe('Fetching historical market data...');

    push({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'data_loaded', symbols_count: 3, bars_count: 1234 });
    expect(messages().at(-1)).toBe('Market data loaded (3 symbols, 1,234 bars)');

    push({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'completed', trades_count: 12, execution_time: 4.567 });
    expect(messages().at(-1)).toBe('Backtest complete — 12 trades in 4.6s');

    push({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'unmapped' });
    expect(messages().at(-1)).toBe('Backtesting...');

    push({ type: 'progress', cycle_index: 0, phase: 'analyzing', sub_phase: 'draft' });
    expect(messages().at(-1)).toBe('Generating analysis narrative...');

    push({ type: 'progress', cycle_index: 0, phase: 'analyzing', sub_phase: 'review' });
    expect(messages().at(-1)).toBe('Self-reviewing analysis against metrics...');

    push({ type: 'progress', cycle_index: 0, phase: 'analyzing', sub_phase: 'completed', is_winning: true });
    expect(messages().at(-1)).toBe('Analysis complete — WINNING');

    push({ type: 'progress', cycle_index: 0, phase: 'analyzing', sub_phase: 'unmapped' });
    expect(messages().at(-1)).toBe('Analyzing...');

    push({ type: 'progress', cycle_index: 0, phase: 'phase_transition', sub_phase: 'foo' });
    expect(messages().at(-1)).toBe('phase_transition — foo');
  });

  // isRemedied/gateIcon/gateSeverityClass/gateViewModels/comparisonMetrics/
  // verdictLabel/verdictColor moved to strategy-card.component.spec.ts along
  // with the markup and methods that used them.

  it('loadPaperTradingResults keeps the most recent session per lab record and resumes polling', () => {
    const older = { lab_record_id: 'rec-1', session_id: 'pt-old', status: 'completed', started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:05:00Z' };
    const newer = { lab_record_id: 'rec-1', session_id: 'pt-new', status: 'running', started_at: '2026-01-02T00:00:00Z', completed_at: '' };
    apiSpy.getPaperTradingResults.mockReturnValue(of({ items: [older, newer] }));

    component.loadPaperTradingResults();

    expect(runService.hydratePaperTradingSessions).toHaveBeenCalledWith({ 'rec-1': newer });
  });

  it('runPaperTrading tracks the session via runService on success', () => {
    const record = { lab_record_id: 'rec-1', is_publishable: true } as unknown as StrategyLabRecord;

    component.runPaperTrading(record);

    expect(apiSpy.runPaperTrading).toHaveBeenCalledWith({ lab_record_id: 'rec-1' });
    expect(runService.trackPaperTradingSession).toHaveBeenCalledWith(
      'rec-1',
      expect.objectContaining({ session_id: 'pt-1' }),
    );
  });

  it('runPaperTrading surfaces an error on failure', () => {
    apiSpy.runPaperTrading.mockReturnValue(throwError(() => ({ error: { detail: 'worker unavailable' } })));
    const record = { lab_record_id: 'rec-1', is_publishable: true } as unknown as StrategyLabRecord;

    component.runPaperTrading(record);

    expect(component.error()).toBe('worker unavailable');
  });
});
