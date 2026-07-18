import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import type { PaperTradingSession, StrategyLabRunStatus, StrategyLabStreamEvent, StrategySpec } from '../../models';

/**
 * `StrategyLabRunService` is designed to be provided at `StrategyLabComponent`'s
 * own component level in production (a follow-up change wires this up); this
 * minimal host reproduces that same component-provider context for tests,
 * matching `PrReviewRunsService`'s spec.
 */
@Component({ standalone: true, template: '', providers: [StrategyLabRunService] })
class TestHostComponent {}

function makeRunStatus(overrides: Partial<StrategyLabRunStatus> = {}): StrategyLabRunStatus {
  return {
    run_id: 'run-1',
    status: 'running',
    started_at: '2026-01-01T00:00:00Z',
    total_cycles: 10,
    completed_cycles: 2,
    skipped_cycles: 0,
    completed_record_ids: ['rec-1'],
    ...overrides,
  };
}

function makeSession(overrides: Partial<PaperTradingSession> = {}): PaperTradingSession {
  return {
    session_id: 'sess-1',
    lab_record_id: 'rec-1',
    strategy: {} as unknown as StrategySpec,
    status: 'running',
    initial_capital: 10000,
    current_capital: 10000,
    trades: [],
    trade_decisions: [],
    symbols_traded: [],
    data_source: 'test',
    data_period_start: '2026-01-01',
    data_period_end: '2026-01-02',
    started_at: '2026-01-01T00:00:00Z',
    completed_at: '',
    ...overrides,
  };
}

