import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabRunService } from '../../services/strategy-lab-run.service';
import { StrategyLabComponent } from './strategy-lab.component';
import type {
  ActiveRunsResponse,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
} from '../../models';

/**
 * End-to-end pass across the whole OnPush migration (#1657-#1660): unlike
 * strategy-lab.component.spec.ts (which stubs StrategyLabRunService to unit
 * test the component in isolation), these tests wire up the *real*
 * StrategyLabRunService — backed only by a mocked InvestmentApiService — so
 * the full chain (SSE/poll mock → service → reduce() → signals → template)
 * is exercised exactly as it runs in production, under real fake-timer-driven
 * polling and Angular's real automatic (zone-triggered) change detection via
 * `fixture.autoDetectChanges(true)`, which — confirmed empirically — respects
 * OnPush dirty-gating for the fixture's own root component (a manual
 * `fixture.detectChanges()` call does not: it always force-checks the root
 * regardless of OnPush, which is why the other spec files call it explicitly
 * after each stub signal write).
 */
describe('StrategyLabComponent — end-to-end OnPush migration regression', () => {
  let fixture: ComponentFixture<StrategyLabComponent>;
  let apiSpy: {
    runStrategyLab: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getRunStatus: ReturnType<typeof vi.fn>;
    getStrategyLabConfig: ReturnType<typeof vi.fn>;
    getStrategyLabResults: ReturnType<typeof vi.fn>;
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    getPaperTradingSession: ReturnType<typeof vi.fn>;
    getActiveRuns: ReturnType<typeof vi.fn>;
  };
  let sse: Subject<StrategyLabStreamEvent>;

  const startResponse = { run_id: 'run-1', status: 'running' as const, total_cycles: 3, message: 'started' };

  async function createFixture(): Promise<ComponentFixture<StrategyLabComponent>> {
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
        // StrategyLabRunService is NOT overridden here — the real class,
        // provided by StrategyLabComponent's own component-level `providers`.
      ],
    }).compileComponents();

    const f = TestBed.createComponent(StrategyLabComponent);
    f.autoDetectChanges(true);
    await vi.advanceTimersByTimeAsync(1); // let ngOnInit's initial loads settle
    return f;
  }

  beforeEach(() => {
    vi.useFakeTimers();
    sse = new Subject<StrategyLabStreamEvent>();
    apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(of(startResponse)),
      streamRunStatus: vi.fn().mockReturnValue(sse),
      getRunStatus: vi.fn(),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getPaperTradingSession: vi.fn(),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] } as ActiveRunsResponse)),
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders a multi-event live run correctly at each step, not just the final state', async () => {
    fixture = await createFixture();
    const el = () => fixture.nativeElement as HTMLElement;

    fixture.componentInstance.runNewStrategy();
    await vi.advanceTimersByTimeAsync(1);

    expect(el().querySelector('.in-progress-section')).toBeTruthy();
    expect(el().querySelector('.generate-btn')?.textContent).toContain('Strategy 1 of 3');

    // Step 1: ideation starts.
    sse.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
    await vi.advanceTimersByTimeAsync(1);
    expect(el().querySelector('.activity-log')?.textContent).toContain('Ideating new trading strategy');
    expect(el().querySelector('.phase-step.current .phase-label')?.textContent).toBe('Ideate: current step');

    // Step 2: ideation completes, strategy preview appears.
    sse.next({
      type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'completed',
      strategy: { asset_class: 'crypto', hypothesis: 'Breakout continuation' },
    });
    await vi.advanceTimersByTimeAsync(1);
    expect(el().querySelector('.preview-asset')?.textContent).toBe('crypto');
    expect(el().querySelector('.preview-hypothesis')?.textContent).toBe('Breakout continuation');

    // Step 3: moves to backtesting — phase stepper advances, ideate now shows completed.
    sse.next({ type: 'progress', cycle_index: 0, phase: 'backtesting', sub_phase: 'running_code' });
    await vi.advanceTimersByTimeAsync(1);
    const steps = Array.from(el().querySelectorAll('.phase-step'));
    const ideateStep = steps.find((s) => s.textContent?.includes('Ideate'));
    const backtestStep = steps.find((s) => s.textContent?.includes('Backtest'));
    expect(ideateStep?.className).toContain('completed');
    expect(backtestStep?.className).toContain('current');

    // Step 4: cycle completes — progress bar advances, phase stepper/activity log reset.
    sse.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });
    await vi.advanceTimersByTimeAsync(1);
    expect(el().querySelector('.progress-text')?.textContent?.trim()).toBe('33%');
    expect(apiSpy.getStrategyLabResults).toHaveBeenCalled(); // mid-run refresh after the completed cycle

    // Step 5: run completes — in-progress section disappears, run button resets.
    sse.next({
      type: 'complete', message: 'done', status: 'completed',
      completed_count: 3, skipped_count: 0, errored_count: 0, errored_details: [],
      completed_batches: 1, total_batches: 1,
    });
    await vi.advanceTimersByTimeAsync(1);
    expect(el().querySelector('.in-progress-section')).toBeNull();
    expect(el().querySelector('.generate-btn')?.getAttribute('disabled')).toBeNull();
  });

  it('renders correctly through the SSE-drop → polling-fallback transition', async () => {
    fixture = await createFixture();
    const el = () => fixture.nativeElement as HTMLElement;

    fixture.componentInstance.runNewStrategy();
    await vi.advanceTimersByTimeAsync(1);
    sse.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
    await vi.advanceTimersByTimeAsync(1);
    expect(el().querySelector('.in-progress-section')).toBeTruthy();

    // Transport-level SSE failure — falls back to REST polling.
    apiSpy.getRunStatus.mockReturnValue(of({
      run_id: 'run-1', status: 'running', started_at: '2026-01-01T00:00:00Z',
      total_cycles: 3, completed_cycles: 2, skipped_cycles: 0, completed_record_ids: [],
    } as StrategyLabRunStatus));
    sse.error(new Error('SSE connection lost'));
    await vi.advanceTimersByTimeAsync(1); // fallbackToPolling's first (t=0) tick

    expect(apiSpy.getRunStatus).toHaveBeenCalledWith('run-1');
    expect(el().querySelector('.generate-btn')?.textContent).toContain('Strategy 3 of 3');

    // Poll again, this time reporting the run finished.
    apiSpy.getRunStatus.mockReturnValue(of({
      run_id: 'run-1', status: 'completed', started_at: '2026-01-01T00:00:00Z',
      total_cycles: 3, completed_cycles: 3, skipped_cycles: 0, completed_record_ids: [],
    } as StrategyLabRunStatus));
    await vi.advanceTimersByTimeAsync(5000);

    expect(el().querySelector('.in-progress-section')).toBeNull();
  });

  it('does not re-render on checkForActiveRun ticks that find nothing running', async () => {
    fixture = await createFixture();
    const labelSpy = vi.spyOn(fixture.componentInstance, 'runButtonLabel');
    const callsAfterInit = labelSpy.mock.calls.length;

    // Two more checkForActiveRun ticks (t=3000, t=6000), still finding nothing.
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(3000);

    expect(labelSpy.mock.calls.length).toBe(callsAfterInit);
    expect(apiSpy.getActiveRuns).toHaveBeenCalledTimes(3); // the ticks did run — they just changed nothing
  });

  it('stops re-rendering from paper-trading polling once the session reaches a terminal status', async () => {
    fixture = await createFixture();
    apiSpy.getPaperTradingSession.mockReturnValue(of({
      session: { session_id: 'pt-1', lab_record_id: 'rec-1', status: 'completed' },
      message: 'ok',
    }));

    fixture.componentInstance.runService.trackPaperTradingSession('rec-1', {
      session_id: 'pt-1', lab_record_id: 'rec-1', status: 'running',
    } as never);
    await vi.advanceTimersByTimeAsync(3000); // the poll fires, sees 'completed', stops itself

    const labelSpy = vi.spyOn(fixture.componentInstance, 'runButtonLabel');
    const callsBefore = labelSpy.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30000); // far past another 3s/6s/9s tick, if any remained

    expect(apiSpy.getPaperTradingSession).toHaveBeenCalledTimes(1); // no further polling after terminal
    expect(labelSpy.mock.calls.length).toBe(callsBefore);
  });

  it('unsubscribes every StrategyLabRunService timer/SSE connection on component destroy', async () => {
    fixture = await createFixture();
    const runService = fixture.debugElement.injector.get(StrategyLabRunService);

    fixture.componentInstance.runNewStrategy();
    await vi.advanceTimersByTimeAsync(1);
    runService.trackPaperTradingSession('rec-1', { session_id: 'pt-1', lab_record_id: 'rec-1', status: 'running' } as never);
    await vi.advanceTimersByTimeAsync(1);

    expect(apiSpy.getActiveRuns).not.toHaveBeenCalledTimes(0); // sanity: checkForActiveRun did start
    apiSpy.getActiveRuns.mockClear();
    apiSpy.getPaperTradingSession.mockClear();

    fixture.destroy();
    await vi.advanceTimersByTimeAsync(30000); // well past every timer's next tick

    expect(apiSpy.getActiveRuns).not.toHaveBeenCalled();
    expect(apiSpy.getPaperTradingSession).not.toHaveBeenCalled();
    // The SSE Subject itself has no more subscribers once the service tore down its subscription.
    expect(sse.observed).toBe(false);
  });

  it('never lets a run appear to keep progressing after a hard error event', async () => {
    fixture = await createFixture();
    const el = () => fixture.nativeElement as HTMLElement;

    fixture.componentInstance.runNewStrategy();
    await vi.advanceTimersByTimeAsync(1);
    sse.next({ type: 'error', detail: 'Sandbox crashed unrecoverably' });
    await vi.advanceTimersByTimeAsync(1);

    expect(el().querySelector('.in-progress-section')).toBeNull();
    expect(fixture.componentInstance.running()).toBe(false);
    // The terminal 'error' message durably reaches the banner. Finishing the
    // run synchronously triggers refreshResultsOnRunFinish -> loadResults(),
    // which clears `error` at its own start; the effect captures the terminal
    // message beforehand and re-asserts it after, so the banner survives the
    // reload instead of flashing and vanishing (which had left sighted users
    // no visible reason the run stopped).
    expect(fixture.componentInstance.error()).toBe('Sandbox crashed unrecoverably');
  });
});
