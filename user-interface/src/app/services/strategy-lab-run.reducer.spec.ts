import { describe, expect, it } from 'vitest';
import { reduce } from './strategy-lab-run.reducer';
import type { StrategyLabRunStatus, StrategyLabStreamEvent } from '../models';

const baseState: StrategyLabRunStatus = {
  run_id: 'run-1',
  status: 'running',
  started_at: '2026-01-01T00:00:00Z',
  total_cycles: 10,
  completed_cycles: 2,
  skipped_cycles: 0,
  errored_cycles: 0,
  errored_details: [],
  completed_record_ids: ['rec-1', 'rec-2'],
  batch_size: 5,
  batch_count: 2,
  completed_batches: 0,
  current_batch: 1,
};

describe('reduce (strategy-lab-run.reducer)', () => {
  describe('null state', () => {
    it('returns null unchanged for every event type that requires existing state', () => {
      const events: StrategyLabStreamEvent[] = [
        { type: 'snapshot', ...baseState, error: null },
        { type: 'progress', cycle_index: 0, phase: 'ideating' },
        { type: 'cycle_complete', cycle_index: 0, record_id: 'rec-1', completed_cycles: 1, batch_index: 1 },
        { type: 'cycle_skipped', cycle_index: 0, reason: 'no_market_data', batch_index: 1 },
        { type: 'cycle_errored', cycle_index: 0, batch_index: 1, reason: 'ValueError', error: 'boom' },
        { type: 'batch_start', batch_index: 1, total_batches: 2, batch_size: 5, completed_batches: 0 },
        { type: 'batch_complete', batch_index: 1, total_batches: 2, completed_batches: 1 },
      ];
      for (const event of events) {
        expect(reduce(null, event)).toBeNull();
      }
    });

    it('returns null unchanged for no-op event types', () => {
      const events: StrategyLabStreamEvent[] = [
        { type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' },
        {
          type: 'complete',
          message: 'done',
          status: 'completed',
          completed_count: 10,
          skipped_count: 0,
          errored_count: 0,
          errored_details: [],
          completed_batches: 2,
          total_batches: 2,
        },
        { type: 'error', detail: 'Run failed' },
        { type: 'done' },
      ];
      for (const event of events) {
        expect(reduce(null, event)).toBeNull();
      }
    });
  });

  describe('snapshot', () => {
    it('merges present fields and returns a new object', () => {
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: 'run-1',
        status: 'completed_with_errors',
        started_at: baseState.started_at,
        total_cycles: 10,
        completed_cycles: 4,
        skipped_cycles: 1,
        errored_cycles: 2,
        errored_details: [{ cycle_index: 3, error: 'oops' }],
        current_cycle: { cycle_index: 4, phase: 'backtesting' },
        completed_record_ids: ['rec-1', 'rec-2', 'rec-3', 'rec-4'],
        error: null,
        batch_size: 5,
        batch_count: 2,
        completed_batches: 1,
        current_batch: 2,
      };

      const result = reduce(baseState, event);

      expect(result).not.toBe(baseState);
      expect(result).toEqual({
        ...baseState,
        status: 'completed_with_errors',
        completed_cycles: 4,
        skipped_cycles: 1,
        errored_cycles: 2,
        errored_details: [{ cycle_index: 3, error: 'oops' }],
        current_cycle: { cycle_index: 4, phase: 'backtesting' },
        completed_record_ids: ['rec-1', 'rec-2', 'rec-3', 'rec-4'],
        error: undefined,
        completed_batches: 1,
        current_batch: 2,
      });
    });

    it('accepts the interrupted status (server-restart reclaim)', () => {
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        ...baseState,
        status: 'interrupted',
        error: null,
      };
      expect(reduce(baseState, event)?.status).toBe('interrupted');
    });

    it('falls back to prior values for optional fields the event omits', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        errored_cycles: 3,
        errored_details: [{ cycle_index: 1, error: 'x' }],
        batch_size: 7,
        batch_count: 3,
        completed_batches: 2,
        current_batch: 3,
      };
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        run_id: state.run_id,
        status: state.status,
        started_at: state.started_at,
        total_cycles: state.total_cycles,
        completed_cycles: state.completed_cycles,
        skipped_cycles: state.skipped_cycles,
        completed_record_ids: state.completed_record_ids,
        error: null,
        // errored_cycles/errored_details/batch_size/batch_count/completed_batches/
        // current_batch deliberately omitted.
      };

      const result = reduce(state, event);

      expect(result?.errored_cycles).toBe(3);
      expect(result?.errored_details).toEqual([{ cycle_index: 1, error: 'x' }]);
      expect(result?.batch_size).toBe(7);
      expect(result?.batch_count).toBe(3);
      expect(result?.completed_batches).toBe(2);
      expect(result?.current_batch).toBe(3);
    });

    it('a real null current_cycle/error on the wire does not clear the prior value', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: { cycle_index: 1, phase: 'coding' },
        error: 'previous error',
      };
      const event: StrategyLabStreamEvent = {
        type: 'snapshot',
        ...baseState,
        current_cycle: null,
        error: null,
      };

      const result = reduce(state, event);

      expect(result?.current_cycle).toEqual({ cycle_index: 1, phase: 'coding' });
      expect(result?.error).toBe('previous error');
    });

    it('does not mutate the input state object', () => {
      const state: StrategyLabRunStatus = { ...baseState };
      const frozen = Object.freeze({ ...state });
      const event: StrategyLabStreamEvent = { type: 'snapshot', ...baseState, completed_cycles: 9, error: null };

      expect(() => reduce(frozen, event)).not.toThrow();
      expect(frozen.completed_cycles).toBe(baseState.completed_cycles);
    });
  });

  describe('progress', () => {
    it('builds a new current_cycle from the event fields', () => {
      const event: StrategyLabStreamEvent = {
        type: 'progress',
        cycle_index: 2,
        phase: 'backtesting',
        sub_phase: 'running_code',
        checks_passed: 4,
        checks_total: 5,
        symbols_count: 3,
        bars_count: 500,
      };

      const result = reduce(baseState, event);

      expect(result).not.toBe(baseState);
      expect(result?.current_cycle).toEqual({
        cycle_index: 2,
        phase: 'backtesting',
        sub_phase: 'running_code',
        refinement_round: undefined,
        strategy: undefined,
        metrics: undefined,
        checks_passed: 4,
        checks_total: 5,
        symbols_count: 3,
        bars_count: 500,
        trades_count: undefined,
        execution_time: undefined,
        failure_phase: undefined,
        changes_made: undefined,
        is_winning: undefined,
      });
    });

    it('accepts an open-ended phase value beyond the 4-value UI stepper set', () => {
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 0, phase: 'designing' };
      expect(reduce(baseState, event)?.current_cycle?.phase).toBe('designing');
    });

    it('preserves the previous strategy/metrics when the new event omits them', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: {
          cycle_index: 2,
          phase: 'coding',
          strategy: { asset_class: 'stocks', hypothesis: 'breakout' },
          metrics: { sharpe_ratio: 1.2 },
        },
      };
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 2, phase: 'backtesting' };

      const result = reduce(state, event);

      expect(result?.current_cycle?.strategy).toEqual({ asset_class: 'stocks', hypothesis: 'breakout' });
      expect(result?.current_cycle?.metrics).toEqual({ sharpe_ratio: 1.2 });
    });

    it('overwrites strategy/metrics when the new event provides them', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: {
          cycle_index: 2,
          phase: 'ideating',
          strategy: { asset_class: 'stocks', hypothesis: 'old' },
        },
      };
      const event: StrategyLabStreamEvent = {
        type: 'progress',
        cycle_index: 2,
        phase: 'coding',
        strategy: { asset_class: 'crypto', hypothesis: 'new' },
        metrics: { annualized_return_pct: 12 },
      };

      const result = reduce(state, event);

      expect(result?.current_cycle?.strategy).toEqual({ asset_class: 'crypto', hypothesis: 'new' });
      expect(result?.current_cycle?.metrics).toEqual({ annualized_return_pct: 12 });
    });

    it('does not carry forward per-cycle fields the new event omits (no stale checks_passed etc.)', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: { cycle_index: 2, phase: 'coding', checks_passed: 5, checks_total: 5 },
      };
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 2, phase: 'backtesting' };

      const result = reduce(state, event);

      expect(result?.current_cycle?.checks_passed).toBeUndefined();
      expect(result?.current_cycle?.checks_total).toBeUndefined();
    });

    it('does not mutate the input state object', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: { cycle_index: 2, phase: 'coding' },
      };
      const event: StrategyLabStreamEvent = { type: 'progress', cycle_index: 2, phase: 'backtesting' };

      reduce(state, event);

      expect(state.current_cycle).toEqual({ cycle_index: 2, phase: 'coding' });
    });
  });

  describe('cycle_complete', () => {
    it('sets completed_cycles from the event and clears current_cycle', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        current_cycle: { cycle_index: 2, phase: 'analyzing' },
      };
      const event: StrategyLabStreamEvent = {
        type: 'cycle_complete',
        cycle_index: 2,
        record_id: 'rec-3',
        completed_cycles: 3,
        batch_index: 1,
      };

      const result = reduce(state, event);

      expect(result).not.toBe(state);
      expect(result?.completed_cycles).toBe(3);
      expect(result?.current_cycle).toBeUndefined();
    });
  });

  describe('cycle_skipped', () => {
    it('increments skipped_cycles and clears current_cycle', () => {
      const state: StrategyLabRunStatus = {
        ...baseState,
        skipped_cycles: 1,
        current_cycle: { cycle_index: 2, phase: 'backtesting' },
      };
      const event: StrategyLabStreamEvent = { type: 'cycle_skipped', cycle_index: 2, reason: 'no_market_data', batch_index: 1 };

      const result = reduce(state, event);

      expect(result?.skipped_cycles).toBe(2);
      expect(result?.current_cycle).toBeUndefined();
    });
  });

  describe('cycle_errored', () => {
    it('increments errored_cycles from undefined and appends an errored_details entry', () => {
      const state: StrategyLabRunStatus = { ...baseState, errored_cycles: undefined, errored_details: undefined };
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 3,
        batch_index: 1,
        reason: 'ValueError',
        error: 'division by zero',
      };

      const result = reduce(state, event);

      expect(result?.errored_cycles).toBe(1);
      expect(result?.errored_details).toEqual([
        { cycle_index: 3, batch_index: 1, error: 'division by zero', exception_type: 'ValueError' },
      ]);
      expect(result?.current_cycle).toBeUndefined();
    });

    it('caps errored_details at the most recent 50 entries', () => {
      const existing = Array.from({ length: 50 }, (_, i) => ({ cycle_index: i, error: `err-${i}` }));
      const state: StrategyLabRunStatus = { ...baseState, errored_cycles: 50, errored_details: existing };
      const event: StrategyLabStreamEvent = {
        type: 'cycle_errored',
        cycle_index: 50,
        batch_index: 1,
        reason: 'ValueError',
        error: 'newest',
      };

      const result = reduce(state, event);

      expect(result?.errored_details).toHaveLength(50);
      expect(result?.errored_details?.[49]).toEqual({
        cycle_index: 50,
        batch_index: 1,
        error: 'newest',
        exception_type: 'ValueError',
      });
      expect(result?.errored_details?.[0]).toEqual({ cycle_index: 1, error: 'err-1' });
    });
  });

  describe('batch_start', () => {
    it('sets current_batch/batch_count/completed_batches from the event', () => {
      const event: StrategyLabStreamEvent = {
        type: 'batch_start',
        batch_index: 2,
        total_batches: 3,
        batch_size: 5,
        completed_batches: 1,
      };

      const result = reduce(baseState, event);

      expect(result?.current_batch).toBe(2);
      expect(result?.batch_count).toBe(3);
      expect(result?.completed_batches).toBe(1);
    });
  });

  describe('batch_complete', () => {
    it('sets completed_batches from the event and clears current_batch to null', () => {
      const event: StrategyLabStreamEvent = {
        type: 'batch_complete',
        batch_index: 1,
        total_batches: 2,
        completed_batches: 1,
      };

      const result = reduce(baseState, event);

      expect(result?.completed_batches).toBe(1);
      expect(result?.current_batch).toBeNull();
    });
  });

  describe('no-op event types (no StrategyLabRunStatus field to update)', () => {
    it('returns the exact same state reference for batch_warning, complete, error, and done', () => {
      const events: StrategyLabStreamEvent[] = [
        { type: 'batch_warning', batch_index: 1, reason: 'signal_brief_failed' },
        {
          type: 'complete',
          message: 'done',
          status: 'completed',
          completed_count: 10,
          skipped_count: 0,
          errored_count: 0,
          errored_details: [],
          completed_batches: 2,
          total_batches: 2,
        },
        { type: 'error', detail: 'Run failed' },
        { type: 'error', error: 'subscription reclaimed' },
        { type: 'done' },
      ];
      for (const event of events) {
        expect(reduce(baseState, event)).toBe(baseState);
      }
    });
  });
});
