import { TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { InvestmentApiService } from './investment-api.service';
import type {
  ActiveRunsResponse,
  PaperTradingResponse,
  PaperTradingSession,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
} from '../models';

describe('StrategyLabRunService', () => {
  let service: StrategyLabRunService;
  let api: {
    getActiveRuns: ReturnType<typeof vi.fn>;
    streamRunStatus: ReturnType<typeof vi.fn>;
    getRunStatus: ReturnType<typeof vi.fn>;
    getPaperTradingSession: ReturnType<typeof vi.fn>;
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

  const runningSession: PaperTradingSession = {
    session_id: 'pt-1',
    lab_record_id: 'rec-1',
    strategy: {} as never,
    status: 'running',
    initial_capital: 10000,
    current_capital: 10000,
    trades: [],
    trade_decisions: [],
    symbols_traded: [],
    data_source: 'tradingview',
    data_period_start: '',
    data_period_end: '',
    started_at: '2026-01-01T00:00:00Z',
    completed_at: '',
  };

  beforeEach(() => {
    api = {
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] } as ActiveRunsResponse)),
      streamRunStatus: vi.fn().mockReturnValue(new Subject<StrategyLabStreamEvent>()),
      getRunStatus: vi.fn(),
      getPaperTradingSession: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [StrategyLabRunService, { provide: InvestmentApiService, useValue: api }],
    });
    service = TestBed.inject(StrategyLabRunService);
  });

  afterEach(() => {
    service.ngOnDestroy();
    vi.useRealTimers();
  });

  it('starts with empty state', () => {
    expect(service.runStatus()).toBeNull();
    expect(service.running()).toBe(false);
    expect(service.activeRunId()).toBeNull();
    expect(service.paperTradingSessions()).toEqual({});
    expect(service.paperTradingLabRecordId()).toBeNull();
  });

  describe('checkForActiveRun', () => {
    it('makes up to 4 attempts (0s, 3s, 6s, 9s) and stops when nothing is found', async () => {
      vi.useFakeTimers();
      service.checkForActiveRun();

      await vi.advanceTimersByTimeAsync(1);
      await vi.advanceTimersByTimeAsync(3000);
      await vi.advanceTimersByTimeAsync(3000);
      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getActiveRuns).toHaveBeenCalledTimes(4);

      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getActiveRuns).toHaveBeenCalledTimes(4); // no 5th attempt
    });

    it('begins tracking a running run and stops polling for more', async () => {
      vi.useFakeTimers();
      api.getActiveRuns.mockReturnValue(of({ runs: [baseRunStatus] }));
      api.streamRunStatus.mockReturnValue(new Subject<StrategyLabStreamEvent>());

      service.checkForActiveRun();
      await vi.advanceTimersByTimeAsync(1);

      expect(service.running()).toBe(true);
      expect(service.activeRunId()).toBe('run-1');
      expect(service.runStatus()).toEqual(baseRunStatus);
      expect(api.streamRunStatus).toHaveBeenCalledWith('run-1');

      await vi.advanceTimersByTimeAsync(9000);
      expect(api.getActiveRuns).toHaveBeenCalledTimes(1);
    });

    it('ignores runs that are not status "running"', async () => {
      vi.useFakeTimers();
      api.getActiveRuns.mockReturnValue(of({ runs: [{ ...baseRunStatus, status: 'completed' }] }));

      service.checkForActiveRun();
      await vi.advanceTimersByTimeAsync(1);

      expect(service.running()).toBe(false);
    });

    it('stops early if running() becomes true independently', async () => {
      vi.useFakeTimers();
      api.streamRunStatus.mockReturnValue(new Subject<StrategyLabStreamEvent>());
      service.checkForActiveRun();
      await vi.advanceTimersByTimeAsync(1); // first (t=0) attempt fires, finds nothing

      service.startRun('run-2', baseRunStatus); // e.g. the user started a run locally

      await vi.advanceTimersByTimeAsync(9000); // t=3000, 6000, 9000 would otherwise fire
      expect(api.getActiveRuns).toHaveBeenCalledTimes(1); // no further attempts once running
    });
  });

  describe('startRun / SSE stream', () => {
    it('sets state immediately and connects the stream', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);

      service.startRun('run-1', baseRunStatus);

      expect(service.running()).toBe(true);
      expect(service.activeRunId()).toBe('run-1');
      expect(service.runStatus()).toEqual(baseRunStatus);
      expect(api.streamRunStatus).toHaveBeenCalledWith('run-1');
    });

    it('folds incoming events via reduce() and emits every event on events$', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      const seen: StrategyLabStreamEvent[] = [];
      service.startRun('run-1', baseRunStatus);
      service.events$.subscribe((e) => seen.push(e));

      sse.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });

      expect(service.runStatus()?.completed_cycles).toBe(1);
      expect(seen).toEqual([
        { type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 },
      ]);
    });

    it('finishes the run on a "complete" event', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({
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
      expect(service.activeRunId()).toBeNull();
      expect(service.runStatus()).toBeNull();
    });

    it('captures the true status into lastTerminalStatus on a "complete" event, not the stale initial "running"', () => {
      // Regression: the reducer's 'complete' case used to be a no-op for
      // `status`, so a run whose SSE connection stayed open its whole
      // lifetime (no 'snapshot' event to update it) would have finishRun()
      // capture the stale initial 'running' value into lastTerminalStatus.
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({
        type: 'complete',
        message: 'done',
        status: 'completed_with_errors',
        completed_count: 3,
        skipped_count: 0,
        errored_count: 2,
        errored_details: [],
        completed_batches: 1,
        total_batches: 1,
      });

      expect(service.lastTerminalStatus()?.status).toBe('completed_with_errors');
    });

    it('finishes the run on an "error" event', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({ type: 'error', detail: 'boom' });

      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
    });

    it('captures "failed" into lastTerminalStatus on an "error" event, not the stale initial "running"', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({ type: 'error', detail: 'boom' });

      expect(service.lastTerminalStatus()?.status).toBe('failed');
    });

    it('finishes the run on a "cancelled" event', () => {
      // Regression: 'cancelled' is a distinct terminal event type (not
      // folded into 'error'), so handleStreamEvent()'s finishRun() trigger
      // must explicitly include it — otherwise a cancelled run would never
      // clear running()/runStatus() or unsubscribe its SSE connection.
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({ type: 'cancelled', detail: 'Run cancelled by user' });

      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
    });

    it('captures "cancelled" into lastTerminalStatus on a "cancelled" event, not the stale initial "running"', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({ type: 'cancelled', detail: 'Run cancelled by user' });

      expect(service.lastTerminalStatus()?.status).toBe('cancelled');
    });

    it('finishes the run when the stream completes (the "done" sentinel)', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.complete();

      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
    });

    it('captures the terminal snapshot into lastTerminalStatus before the stream completes with no explicit complete/error event', () => {
      // A reconnect to an already-finished run: the backend sends a terminal
      // `snapshot` (no distinct complete/error event replays for a
      // reconnect), then closes the stream — reduce()'s 'snapshot' case folds
      // the terminal status into runStatus, and finishRun() must capture that
      // exact value into lastTerminalStatus before nulling runStatus, since
      // nothing else observing this component ever sees it otherwise.
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      sse.next({
        type: 'snapshot',
        run_id: 'run-1',
        status: 'failed',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 5,
        completed_cycles: 2,
        skipped_cycles: 0,
        completed_record_ids: [],
        error: null,
      });
      sse.next({ type: 'done' });
      sse.complete();

      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
      expect(service.lastTerminalStatus()?.status).toBe('failed');
      expect(service.lastTerminalStatus()?.completed_cycles).toBe(2);
    });

    it('falls back to REST polling when the stream errors', async () => {
      vi.useFakeTimers();
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      api.getRunStatus.mockReturnValue(of({ ...baseRunStatus, completed_cycles: 2 }));
      service.startRun('run-1', baseRunStatus);

      sse.error(new Error('SSE connection lost'));
      await vi.advanceTimersByTimeAsync(1);

      expect(api.getRunStatus).toHaveBeenCalledWith('run-1');
      expect(service.runStatus()?.completed_cycles).toBe(2);
      expect(service.running()).toBe(true);
    });

    it('stops reacting to the stream once destroyed', () => {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);

      service.ngOnDestroy();
      sse.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 99, batch_index: 1 });

      expect(service.runStatus()?.completed_cycles).not.toBe(99);
    });
  });

  describe('fallbackToPolling (after an SSE drop)', () => {
    function triggerFallback(): void {
      const sse = new Subject<StrategyLabStreamEvent>();
      api.streamRunStatus.mockReturnValue(sse);
      service.startRun('run-1', baseRunStatus);
      sse.error(new Error('lost'));
    }

    it('polls every 5s starting immediately and stops once the status is terminal', async () => {
      vi.useFakeTimers();
      api.getRunStatus
        .mockReturnValueOnce(of({ ...baseRunStatus, status: 'running', completed_cycles: 1 }))
        .mockReturnValueOnce(of({ ...baseRunStatus, status: 'completed', completed_cycles: 5 }));
      triggerFallback();

      await vi.advanceTimersByTimeAsync(1);
      expect(api.getRunStatus).toHaveBeenCalledTimes(1);
      expect(service.runStatus()?.completed_cycles).toBe(1);
      expect(service.running()).toBe(true);

      await vi.advanceTimersByTimeAsync(5000);
      expect(api.getRunStatus).toHaveBeenCalledTimes(2);
      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
      // No complete/error stream event ever reached the caller on this path —
      // lastTerminalStatus is the only record of how the run actually ended.
      expect(service.lastTerminalStatus()?.status).toBe('completed');
      expect(service.lastTerminalStatus()?.completed_cycles).toBe(5);

      await vi.advanceTimersByTimeAsync(5000);
      expect(api.getRunStatus).toHaveBeenCalledTimes(2); // no further polling
    });

    it('clears runStatus so lastTerminalStatus reads null when polling itself errors', async () => {
      // The run's actual fate is unknown here (still 'running' is the last
      // value takeWhile let through, not a real terminal status) — capturing
      // it into lastTerminalStatus as-is would let a caller mistake a lost
      // connection for a successful completion. null is the deliberate
      // "we don't know" signal instead.
      vi.useFakeTimers();
      api.getRunStatus.mockReturnValue(throwError(() => new Error('network')));
      triggerFallback();

      await vi.advanceTimersByTimeAsync(1);

      expect(service.running()).toBe(false);
      expect(service.runStatus()).toBeNull();
      expect(service.lastTerminalStatus()).toBeNull();
    });

    it('stops polling once destroyed', async () => {
      vi.useFakeTimers();
      api.getRunStatus.mockReturnValue(of({ ...baseRunStatus, status: 'running' }));
      triggerFallback();
      await vi.advanceTimersByTimeAsync(1);
      expect(api.getRunStatus).toHaveBeenCalledTimes(1);

      service.ngOnDestroy();
      api.getRunStatus.mockClear();
      await vi.advanceTimersByTimeAsync(10000);

      expect(api.getRunStatus).not.toHaveBeenCalled();
    });
  });

  describe('paper trading session polling', () => {
    it('trackPaperTradingSession stores the session, sets paperTradingLabRecordId, and polls starting at 3s', async () => {
      vi.useFakeTimers();
      const updated = { ...runningSession, current_capital: 10500 };
      api.getPaperTradingSession.mockReturnValue(of({ session: updated, message: 'ok' } as PaperTradingResponse));

      service.trackPaperTradingSession('rec-1', runningSession);

      expect(service.paperTradingSessions()['rec-1']).toEqual(runningSession);
      expect(service.paperTradingLabRecordId()).toBe('rec-1');
      expect(api.getPaperTradingSession).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(3000);

      expect(api.getPaperTradingSession).toHaveBeenCalledWith('pt-1');
      expect(service.paperTradingSessions()['rec-1'].current_capital).toBe(10500);
    });

    it('keeps polling through the live-mode opening/warming_up/live states, not just "running"', async () => {
      vi.useFakeTimers();
      const openingSession: PaperTradingSession = { ...runningSession, status: 'opening' };
      api.getPaperTradingSession
        .mockReturnValueOnce(of({ session: { ...runningSession, status: 'opening' }, message: 'ok' } as PaperTradingResponse))
        .mockReturnValueOnce(of({ session: { ...runningSession, status: 'warming_up' }, message: 'ok' } as PaperTradingResponse))
        .mockReturnValueOnce(of({ session: { ...runningSession, status: 'live' }, message: 'ok' } as PaperTradingResponse))
        .mockReturnValueOnce(of({ session: { ...runningSession, status: 'completed' }, message: 'ok' } as PaperTradingResponse));

      service.trackPaperTradingSession('rec-1', openingSession);

      // First poll response is 'opening' — an inclusive-but-wrong takeWhile
      // predicate keyed on 'running' would stop right here.
      await vi.advanceTimersByTimeAsync(3000);
      expect(service.paperTradingSessions()['rec-1'].status).toBe('opening');
      expect(service.paperTradingLabRecordId()).toBe('rec-1');

      await vi.advanceTimersByTimeAsync(3000);
      expect(service.paperTradingSessions()['rec-1'].status).toBe('warming_up');
      expect(service.paperTradingLabRecordId()).toBe('rec-1');

      await vi.advanceTimersByTimeAsync(3000);
      expect(service.paperTradingSessions()['rec-1'].status).toBe('live');
      expect(service.paperTradingLabRecordId()).toBe('rec-1');

      await vi.advanceTimersByTimeAsync(3000);
      expect(service.paperTradingSessions()['rec-1'].status).toBe('completed');
      expect(service.paperTradingLabRecordId()).toBeNull();

      expect(api.getPaperTradingSession).toHaveBeenCalledTimes(4);
    });

    it('hydratePaperTradingSessions resumes polling for opening/warming_up/live sessions, not just "running"', async () => {
      vi.useFakeTimers();
      const openingSession: PaperTradingSession = {
        ...runningSession,
        session_id: 'pt-opening',
        lab_record_id: 'rec-opening',
        status: 'opening',
      };
      api.getPaperTradingSession.mockReturnValue(
        of({ session: { ...openingSession, status: 'warming_up' }, message: 'ok' } as PaperTradingResponse),
      );

      service.hydratePaperTradingSessions({ 'rec-opening': openingSession });

      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getPaperTradingSession).toHaveBeenCalledWith('pt-opening');
      expect(service.paperTradingSessions()['rec-opening'].status).toBe('warming_up');
    });

    it('clears paperTradingLabRecordId and stops polling once the session reaches a terminal status', async () => {
      vi.useFakeTimers();
      api.getPaperTradingSession.mockReturnValue(
        of({ session: { ...runningSession, status: 'completed' }, message: 'ok' } as PaperTradingResponse),
      );

      service.trackPaperTradingSession('rec-1', runningSession);
      await vi.advanceTimersByTimeAsync(3000);

      expect(service.paperTradingLabRecordId()).toBeNull();
      expect(service.paperTradingSessions()['rec-1'].status).toBe('completed');

      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getPaperTradingSession).toHaveBeenCalledTimes(1);
    });

    it('emits a message on errors$ and clears paperTradingLabRecordId when polling fails', async () => {
      vi.useFakeTimers();
      api.getPaperTradingSession.mockReturnValue(throwError(() => ({ error: { detail: 'session lost' } })));
      const errors: string[] = [];
      service.errors$.subscribe((e) => errors.push(e));

      service.trackPaperTradingSession('rec-1', runningSession);
      await vi.advanceTimersByTimeAsync(3000);

      expect(errors).toEqual(['session lost']);
      expect(service.paperTradingLabRecordId()).toBeNull();
    });

    it('falls back to err.message, then a generic message, when the error has no HTTP body', async () => {
      vi.useFakeTimers();
      api.getPaperTradingSession.mockReturnValueOnce(throwError(() => new Error('network down')));
      const errors: string[] = [];
      service.errors$.subscribe((e) => errors.push(e));

      service.trackPaperTradingSession('rec-1', runningSession);
      await vi.advanceTimersByTimeAsync(3000);

      api.getPaperTradingSession.mockReturnValueOnce(throwError(() => ({})));
      service.trackPaperTradingSession('rec-2', { ...runningSession, session_id: 'pt-2', lab_record_id: 'rec-2' });
      await vi.advanceTimersByTimeAsync(3000);

      expect(errors).toEqual(['network down', 'Paper trading polling failed.']);
    });

    it('clearPaperTradingSessions drops all known sessions', () => {
      service.trackPaperTradingSession('rec-1', runningSession);
      expect(service.paperTradingSessions()).toEqual({ 'rec-1': runningSession });

      service.clearPaperTradingSessions();

      expect(service.paperTradingSessions()).toEqual({});
    });

    it('hydratePaperTradingSessions adopts a batch, resumes polling only for running ones, and never sets paperTradingLabRecordId', async () => {
      vi.useFakeTimers();
      const completedSession: PaperTradingSession = {
        ...runningSession,
        session_id: 'pt-2',
        lab_record_id: 'rec-2',
        status: 'completed',
      };
      api.getPaperTradingSession.mockReturnValue(of({ session: runningSession, message: 'ok' } as PaperTradingResponse));

      service.hydratePaperTradingSessions({ 'rec-1': runningSession, 'rec-2': completedSession });

      expect(service.paperTradingSessions()).toEqual({ 'rec-1': runningSession, 'rec-2': completedSession });
      expect(service.paperTradingLabRecordId()).toBeNull();

      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getPaperTradingSession).toHaveBeenCalledTimes(1);
      expect(api.getPaperTradingSession).toHaveBeenCalledWith('pt-1');
    });

    it('polls two tracked records independently', async () => {
      vi.useFakeTimers();
      const session2: PaperTradingSession = { ...runningSession, session_id: 'pt-2', lab_record_id: 'rec-2' };
      api.getPaperTradingSession.mockImplementation((sessionId: string) =>
        of({ session: sessionId === 'pt-1' ? runningSession : session2, message: 'ok' } as PaperTradingResponse),
      );

      service.trackPaperTradingSession('rec-1', runningSession);
      service.trackPaperTradingSession('rec-2', session2);
      await vi.advanceTimersByTimeAsync(3000);

      expect(api.getPaperTradingSession).toHaveBeenCalledWith('pt-1');
      expect(api.getPaperTradingSession).toHaveBeenCalledWith('pt-2');
      expect(service.paperTradingSessions()['rec-1']).toEqual(runningSession);
      expect(service.paperTradingSessions()['rec-2']).toEqual(session2);
    });

    it('stops all paper-trading polls once destroyed', async () => {
      vi.useFakeTimers();
      api.getPaperTradingSession.mockReturnValue(of({ session: runningSession, message: 'ok' } as PaperTradingResponse));
      service.trackPaperTradingSession('rec-1', runningSession);
      await vi.advanceTimersByTimeAsync(3000);
      expect(api.getPaperTradingSession).toHaveBeenCalledTimes(1);

      service.ngOnDestroy();
      api.getPaperTradingSession.mockClear();
      await vi.advanceTimersByTimeAsync(10000);

      expect(api.getPaperTradingSession).not.toHaveBeenCalled();
    });
  });

  describe('ngOnDestroy', () => {
    it('completes events$ and errors$', () => {
      let eventsCompleted = false;
      let errorsCompleted = false;
      service.events$.subscribe({ complete: () => { eventsCompleted = true; } });
      service.errors$.subscribe({ complete: () => { errorsCompleted = true; } });

      service.ngOnDestroy();

      expect(eventsCompleted).toBe(true);
      expect(errorsCompleted).toBe(true);
    });

    it('is idempotent', () => {
      expect(() => {
        service.ngOnDestroy();
        service.ngOnDestroy();
      }).not.toThrow();
    });
  });
});
