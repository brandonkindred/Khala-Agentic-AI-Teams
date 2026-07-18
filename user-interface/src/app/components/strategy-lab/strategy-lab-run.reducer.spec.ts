import { reduce } from './strategy-lab-run.reducer';
import type { StrategyLabErroredDetail, StrategyLabRunStatus, StrategyLabStreamEvent } from '../../models';

function baseState(overrides: Partial<StrategyLabRunStatus> = {}): StrategyLabRunStatus {
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

describe('strategy-lab-run.reducer', () => {
  describe('no-op event types', () => {
    const noOpEvents: [string, StrategyLabStreamEvent][] = [
      ['batch_warning', { type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' }],
      [
        'complete',
        {
          type: 'complete',
          message: 'done',
          status: 'completed',
          completed_count: 5,
          skipped_count: 0,
          errored_count: 0,
          errored_details: [],
          completed_batches: 1,
          total_batches: 1,
        },
      ],
      ['error (detail variant)', { type: 'error', detail: 'boom' }],
      ['error (reclaim variant)', { type: 'error', error: 'reclaimed' }],
      ['done', { type: 'done' }],
    ];

    it.each(noOpEvents)('returns the same state reference for %s', (_label, event) => {
      const state = baseState();
      expect(reduce(state, event)).toBe(state);
    });

    it.each(noOpEvents)('returns null unchanged for %s when state is already null', (_label, event) => {
      expect(reduce(null, event)).toBeNull();
    });
  });

  describe('null-state preservation for run-scoped events', () => {
    const guardedEvents: [string, StrategyLabStreamEvent][] = [
      [
        'snapshot',
        {
          type: 'snapshot',
          run_id: 'run-1',
          started_at: '2026-01-01T00:00:00Z',
          total_cycles: 10,
          completed_cycles: 3,
          skipped_cycles: 1,
          completed_record_ids: ['rec-1'],
          status: 'running',
          error: null,
        },
      ],
      ['batch_start', { type: 'batch_start', batch_index: 1, total_batches: 2, batch_size: 5, completed_batches: 0 }],
      ['batch_complete', { type: 'batch_complete', batch_index: 1, total_batches: 2, completed_batches: 1 }],
      ['progress', { type: 'progress', cycle_index: 0, phase: 'ideating' }],
      [
        'cycle_complete',
        { type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 0 },
      ],
      ['cycle_skipped', { type: 'cycle_skipped', cycle_index: 0, reason: 'no_market_data', batch_index: 0 }],
      [
        'cycle_errored',
        { type: 'cycle_errored', cycle_index: 0, batch_index: 0, reason: 'ValueError', error: 'bad data' },
      ],
    ];

    it.each(guardedEvents)('returns null for %s when state is null', (_label, event) => {
      expect(reduce(null, event)).toBeNull();
    });
  });

  describe('no mutation', () => {
    it('does not mutate a frozen state for a snapshot event', () => {
      const state: StrategyLabRunStatus = baseState();
      Object.freeze(state);
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: 'run-1',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 10,
        completed_cycles: 5,
        skipped_cycles: 1,
        completed_record_ids: ['rec-1', 'rec-2'],
        status: 'running',
        error: null,
      };

      expect(() => reduce(state, event)).not.toThrow();
      expect(state.completed_cycles).toBe(2);
      expect(reduce(state, event)).not.toBe(state);
    });

    it('does not mutate a frozen state for a progress event', () => {
      const state: StrategyLabRunStatus = baseState();
      Object.freeze(state);
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 1, phase: 'coding' };

      expect(() => reduce(state, event)).not.toThrow();
      expect(state.current_cycle).toBeUndefined();
    });

    it('does not mutate a frozen errored_details array when appending', () => {
      const existing: StrategyLabErroredDetail[] = [{ cycle_index: 0, error: 'e0' }];
      Object.freeze(existing);
      const state = baseState({ errored_details: existing });
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 1,
        batch_index: 0,
        reason: 'ValueError',
        error: 'boom',
      };

      expect(() => reduce(state, event)).not.toThrow();
      expect(existing).toHaveLength(1);
    });
  });

  describe('snapshot', () => {
    it('merges all fields from the event', () => {
      const state = baseState({ errored_cycles: 0, batch_size: 1, batch_count: 1, completed_batches: 0, current_batch: 1 });
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: 'run-1',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 10,
        completed_cycles: 7,
        skipped_cycles: 2,
        errored_cycles: 1,
        errored_details: [{ cycle_index: 3, error: 'boom' }],
        current_cycle: { cycle_index: 7, phase: 'coding', strategy: null, metrics: null },
        completed_record_ids: ['rec-1', 'rec-2'],
        error: 'partial failure',
        batch_size: 5,
        batch_count: 3,
        completed_batches: 2,
        current_batch: 3,
        status: 'completed_with_errors',
      };

      const result = reduce(state, event);

      expect(result).toEqual({
        run_id: 'run-1',
        status: 'completed_with_errors',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 10,
        completed_cycles: 7,
        skipped_cycles: 2,
        errored_cycles: 1,
        errored_details: [{ cycle_index: 3, error: 'boom' }],
        current_cycle: { cycle_index: 7, phase: 'coding', strategy: null, metrics: null },
        completed_record_ids: ['rec-1', 'rec-2'],
        error: 'partial failure',
        batch_size: 5,
        batch_count: 3,
        completed_batches: 2,
        current_batch: 3,
      });
    });

    it('falls back to prior state for each absent optional field, including explicit nulls', () => {
      const state = baseState({
        errored_cycles: 4,
        errored_details: [{ cycle_index: 1, error: 'prior' }],
        current_cycle: { cycle_index: 1, phase: 'coding' },
        error: 'prior error',
        batch_size: 5,
        batch_count: 2,
        completed_batches: 1,
        current_batch: 2,
      });
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: 'run-1',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 10,
        completed_cycles: 3,
        skipped_cycles: 0,
        completed_record_ids: ['rec-1'],
        status: 'running',
        error: null,
      };

      const result = reduce(state, event);

      expect(result?.errored_cycles).toBe(4);
      expect(result?.errored_details).toEqual([{ cycle_index: 1, error: 'prior' }]);
      expect(result?.current_cycle).toEqual({ cycle_index: 1, phase: 'coding' });
      expect(result?.error).toBe('prior error');
      expect(result?.batch_size).toBe(5);
      expect(result?.batch_count).toBe(2);
      expect(result?.completed_batches).toBe(1);
      expect(result?.current_batch).toBe(2);
    });

    it('passes through an interrupted status not present in StrategyLabRunStatus.status', () => {
      const state = baseState();
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: 'run-1',
        started_at: '2026-01-01T00:00:00Z',
        total_cycles: 10,
        completed_cycles: 2,
        skipped_cycles: 0,
        completed_record_ids: ['rec-1'],
        status: 'interrupted',
        error: null,
      };

      const status: string = reduce(state, event)!.status;
      expect(status).toBe('interrupted');
    });
  });

  describe('progress', () => {
    it('builds a new current_cycle from all provided fields', () => {
      const state = baseState();
      const event: StrategyLabStreamEvent = {
        type: 'progress',
        cycle_index: 1,
        phase: 'backtesting',
        sub_phase: 'running_code',
        refinement_round: 2,
        strategy: { asset_class: 'stocks', hypothesis: 'momentum' },
        metrics: { total_return_pct: 12.5 },
        checks_passed: 4,
        checks_total: 5,
        symbols_count: 10,
        bars_count: 500,
        trades_count: 20,
        execution_time: 3.2,
        is_winning: true,
      };

      const result = reduce(state, event);

      expect(result?.current_cycle).toEqual({
        cycle_index: 1,
        phase: 'backtesting',
        sub_phase: 'running_code',
        refinement_round: 2,
        strategy: { asset_class: 'stocks', hypothesis: 'momentum' },
        metrics: { total_return_pct: 12.5 },
        checks_passed: 4,
        checks_total: 5,
        symbols_count: 10,
        bars_count: 500,
        trades_count: 20,
        execution_time: 3.2,
        is_winning: true,
      });
    });

    it('falls back to the previous cycle strategy and metrics when the event omits them', () => {
      const state = baseState({
        current_cycle: {
          cycle_index: 0,
          phase: 'ideating',
          strategy: { asset_class: 'crypto', hypothesis: 'mean reversion' },
          metrics: { sharpe_ratio: 1.2 },
        },
      });
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 0, phase: 'coding' };

      const result = reduce(state, event);

      expect(result?.current_cycle?.strategy).toEqual({ asset_class: 'crypto', hypothesis: 'mean reversion' });
      expect(result?.current_cycle?.metrics).toEqual({ sharpe_ratio: 1.2 });
    });

    it('passes through a phase string outside the legacy 5-value UI enum', () => {
      const state = baseState();
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 0, phase: 'designing' };

      expect(reduce(state, event)?.current_cycle?.phase).toBe('designing');
    });
  });

  describe('cycle_complete', () => {
    it('sets completed_cycles from the event and clears current_cycle', () => {
      const state = baseState({ completed_cycles: 2, current_cycle: { cycle_index: 2, phase: 'analyzing' } });
      const event: StrategyLabStreamEvent = {
        type: 'cycle_complete',
        cycle_index: 2,
        record_id: 'rec-3',
        completed_cycles: 3,
        batch_index: 0,
      };

      const result = reduce(state, event);

      expect(result?.completed_cycles).toBe(3);
      expect(result?.current_cycle).toBeUndefined();
    });
  });

  describe('cycle_skipped', () => {
    it('increments skipped_cycles and clears current_cycle', () => {
      const state = baseState({ skipped_cycles: 1, current_cycle: { cycle_index: 1, phase: 'backtesting' } });
      const event: StrategyLabStreamEvent = {
        type: 'cycle_skipped',
        cycle_index: 1,
        reason: 'no_market_data',
        batch_index: 0,
      };

      const result = reduce(state, event);

      expect(result?.skipped_cycles).toBe(2);
      expect(result?.current_cycle).toBeUndefined();
    });
  });

  describe('cycle_errored', () => {
    it('increments errored_cycles, carries reason through as-is, and clears current_cycle', () => {
      const state = baseState({ errored_cycles: 1, current_cycle: { cycle_index: 1, phase: 'backtesting' } });
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 1,
        batch_index: 0,
        reason: 'ValueError',
        error: 'bad data',
      };

      const result = reduce(state, event);

      expect(result?.errored_cycles).toBe(2);
      expect(result?.errored_details).toEqual([
        { cycle_index: 1, batch_index: 0, error: 'bad data', reason: 'ValueError' },
      ]);
      expect(result?.current_cycle).toBeUndefined();
    });

    it('preserves the tracker_merge_failed marker under `reason` so downstream double-count detection matches', () => {
      // Regression: a prior version of this reducer stored event.reason
      // under detail.exception_type, so a live-streamed tracker-merge
      // failure (which the backend marks with reason: 'tracker_merge_failed'
      // both on the wire and in its own stored errored_details) never
      // matched code that specifically checks detail.reason.
      const state = baseState();
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 2,
        batch_index: 0,
        reason: 'tracker_merge_failed',
        error: 'merge boom',
      };

      const result = reduce(state, event);

      expect(result?.errored_details?.[0].reason).toBe('tracker_merge_failed');
    });

    it('initializes errored_cycles/errored_details when absent on prior state', () => {
      const state = baseState();
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 0,
        batch_index: 0,
        reason: 'KeyError',
        error: 'missing key',
      };

      const result = reduce(state, event);

      expect(result?.errored_cycles).toBe(1);
      expect(result?.errored_details).toHaveLength(1);
    });

    it('caps errored_details at 50 entries, dropping the oldest', () => {
      const existing: StrategyLabErroredDetail[] = Array.from({ length: 50 }, (_, i) => ({
        cycle_index: i,
        error: `error-${i}`,
      }));
      const state = baseState({ errored_details: existing });
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 50,
        batch_index: 0,
        reason: 'ValueError',
        error: 'error-50',
      };

      const result = reduce(state, event);

      expect(result?.errored_details).toHaveLength(50);
      expect(result?.errored_details?.[0].cycle_index).toBe(1);
      expect(result?.errored_details?.[49].cycle_index).toBe(50);
    });
  });

  describe('batch_start', () => {
    it('sets current_batch, batch_count, and completed_batches from the event', () => {
      const state = baseState({ current_batch: null, batch_count: 1, completed_batches: 0 });
      const event: StrategyLabStreamEvent = {
        type: 'batch_start',
        batch_index: 2,
        total_batches: 3,
        batch_size: 5,
        completed_batches: 1,
      };

      const result = reduce(state, event);

      expect(result?.current_batch).toBe(2);
      expect(result?.batch_count).toBe(3);
      expect(result?.completed_batches).toBe(1);
    });
  });

  describe('batch_complete', () => {
    it('sets completed_batches from the event and clears current_batch', () => {
      const state = baseState({ current_batch: 2, completed_batches: 1 });
      const event: StrategyLabStreamEvent = {
        type: 'batch_complete',
        batch_index: 2,
        total_batches: 3,
        completed_batches: 2,
      };

      const result = reduce(state, event);

      expect(result?.completed_batches).toBe(2);
      expect(result?.current_batch).toBeNull();
    });
  });

  describe('unrecognized event type (defensive default)', () => {
    it('returns state unchanged rather than throwing', () => {
      const state = baseState();
      const event = { type: 'some_future_event' } as unknown as StrategyLabStreamEvent;

      expect(reduce(state, event)).toBe(state);
    });
  });
});
