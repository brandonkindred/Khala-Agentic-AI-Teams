import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NEVER, of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
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

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

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