describe('StrategyLabRunService', () => {
  let service: StrategyLabRunService;
  let fixture: ComponentFixture<TestHostComponent>;
  let apiSpy: {
    getActiveRuns: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getRunStatus: ReturnType<typeof vi.fn>;
    getPaperTradingSession: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    vi.useFakeTimers();
    apiSpy = {
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
      streamRunStatus: vi.fn().mockReturnValue(new Subject<StrategyLabStreamEvent>()),
      getRunStatus: vi.fn(),
      getPaperTradingSession: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    });
    fixture = TestBed.createComponent(TestHostComponent);
    service = fixture.debugElement.injector.get(StrategyLabRunService);
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture?.destroy();
    vi.useRealTimers();
  });

  // -------------------------------------------------------------------------
  // SSE connect + event folding through reduce()
  // -------------------------------------------------------------------------

  it('startRun seeds state and connects the SSE stream', () => {
    const status = makeRunStatus({ run_id: 'run-42', completed_cycles: 1 });
    apiSpy.streamRunStatus.mockReturnValue(new Subject());

    service.startRun('run-42', status);

    expect(service.activeRunId()).toBe('run-42');
    expect(service.running()).toBe(true);
    expect(service.runStatus()).toEqual(status);
    expect(apiSpy.streamRunStatus).toHaveBeenCalledWith('run-42');
  });

  it('replaces rather than duplicates the SSE stream when startRun is called again', () => {
    const first$ = new Subject<StrategyLabStreamEvent>();
    const second$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValueOnce(first$).mockReturnValueOnce(second$);

    service.startRun('run-1', makeRunStatus());
    expect(first$.observed).toBe(true);
    service.startRun('run-2', makeRunStatus({ run_id: 'run-2' }));

    expect(first$.observed).toBe(false); // prior stream unsubscribed, not left dangling
    expect(second$.observed).toBe(true);
    expect(service.activeRunId()).toBe('run-2');
  });

  it('folds incoming SSE events into runStatus via reduce(), without mutating the previous snapshot', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);

    service.startRun('run-1', makeRunStatus({ completed_cycles: 2 }));
    const before = service.runStatus();
    sse$.next({ type: 'cycle_complete', cycle_index: 2, record_id: 'rec-1', completed_cycles: 3, batch_index: 0 });

    expect(before?.completed_cycles).toBe(2); // previous snapshot untouched
    expect(service.runStatus()?.completed_cycles).toBe(3); // new snapshot reflects the event
    expect(service.runStatus()).not.toBe(before); // reduce() returned a new object, not a mutation
  });

  it("forwards every stream event on events$ verbatim, for a future consumer's own side-effect switch", () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    const seen: StrategyLabStreamEvent[] = [];
    service.events$.subscribe((e) => seen.push(e));

    service.startRun('run-1', makeRunStatus());
    const progressEvent: StrategyLabStreamEvent = { type: 'progress', cycle_index: 0, phase: 'ideating' };
    sse$.next(progressEvent);

    expect(seen).toEqual([progressEvent]);
  });

  it('treats a "complete" stream event as ending the run immediately, distinct from the SSE observable completing', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    service.startRun('run-1', makeRunStatus());

    sse$.next({
      type: 'complete',
      message: 'done',
      status: 'completed',
      completed_count: 5,
      skipped_count: 0,
      errored_count: 0,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });

    expect(service.running()).toBe(false);
    expect(service.runStatus()).toBeNull();
    // The service itself unsubscribed the SSE stream in response to the event
    // type — sse$ was never completed, proving finishRun() tore it down proactively.
    expect(sse$.observed).toBe(false);
  });

  it('treats an "error" stream event as ending the run immediately (same mechanism as "complete")', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    service.startRun('run-1', makeRunStatus());

    sse$.next({ type: 'error', detail: 'boom' });

    expect(service.running()).toBe(false);
    expect(sse$.observed).toBe(false);
  });

  it('does not end the run for a "done"-type event on its own — only the observable completing does', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    service.startRun('run-1', makeRunStatus());

    sse$.next({ type: 'done' });
    expect(service.running()).toBe(true); // the event type alone doesn't finish the run

    sse$.complete(); // the observable actually completing does
    expect(service.running()).toBe(false);
  });

  // -------------------------------------------------------------------------
  // checkForActiveRun
  // -------------------------------------------------------------------------

  it('finds an active run and starts tracking it, connecting the SSE stream', () => {
    const active = makeRunStatus({ run_id: 'run-9' });
    apiSpy.getActiveRuns.mockReturnValue(of({ runs: [active] }));
    apiSpy.streamRunStatus.mockReturnValue(new Subject());

    service.checkForActiveRun();
    vi.advanceTimersByTime(0);

    expect(service.activeRunId()).toBe('run-9');
    expect(service.running()).toBe(true);
    expect(service.runStatus()).toEqual(active);
    expect(apiSpy.streamRunStatus).toHaveBeenCalledWith('run-9');
  });

  it('stops polling for an active run after 4 attempts (0s/3s/6s/9s) when none is ever found', () => {
    service.checkForActiveRun();
    vi.advanceTimersByTime(0); // attempt 1
    vi.advanceTimersByTime(3000); // attempt 2
    vi.advanceTimersByTime(3000); // attempt 3
    vi.advanceTimersByTime(3000); // attempt 4
    expect(apiSpy.getActiveRuns).toHaveBeenCalledTimes(4);

    vi.advanceTimersByTime(3000); // would be attempt 5 -> takeWhile stops it first
    expect(apiSpy.getActiveRuns).toHaveBeenCalledTimes(4);
  });

  it('stops checking as soon as a run is found, even before the 4-attempt cap', () => {
    const active = makeRunStatus({ run_id: 'run-9' });
    apiSpy.getActiveRuns.mockReturnValue(of({ runs: [active] }));
    apiSpy.streamRunStatus.mockReturnValue(new Subject());

    service.checkForActiveRun();
    vi.advanceTimersByTime(0); // attempt 1 finds it
    const callsAfterFound = apiSpy.getActiveRuns.mock.calls.length;
    vi.advanceTimersByTime(9000); // three more ticks that would otherwise have fired
    expect(apiSpy.getActiveRuns.mock.calls.length).toBe(callsAfterFound);
  });

  it('replaces rather than duplicates a running active-run check when called again', () => {
    service.checkForActiveRun(); // first poller scheduled
    service.checkForActiveRun(); // immediately superseded — first poller must be unsubscribed
    vi.advanceTimersByTime(0);
    expect(apiSpy.getActiveRuns).toHaveBeenCalledTimes(1); // not 2 — no duplicate ticking pollers
  });

  // Deliberately not tested: getActiveRuns() has no `error:` callback at all
  // (preserved verbatim — see class doc). RxJS's default unhandled-error path
  // schedules a throw via a macrotask, which fake timers would then re-throw
  // synchronously inside advanceTimersByTime — a fragile test coupled to
  // RxJS-internal scheduling for a quirk the issue explicitly says to
  // preserve, not verify.

  // -------------------------------------------------------------------------
  // SSE error -> fallback-to-polling
  // -------------------------------------------------------------------------

  it('falls back to REST polling when the SSE stream errors', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    apiSpy.getRunStatus.mockReturnValue(of(makeRunStatus({ run_id: 'run-1', completed_cycles: 3 })));

    service.startRun('run-1', makeRunStatus());
    sse$.error(new Error('SSE connection lost'));
    vi.advanceTimersByTime(0); // fallback timer(0, 5000)'s first tick

    expect(apiSpy.getRunStatus).toHaveBeenCalledWith('run-1');
    expect(service.runStatus()?.completed_cycles).toBe(3);
  });

  it('stops tracking once fallback polling observes a terminal status', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    apiSpy.getRunStatus.mockReturnValue(of(makeRunStatus({ run_id: 'run-1', status: 'completed' })));

    service.startRun('run-1', makeRunStatus());
    sse$.error(new Error('lost'));
    vi.advanceTimersByTime(0);

    expect(service.running()).toBe(false);
    expect(service.activeRunId()).toBeNull();
    expect(service.runStatus()).toBeNull();
  });

  it('stops tracking silently when the fallback REST poll itself fails (no errors$ emission)', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    apiSpy.getRunStatus.mockReturnValue(throwError(() => new Error('down')));
    const errors: string[] = [];
    service.errors$.subscribe((e) => errors.push(e));

    service.startRun('run-1', makeRunStatus());
    sse$.error(new Error('lost'));
    vi.advanceTimersByTime(0);

    expect(service.running()).toBe(false);
    expect(errors).toEqual([]); // silent, matching the original's lack of a message here
  });

  // -------------------------------------------------------------------------
  // Per-record paper-trading polling
  // -------------------------------------------------------------------------

  it('tracks a running paper-trading session and polls it to completion', () => {
    const session = makeSession({ lab_record_id: 'rec-1', session_id: 'sess-1', status: 'running' });
    apiSpy.getPaperTradingSession.mockReturnValue(
      of({ session: { ...session, status: 'completed' as const }, message: '' }),
    );

    service.trackPaperTradingSession('rec-1', session);
    expect(service.paperTradingSessions()['rec-1']).toEqual(session);

    vi.advanceTimersByTime(3000); // first poll tick (timer(3000, 3000) — no immediate fire)
    expect(apiSpy.getPaperTradingSession).toHaveBeenCalledWith('sess-1');
    expect(service.paperTradingSessions()['rec-1'].status).toBe('completed');
  });

  it('does not start polling a paper-trading session that is already terminal', () => {
    const session = makeSession({ lab_record_id: 'rec-1', status: 'completed' });
    service.trackPaperTradingSession('rec-1', session);
    vi.advanceTimersByTime(10000);
    expect(apiSpy.getPaperTradingSession).not.toHaveBeenCalled();
  });

  it('polls two records concurrently without cross-talk', () => {
    apiSpy.getPaperTradingSession.mockImplementation((sessionId: string) =>
      of({
        session: makeSession({
          lab_record_id: sessionId === 'sess-A' ? 'rec-A' : 'rec-B',
          session_id: sessionId,
          status: 'completed',
        }),
        message: '',
      }),
    );

    service.trackPaperTradingSession(
      'rec-A',
      makeSession({ lab_record_id: 'rec-A', session_id: 'sess-A', status: 'running' }),
    );
    service.trackPaperTradingSession(
      'rec-B',
      makeSession({ lab_record_id: 'rec-B', session_id: 'sess-B', status: 'running' }),
    );
    vi.advanceTimersByTime(3000);

    expect(apiSpy.getPaperTradingSession).toHaveBeenCalledWith('sess-A');
    expect(apiSpy.getPaperTradingSession).toHaveBeenCalledWith('sess-B');
    expect(service.paperTradingSessions()['rec-A'].status).toBe('completed');
    expect(service.paperTradingSessions()['rec-B'].status).toBe('completed');
  });

  it('stops polling once a paper-trading session reaches a terminal status', () => {
    const session = makeSession({ lab_record_id: 'rec-1', session_id: 'sess-1', status: 'running' });
    apiSpy.getPaperTradingSession.mockReturnValue(
      of({ session: { ...session, status: 'completed' as const }, message: '' }),
    );

    service.trackPaperTradingSession('rec-1', session);
    vi.advanceTimersByTime(3000);
    const callsAfterTerminal = apiSpy.getPaperTradingSession.mock.calls.length;
    vi.advanceTimersByTime(30000);
    expect(apiSpy.getPaperTradingSession.mock.calls.length).toBe(callsAfterTerminal);
  });

  it('surfaces a human-readable error on errors$ when paper-trading polling fails, and stops polling', () => {
    apiSpy.getPaperTradingSession.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    const errors: string[] = [];
    service.errors$.subscribe((e) => errors.push(e));

    service.trackPaperTradingSession(
      'rec-1',
      makeSession({ lab_record_id: 'rec-1', session_id: 'sess-1', status: 'running' }),
    );
    vi.advanceTimersByTime(3000);

    expect(errors).toEqual(['boom']);
    const callsAfterError = apiSpy.getPaperTradingSession.mock.calls.length;
    vi.advanceTimersByTime(30000);
    expect(apiSpy.getPaperTradingSession.mock.calls.length).toBe(callsAfterError); // no leak after error
  });

  it('falls back to a default message when a paper-trading poll error has no detail', () => {
    apiSpy.getPaperTradingSession.mockReturnValue(throwError(() => ({})));
    const errors: string[] = [];
    service.errors$.subscribe((e) => errors.push(e));

    service.trackPaperTradingSession(
      'rec-1',
      makeSession({ lab_record_id: 'rec-1', session_id: 'sess-1', status: 'running' }),
    );
    vi.advanceTimersByTime(3000);

    expect(errors).toEqual(['Paper trading polling failed.']);
  });

  it('restarts polling for the same record when tracked again before its first tick (no duplicate poller)', () => {
    const session = makeSession({ lab_record_id: 'rec-1', session_id: 'sess-1', status: 'running' });
    apiSpy.getPaperTradingSession.mockReturnValue(of({ session, message: '' }));

    service.trackPaperTradingSession('rec-1', session);
    service.trackPaperTradingSession('rec-1', session); // second call replaces, not duplicates, the poller
    vi.advanceTimersByTime(3000);

    expect(apiSpy.getPaperTradingSession).toHaveBeenCalledTimes(1);
  });

  // -------------------------------------------------------------------------
  // Teardown
  // -------------------------------------------------------------------------

  it('unsubscribes a live SSE stream on ngOnDestroy even when no fallback has occurred', () => {
    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    service.startRun('run-1', makeRunStatus());
    expect(sse$.observed).toBe(true);

    fixture.destroy();

    expect(sse$.observed).toBe(false);
  });

  it('tears down every subscription on destroy — no leaked active-run, fallback, or paper-trading polls', () => {
    apiSpy.getActiveRuns.mockReturnValue(of({ runs: [] }));
    service.checkForActiveRun();

    const sse$ = new Subject<StrategyLabStreamEvent>();
    apiSpy.streamRunStatus.mockReturnValue(sse$);
    apiSpy.getRunStatus.mockReturnValue(of(makeRunStatus({ status: 'running' })));
    service.startRun('run-99', makeRunStatus({ run_id: 'run-99' }));
    sse$.error(new Error('lost'));
    vi.advanceTimersByTime(0); // fallback poll now ticking too

    apiSpy.getPaperTradingSession.mockReturnValue(
      of({ session: makeSession({ lab_record_id: 'rec-1', status: 'running' }), message: '' }),
    );
    service.trackPaperTradingSession('rec-1', makeSession({ lab_record_id: 'rec-1', status: 'running' }));

    const activeRunsBefore = apiSpy.getActiveRuns.mock.calls.length;
    const runStatusBefore = apiSpy.getRunStatus.mock.calls.length;
    const paperBefore = apiSpy.getPaperTradingSession.mock.calls.length;

    fixture.destroy(); // triggers ngOnDestroy (component-provided)
    vi.advanceTimersByTime(60000); // well past every interval

    expect(apiSpy.getActiveRuns.mock.calls.length).toBe(activeRunsBefore);
    expect(apiSpy.getRunStatus.mock.calls.length).toBe(runStatusBefore);
    expect(apiSpy.getPaperTradingSession.mock.calls.length).toBe(paperBefore);
  });

  it('completes events$ and errors$ on ngOnDestroy', () => {
    let eventsCompleted = false;
    let errorsCompleted = false;
    service.events$.subscribe({
      complete: () => {
        eventsCompleted = true;
      },
    });
    service.errors$.subscribe({
      complete: () => {
        errorsCompleted = true;
      },
    });

    service.ngOnDestroy();

    expect(eventsCompleted).toBe(true);
    expect(errorsCompleted).toBe(true);
  });
});
