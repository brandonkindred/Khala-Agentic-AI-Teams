import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NEVER, of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { NotificationService } from '../../core/notification.service';
import { StrategyLabComponent } from './strategy-lab.component';
import type { RunStrategyLabRequest, StrategyLabRunStartResponse } from '../../models';

/**
 * Focused coverage for the asset-category selection feature. The component is
 * instantiated without `detectChanges()` so `ngOnInit` (and its data-loading
 * API calls) never fire — we exercise the category state and `runNewStrategy`
 * payload directly.
 */
describe('StrategyLabComponent — asset categories', () => {
  let component: StrategyLabComponent;
  let fixture: ComponentFixture<StrategyLabComponent>;
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
      .compileComponents();

    fixture = TestBed.createComponent(StrategyLabComponent);
    component = fixture.componentInstance;
  });

  it('defaults to every category selected', () => {
    expect(component.selectedCategories).toEqual([
      'stocks',
      'crypto',
      'forex',
      'futures',
      'commodities',
    ]);
    expect(component.categoriesValid).toBe(true);
  });

  it('categoriesValid is false when no category is selected', () => {
    component.selectedCategories = [];
    expect(component.categoriesValid).toBe(false);
  });

  it('memoizes pagedTrades and winCount per record', () => {
    const record = {
      lab_record_id: 'rec-1',
      backtest: {
        trades: [
          { outcome: 'win', cumulative_pnl: 10 },
          { outcome: 'loss', cumulative_pnl: 4 },
          { outcome: 'win', cumulative_pnl: 9 },
        ],
      },
    } as unknown as Parameters<typeof component.pagedTrades>[0];

    const paged1 = component.pagedTrades(record);
    expect(component.pagedTrades(record)).toBe(paged1); // stable reference (same record + page)
    expect(component.winCount(record)).toBe(2);
    expect(component.winCount(record)).toBe(2); // served from cache on the second call
  });

  it('sends the selected categories in canonical order on the run request', () => {
    // Select out of canonical order; the payload must be reordered canonically.
    component.selectedCategories = ['forex', 'stocks'];

    component.runNewStrategy();

    expect(apiSpy.runStrategyLab).toHaveBeenCalledTimes(1);
    const payload = apiSpy.runStrategyLab.mock.calls[0][0] as RunStrategyLabRequest;
    expect(payload.allowed_asset_classes).toEqual(['stocks', 'forex']);
    expect(component.running).toBe(true);
  });

  it('omits allowed_asset_classes when every category is selected', () => {
    // All selected == "no constraint": the field is omitted rather than sending
    // the full list (functionally equivalent server-side, smaller payload).
    component.runNewStrategy();

    const payload = apiSpy.runStrategyLab.mock.calls[0][0] as RunStrategyLabRequest;
    expect(payload.allowed_asset_classes).toBeUndefined();
  });

  it('does not start a run when no category is selected', () => {
    component.selectedCategories = [];

    component.runNewStrategy();

    expect(apiSpy.runStrategyLab).not.toHaveBeenCalled();
    expect(component.running).toBe(false);
    expect(component.error).toContain('at least one asset category');
  });

  it('ignores a re-entrant call while a run is already in progress', () => {
    component.running = true;

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
    expect(component.tradingViewStatusKnown).toBe(true);
    expect(component.tradingViewConfigured).toBe(false);
  });

  it('hides the notice once TradingView is configured and enabled', () => {
    integrationsSpy.getTradingViewConfig.mockReturnValue(
      of({ enabled: true, mcp_server_url: 'https://tv/mcp', tool_name: 'get_ohlcv', auth_token_configured: true }),
    );
    initAndDestroy();
    expect(component.tradingViewStatusKnown).toBe(true);
    expect(component.tradingViewConfigured).toBe(true);
  });

  it('keeps the notice hidden when the status call fails (never nag on unknown)', () => {
    integrationsSpy.getTradingViewConfig.mockReturnValue(throwError(() => new Error('offline')));
    initAndDestroy();
    expect(component.tradingViewStatusKnown).toBe(false);
    expect(component.tradingViewConfigured).toBe(false);
  });

  it('adopts the backend category list and resets the selection to all', () => {
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50, asset_categories: ['forex', 'crypto'] }),
    );

    initAndDestroy();

    expect(component.categoryOptions.map((c) => c.value)).toEqual(['forex', 'crypto']);
    expect(component.categoryOptions[0].label).toBe('Forex');
    expect(component.selectedCategories).toEqual(['forex', 'crypto']);
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
    expect(component.selectedCategories).toEqual(['forex']);
  });

  it('falls back to all categories when an explicit selection no longer exists', () => {
    component.onCategoriesChanged(['stocks']);
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50, asset_categories: ['forex', 'crypto'] }),
    );

    initAndDestroy();

    // 'stocks' is gone; rather than leave zero categories, default to all.
    expect(component.selectedCategories).toEqual(['forex', 'crypto']);
  });

  it('keeps the fallback categories when the backend omits the list', () => {
    apiSpy.getStrategyLabConfig.mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 50 }),
    );

    initAndDestroy();

    expect(component.categoryOptions.map((c) => c.value)).toEqual([
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
    expect(component.error).toContain('not publishable');
    expect(component.error).toContain('realism_failed');
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

    expect(component.error).toBe('boom');
    expect(component.deletingLabRecordId).toBeNull();
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });

  it('surfaces an error and skips the toast when clear-all fails after confirm', () => {
    confirmResult = true;
    apiSpy.clearStrategyLabStorage.mockReturnValueOnce(
      throwError(() => ({ error: { detail: 'kaboom' } })),
    );

    component.clearAllLabData();

    expect(component.error).toBe('kaboom');
    expect(component.clearingAll).toBe(false);
    expect(notifySpy.saved).not.toHaveBeenCalled();
  });
});
