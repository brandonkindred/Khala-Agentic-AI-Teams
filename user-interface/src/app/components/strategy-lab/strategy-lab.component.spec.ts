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
import { StrategyLabActivityLogService } from '../../services/strategy-lab-activity-log.service';
import { StrategyLabPaperTradingService } from '../../services/strategy-lab-paper-trading.service';
import { StrategyLabComponent } from './strategy-lab.component';
import { createRunServiceStub, type RunServiceStub } from '../../testing/strategy-lab-run-service.stub';
import {
  createPaperTradingServiceStub,
  type PaperTradingServiceStub,
} from '../../testing/strategy-lab-paper-trading-service.stub';
import type {
  PaperTradingSession,
  RunStrategyLabRequest,
  StrategyLabRecord,
  StrategyLabRunStartResponse,
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
        set: {
          providers: [
            { provide: StrategyLabRunService, useValue: runService },
            StrategyLabActivityLogService,
            StrategyLabPaperTradingService,
          ],
        },
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
  // applied synchronously via `of(...)`) — no private-method access.
  const initAndDestroy = (): void => {
    component.ngOnInit();
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

});

describe('StrategyLabComponent — paper trading delegation', () => {
  let component: StrategyLabComponent;
  let paperTradingService: PaperTradingServiceStub;

  beforeEach(async () => {
    paperTradingService = createPaperTradingServiceStub();
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
        set: {
          providers: [
            { provide: StrategyLabRunService, useValue: createRunServiceStub() },
            StrategyLabActivityLogService,
            { provide: StrategyLabPaperTradingService, useValue: paperTradingService },
          ],
        },
      })
      .compileComponents();

    component = TestBed.createComponent(StrategyLabComponent).componentInstance;
  });

  it('runPaperTrading delegates to paperTradingService', () => {
    const record = { lab_record_id: 'rec-1', is_publishable: true } as unknown as StrategyLabRecord;

    component.runPaperTrading(record);

    expect(paperTradingService.runPaperTrading).toHaveBeenCalledWith(record);
  });

  it('getPaperSession delegates to paperTradingService', () => {
    const record = { lab_record_id: 'rec-1', is_publishable: true } as unknown as StrategyLabRecord;

    component.getPaperSession(record);

    expect(paperTradingService.getPaperSession).toHaveBeenCalledWith(record);
  });

  it('mirrors paperTradingService.errors$ into the error signal', () => {
    paperTradingService.errors$.next('not publishable (realism_failed)');
    expect(component.error()).toBe('not publishable (realism_failed)');

    paperTradingService.errors$.next(null);
    expect(component.error()).toBeNull();
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
        set: {
          providers: [
            { provide: StrategyLabRunService, useValue: runService },
            StrategyLabActivityLogService,
            StrategyLabPaperTradingService,
          ],
        },
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

  it('opens the confirm dialog without throwing when strategy.hypothesis is missing', () => {
    confirmResult = true;
    const recordWithoutHypothesis = {
      lab_record_id: 'rec-2',
      strategy: undefined,
    } as unknown as Parameters<typeof component.deleteRecord>[0];

    expect(() => component.deleteRecord(recordWithoutHypothesis)).not.toThrow();

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    const message = dialogSpy.open.mock.calls[0][1].data.message as string;
    expect(message).not.toContain('undefined');
    expect(message).toContain('Delete this strategy lab run?\n\n\n\nThis removes the record');
    expect(apiSpy.deleteStrategyLabRecord).toHaveBeenCalledWith('rec-2');
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
        set: {
          providers: [
            { provide: StrategyLabRunService, useValue: runService },
            StrategyLabActivityLogService,
            StrategyLabPaperTradingService,
          ],
        },
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
        set: {
          providers: [
            { provide: StrategyLabRunService, useValue: runService },
            StrategyLabActivityLogService,
            StrategyLabPaperTradingService,
          ],
        },
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

  // isRemedied/gateIcon/gateSeverityClass/gateViewModels/comparisonMetrics/
  // verdictLabel/verdictColor moved to strategy-card.component.spec.ts along
  // with the markup and methods that used them.

  // loadPaperTradingResults/runPaperTrading coverage moved to
  // strategy-lab-paper-trading.service.spec.ts; delegation is covered in the
  // 'paper trading delegation' describe above.
});
