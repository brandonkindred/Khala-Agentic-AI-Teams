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
 * Postconditions: for an event that carries no run-status field at all
 *   (`batch_warning`, `done`) or when `state` is `null` (no run is being
 *   tracked), returns the exact same `state` reference, unchanged.
 *   `complete`/`error` fold only `status` (`event.status`, or the safe
 *   default `'failed'` for `error`, which carries no structured outcome
 *   field) — every other field is unaffected, since the component derives
 *   the announcement/warning text for these two directly from the event
 *   outside this reducer. Every other event type returns a **new**
 *   `StrategyLabRunStatus` object reflecting the event; `state` and `event`
 *   are never mutated.
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
        tracker_merge_error_count: event.tracker_merge_error_count ?? state.tracker_merge_error_count,
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
      // event.reason carries whatever the backend's cycle_errored publish
      // put there — a raw exception class name for a cycle's own failure,
      // or the fixed marker 'tracker_merge_failed' for a post-completion
      // tracker-merge failure (main.py) — stored under the same-named
      // `reason` field, not renamed to `exception_type`, so callers that
      // key off the 'tracker_merge_failed' marker (e.g. the live region's
      // double-count correction) see it on a live-streamed event exactly
      // as they would on a polled/snapshot-sourced errored_details entry.
      const detail: StrategyLabErroredDetail = {
        cycle_index: event.cycle_index,
        batch_index: event.batch_index,
        error: event.error,
        reason: event.reason,
      };
      return {
        ...state,
        errored_cycles: (state.errored_cycles ?? 0) + 1,
        errored_details: [...(state.errored_details ?? []), detail].slice(-50),
        // Incremented directly from the live event's own `reason`, mirroring
        // errored_cycles above — independent of errored_details' 50-entry
        // cap, so this stays exact even once matching entries evict.
        tracker_merge_error_count:
          (state.tracker_merge_error_count ?? 0) + (event.reason === 'tracker_merge_failed' ? 1 : 0),
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

    case 'complete': {
      if (!state) return state;
      // event.status is the one StrategyLabRunStatus field a terminal
      // 'complete' event actually carries — folding it in keeps `state`
      // (and anything that captures it afterward, e.g. finishRun()'s
      // lastTerminalStatus) accurate even for a run whose SSE connection
      // stayed open its whole lifetime, when no other case here would ever
      // have updated `status` away from its initial 'running' value. The
      // component reacts to the event's own richer fields (errored_count,
      // skipped_count) for the announcement/warning text outside this
      // reducer — this only keeps the shared status field truthful.
      return { ...state, status: event.status };
    }

    case 'error': {
      if (!state) return state;
      // Unlike 'complete', a terminal 'error' event carries no structured
      // outcome field (just free-text detail/error) — 'failed' is a safe,
      // never-wrongly-successful default for the same reason as above:
      // a run whose connection stayed open its whole lifetime would
      // otherwise leave `status` at its initial 'running' value straight
      // through to finishRun()'s lastTerminalStatus capture. The
      // component's own detail-based classification (failed/cancelled/
      // connection-lost) for the live announcement happens outside this
      // reducer, from the event directly.
      return { ...state, status: 'failed' };
    }

    // These event types carry no StrategyLabRunStatus field — the component
    // reacts to them for other state (completionWarning, error, activity log)
    // outside this reducer.
    case 'batch_warning':
    case 'done':
      return state;
  }
}
