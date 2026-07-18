import type {
  BacktestResult,
  StrategyLabCycleProgress,
  StrategyLabErroredDetail,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
} from '../../models';

/**
 * Applies one Strategy Lab stream event to the current run-status snapshot,
 * producing the next snapshot without mutating either input.
 *
 * Preconditions: `event` is a well-formed member of the `StrategyLabStreamEvent`
 *   union — the SSE decoder that produces `event` owns validating this; a
 *   malformed frame is that caller's contract violation, not defended against
 *   here. `state` is either `null` (no run currently tracked) or a well-formed
 *   `StrategyLabRunStatus`.
 * Postconditions: returns the next `StrategyLabRunStatus`, or `null` when
 *   `state` was already `null` for an event type that only updates an
 *   existing run. Never mutates `state` (or any object/array reachable from
 *   it) or `event`. Returns the exact same `state` reference — not a clone —
 *   when `event`'s type carries no run-status change.
 * Invariants: none — stateless pure function.
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
        // event.status additionally allows 'interrupted' (see the doc comment
        // on StrategyLabSnapshotEvent in the models file); StrategyLabRunStatus
        // stays the narrower 5-value union since it also backs the REST
        // polling response with other, unaudited consumers. Passed through
        // as-is rather than widening that shared type.
        status: event.status as StrategyLabRunStatus['status'],
        completed_cycles: event.completed_cycles,
        skipped_cycles: event.skipped_cycles,
        errored_cycles: event.errored_cycles ?? state.errored_cycles,
        errored_details: event.errored_details ?? state.errored_details,
        // An explicit `null` on the wire means "no new value" here, same as
        // the merge this replaces — not "clear the field". StrategyLabRunStatus
        // itself stays narrower than the snapshot's nested shape (see above).
        current_cycle: (event.current_cycle ?? state.current_cycle) as StrategyLabCycleProgress | undefined,
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
      return {
        ...state,
        current_cycle: {
          cycle_index: event.cycle_index,
          phase: event.phase,
          sub_phase: event.sub_phase,
          refinement_round: event.refinement_round,
          strategy: event.strategy ?? prevStrategy,
          // Not numeric-only on the wire (see StrategyLabProgressEvent.metrics'
          // own doc comment); StrategyLabCycleProgress.metrics stays
          // Partial<BacktestResult> because the template reads named numeric
          // fields off it directly.
          metrics: (event.metrics as Partial<BacktestResult> | undefined) ?? state.current_cycle?.metrics,
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
      return { ...state, completed_cycles: event.completed_cycles, current_cycle: undefined };
    }

    case 'cycle_skipped': {
      if (!state) return state;
      return { ...state, skipped_cycles: state.skipped_cycles + 1, current_cycle: undefined };
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
      return { ...state, completed_batches: event.completed_batches, current_batch: null };
    }

    case 'batch_warning':
    case 'complete':
    case 'error':
    case 'done':
      return state;

    default: {
      // Compile-time exhaustiveness: a new StrategyLabStreamEvent member fails
      // to build until it gets an explicit case above, rather than silently
      // no-oping like the pre-refactor handleStreamEvent did for 'done'.
      const unhandled: never = event;
      void unhandled;
      return state;
    }
  }
}
