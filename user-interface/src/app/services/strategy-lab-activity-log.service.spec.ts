import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrategyLabActivityLogService } from './strategy-lab-activity-log.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { createRunServiceStub, type RunServiceStub } from '../testing/strategy-lab-run-service.stub';
import type { StrategyLabRunStatus, StrategyLabStreamEvent } from '../models';

describe('StrategyLabActivityLogService', () => {
  let service: StrategyLabActivityLogService;
  let runService: RunServiceStub;

  const baseRunStatus: StrategyLabRunStatus = {
    run_id: 'run-1',
    status: 'running',
    started_at: '2026-01-01T00:00:00Z',
    total_cycles: 5,
    completed_cycles: 0,
    skipped_cycles: 0,
    completed_record_ids: [],
  };

  beforeEach(() => {
    runService = createRunServiceStub();
    TestBed.configureTestingModule({
      providers: [
        StrategyLabActivityLogService,
        { provide: StrategyLabRunService, useValue: runService },
      ],
    });
    service = TestBed.inject(StrategyLabActivityLogService);
  });

  afterEach(() => {
    service.ngOnDestroy();
    vi.useRealTimers();
  });

  it('starts with empty state', () => {
    expect(service.activityLog()).toEqual([]);
    expect(service.completionWarning()).toBeNull();
    expect(service.runOutcomeAnnouncement()).toBeNull();
    expect(service.terminalErrorBanner).toBeNull();
  });

  describe('progress', () => {
    it('adds an activity-log entry and resets the log on a new cycle_index, but only when runStatus is set', () => {
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
      expect(service.activityLog()).toEqual([]); // no runStatus yet — guarded no-op

      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
      expect(service.activityLog()).toEqual([
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
      const log = service.activityLog();
      expect(log).toHaveLength(2);
      expect(log[0].status).toBe('done');
      expect(log[1]).toEqual({
        time: expect.any(String),
        status: 'done',
        message: 'Strategy ideated — stocks asset class',
      });

      runService.events$.next({ type: 'progress', cycle_index: 1, phase: 'coding', sub_phase: 'started' });
      // New cycle_index: log resets to just the new entry.
      expect(service.activityLog()).toEqual([
        { time: expect.any(String), status: 'active', message: 'Validating strategy spec and code safety...' },
      ]);
    });
  });

  describe('cycle_complete', () => {
    it('resets the activity log and requests a results refresh, only when runStatus is set', () => {
      const refreshes: void[] = [];
      service.resultsRefreshRequested$.subscribe(() => refreshes.push(undefined));

      runService.events$.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });
      expect(refreshes).toHaveLength(0); // guarded no-op

      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating' });
      runService.events$.next({ type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 });

      expect(service.activityLog()).toEqual([]);
      expect(refreshes).toHaveLength(1);
    });
  });

  describe('batch_warning', () => {
    it('does not set a warning when runStatus is unset (guarded no-op)', () => {
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' });
      expect(service.completionWarning()).toBeNull();
    });

    it('shows the specific message for a signal-brief failure', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' });
      expect(service.completionWarning()).toBe(
        'Signal brief unavailable for a batch; strategies continued without it.',
      );
    });

    it('shows the reason text for any other non-empty reason', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: 'disk_full' });
      expect(service.completionWarning()).toBe('disk_full');
    });

    it('falls back to a generic message for an empty reason', () => {
      runService.runStatus.set(baseRunStatus);
      runService.events$.next({ type: 'batch_warning', batch_index: 1, reason: '' });
      expect(service.completionWarning()).toBe('A non-fatal warning occurred during a batch.');
    });
  });

  describe('complete', () => {
    it('sets a completion warning when cycles errored', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 4, skipped_count: 0, errored_count: 1, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(service.completionWarning()).toBe('Run finished with 1 cycle(s) errored. See details below.');
    });

    it('sets a completion warning combining errored and skipped counts', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed_with_errors',
        completed_count: 3, skipped_count: 2, errored_count: 1, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(service.completionWarning()).toBe('Run finished with 1 cycle(s) errored and 2 cycle(s) skipped. See details below.');
    });

    it('sets no completion warning for a clean finish', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 5, skipped_count: 0, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(service.completionWarning()).toBeNull();
    });

    it('sets no completion warning for a skip-only, zero-error finish (the sighted banner stays scoped to genuine errors)', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 3, skipped_count: 2, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(service.completionWarning()).toBeNull();
    });

    it('sets the run-outcome announcement from the event data', () => {
      runService.events$.next({
        type: 'complete', message: 'done', status: 'completed',
        completed_count: 5, skipped_count: 0, errored_count: 0, errored_details: [],
        completed_batches: 1, total_batches: 1,
      });
      expect(service.runOutcomeAnnouncement()).toBe('Strategy Lab run complete.');
    });
  });

  describe('error', () => {
    it('surfaces the detail message on terminalError$ and terminalErrorBanner', () => {
      const errors: string[] = [];
      service.terminalError$.subscribe((message) => errors.push(message));

      runService.events$.next({ type: 'error', detail: 'Sandbox crashed' });

      expect(errors).toEqual(['Sandbox crashed']);
      expect(service.terminalErrorBanner).toBe('Sandbox crashed');
    });

    it('falls back to a connection-lost message, not "Run failed", for the shared-infra reclaim shape', () => {
      // Regression: a subscription-reclaim event (only .error, never
      // .detail — a connection-level event, not necessarily a job failure)
      // used to fall through to the generic "Run failed" default, wrongly
      // announcing a definite failure for what may just be a reconnectable
      // connection loss.
      const errors: string[] = [];
      service.terminalError$.subscribe((message) => errors.push(message));

      runService.events$.next({ type: 'error', error: 'subscription reclaimed' });

      expect(errors).toEqual(['Strategy Lab lost track of the run — status unavailable.']);
      expect(service.runOutcomeAnnouncement()).toBe('Strategy Lab lost track of the run — status unavailable.');
    });

    it('shows the external-stop detail in the error banner for an interrupted run', () => {
      const errors: string[] = [];
      service.terminalError$.subscribe((message) => errors.push(message));

      runService.events$.next({
        type: 'error',
        detail: 'Run was marked interrupted externally.',
        terminal_status: 'interrupted',
      });

      expect(errors).toEqual(['Run was marked interrupted externally.']);
      expect(service.runOutcomeAnnouncement()).toBe('Strategy Lab run interrupted.');
    });
  });

  describe('cancelled', () => {
    it('surfaces a cancellation in the non-error warning banner, not the red error banner', () => {
      const errors: string[] = [];
      service.terminalError$.subscribe((message) => errors.push(message));

      runService.events$.next({ type: 'cancelled', detail: 'Run cancelled by user' });

      expect(service.completionWarning()).toBe('Run cancelled by user');
      expect(errors).toEqual([]);
    });

    it('falls back to a default cancellation notice when detail is empty', () => {
      runService.events$.next({ type: 'cancelled', detail: '' });
      expect(service.completionWarning()).toBe('Run cancelled by user.');
    });
  });

  it('does nothing for a done event', () => {
    runService.runStatus.set(baseRunStatus);
    runService.events$.next({ type: 'done' });
    expect(service.activityLog()).toEqual([]);
    expect(service.completionWarning()).toBeNull();
    expect(service.terminalErrorBanner).toBeNull();
  });

  it('covers coding/backtesting/analyzing sub-phases via addLogEntry', () => {
    runService.runStatus.set({
      run_id: 'run-1', status: 'running', started_at: '', total_cycles: 4,
      completed_cycles: 0, skipped_cycles: 0, completed_record_ids: [],
    });
    const push = (event: StrategyLabStreamEvent) => runService.events$.next(event);
    const messages = (): string[] => service.activityLog().map((e) => e.message);

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

  it('cancels a pending auto-scroll timer on destroy', () => {
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');
    // Simulate a scroll timer left pending when the service is torn down.
    (service as unknown as { autoScrollTimeoutId: ReturnType<typeof setTimeout> | null })
      .autoScrollTimeoutId = setTimeout(() => undefined, 10_000);

    service.ngOnDestroy();

    expect(clearSpy).toHaveBeenCalled();
    expect(
      (service as unknown as { autoScrollTimeoutId: ReturnType<typeof setTimeout> | null })
        .autoScrollTimeoutId,
    ).toBeNull();
    clearSpy.mockRestore();
  });

  it('requests a scroll after the debounced auto-scroll timer fires', () => {
    vi.useFakeTimers();
    const scrolls: void[] = [];
    service.scrollRequested$.subscribe(() => scrolls.push(undefined));

    runService.runStatus.set(baseRunStatus);
    runService.events$.next({ type: 'progress', cycle_index: 0, phase: 'ideating', sub_phase: 'started' });
    expect(scrolls).toHaveLength(0);

    vi.advanceTimersByTime(50);
    expect(scrolls).toHaveLength(1);
  });
});
