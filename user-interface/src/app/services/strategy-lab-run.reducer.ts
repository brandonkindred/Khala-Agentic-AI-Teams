import type {
  BacktestResult,
  StrategyLabErroredDetail,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
} from '../models';

/**
 * Pure reducer folding one SSE stream event into the next Strategy Lab run
 * status. One `case` per `StrategyLabStreamEvent.type`; each reads fields
 * directly off the type narrowed by that discriminant, with no index casts.
 *
 * Preconditions: `event` is a value the backend actually emits for its
 *   `type` (the discriminated union's field/optionality guarantees are
 *   trusted as-is, not re-validated at runtime); `state`/`event` are never
 *   mutated by the caller after this call.
 * Postconditions: for an event that carries no run-status field
 *   (`batch_warning`, `complete`, `error`, `done`) or when `state` is
 *   `null` (no run is being tracked), returns the exact same `state`
 *   reference, unchanged. Otherwise returns a **new** `StrategyLabRunStatus`
 *   object reflecting the event; `state` and `event` are never mutated.
 */
export function reduce(
  state: StrategyLabRunStatus | null,
  event: StrategyLabStreamEvent,
): StrategyLabRunStatus | null {
  switch (event.type) {
    case 'snapshot': {
      if (!state) return state;
      return {
        ...state,
        status: event.status,
        completed_cycles: event.completed_cycles,
        skipped_cycles: event.skipped_cycles,
        errored_cycles: event.errored_cycles ?? state.errored_cycles,
        errored_details: event.errored_details ?? state.errored_details,
        // A real `null`/omitted current_cycle on the wire is treated the same
        // — both fall back to the prior value. Preserved from the original
        // Object.assign merge this replaces, not a new behavior.
        current_cycle: event.current_cycle ?? state.current_cycle,
        completed_record_ids: event.completed_record_ids,
        error: event.error ?? state.error,
        batch_size: event.batch_size ?? state.batch_size,
        batch_count: event.batch_count ?? state.batch_count,
        completed_batches: event.completed_batches ?? state.completed_batches,
        current_batch: event.current_batch ?? state.current_batch,
      };
    }

    case 'progress': {
      if (!state) return state;
      const prevStrategy = state.current_cycle?.strategy;
      const prevMetrics = state.current_cycle?.metrics;
      return {
        ...state,
        current_cycle: {
          cycle_index: event.cycle_index,
          phase: event.phase,
          sub_phase: event.sub_phase,
          refinement_round: event.refinement_round,
          strategy: event.strategy ?? prevStrategy,
          // event.metrics is honestly Record<string, unknown> — a cycle's
          // "complete" sub-phase republishes a full BacktestResult.model_dump()
          // here, which isn't numeric-only (see StrategyLabProgressEvent's own
          // doc comment). This assertion reflects that still-open, already-
          // documented modeling gap; it is not one of the index casts this
          // reducer otherwise eliminates.
          metrics: (event.metrics as Partial<BacktestResult> | undefined) ?? prevMetrics,
          checks_passed: event.checks_passed,
          checks_total: event.checks_total,
          symbols_count: event.symbols_count,
          bars_count: event.bars_count,
          trades_count: event.trades_count,
          execution_time: event.execution_time,
          failure_phase: event.failure_phase,
          changes_made: event.changes_made,
          is_winning: event.is_winning,
        },
      };
    }

    case 'cycle_complete': {
      if (!state) return state;
      return {
        ...state,
        completed_cycles: event.completed_cycles,
        current_cycle: undefined,
      };
    }

    case 'cycle_skipped': {
      if (!state) return state;
      return {
        ...state,
        skipped_cycles: state.skipped_cycles + 1,
        current_cycle: undefined,
      };
    }

    case 'cycle_errored': {
      if (!state) return state;
      const detail: StrategyLabErroredDetail = {
        cycle_index: event.cycle_index,
        batch_index: event.batch_index,
        error: event.error,
        exception_type: event.reason,
      };
      return {
        ...state,
        errored_cycles: (state.errored_cycles ?? 0) + 1,
        errored_details: [...(state.errored_details ?? []), detail].slice(-50),
        current_cycle: undefined,
      };
    }

    case 'batch_start': {
      if (!state) return state;
      return {
        ...state,
        current_batch: event.batch_index,
        batch_count: event.total_batches,
        completed_batches: event.completed_batches,
      };
    }

    case 'batch_complete': {
      if (!state) return state;
      return {
        ...state,
        completed_batches: event.completed_batches,
        current_batch: null,
      };
    }

    // These event types carry no StrategyLabRunStatus field — the component
    // reacts to them for other state (completionWarning, error, activity log)
    // outside this reducer.
    case 'batch_warning':
    case 'complete':
    case 'error':
    case 'done':
      return state;
  }
}
