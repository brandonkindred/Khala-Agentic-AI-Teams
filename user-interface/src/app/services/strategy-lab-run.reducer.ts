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
 *   `complete`/`error`/`cancelled` fold only `status` (`event.status` for
 *   `complete`; `event.terminal_status` when the `error` carries one — an
 *   external stop marked `'failed'`/`'interrupted'` — else the safe default
 *   `'failed'`; the literal `'cancelled'` for `cancelled`) —
 *   every other field is unaffected, since the component derives the
 *   announcement/warning text for these directly from the event outside
 *   this reducer. Every other event type returns a **new**
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
      // event.reason carries whatever the backend's cycle_errored publish put
      // there — a raw exception class name for a cycle's own failure, or the
      // fixed marker 'tracker_merge_failed' for a post-completion tracker-merge
      // failure (main.py). Store it under the SAME key the backend's persisted
      // errored_details uses for each case, so a live-streamed detail is
      // shaped identically to the polled/snapshot one: a regular failure's
      // class name goes under `exception_type` (matching main.py's cycle
      // except-handler entry), while the tracker-merge marker goes under
      // `reason` (matching main.py's merge-failure entry, which the live
      // region's double-count correction keys off). Mixing the two keys by
      // source is exactly the drift this split avoids. The tracker-merge event
      // additionally carries the raising class under `exception_type`, which the
      // persisted entry also stores — fold it through so the two are identical.
      const isTrackerMerge = event.reason === 'tracker_merge_failed';
      const detail: StrategyLabErroredDetail = {
        cycle_index: event.cycle_index,
        batch_index: event.batch_index,
        error: event.error,
        ...(isTrackerMerge
          ? { reason: event.reason, ...(event.exception_type ? { exception_type: event.exception_type } : {}) }
          : { exception_type: event.reason }),
      };
      return {
        ...state,
        errored_cycles: (state.errored_cycles ?? 0) + 1,
        errored_details: [...(state.errored_details ?? []), detail].slice(-50),
        // Incremented directly from the live event's own `reason`, mirroring
        // errored_cycles above — independent of errored_details' 50-entry
        // cap, so this stays exact even once matching entries evict.
        tracker_merge_error_count: (state.tracker_merge_error_count ?? 0) + (isTrackerMerge ? 1 : 0),
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
      // A genuine user cancellation is its own 'cancelled' event type (see
      // that case below), never routed through 'error'. A terminal 'error'
      // event is either a real in-run failure, a connection-lost reclaim, or
      // an external stop the backend records as 'failed'/'interrupted'. Only
      // the last carries a structured `terminal_status`; fold it when present
      // so an externally-interrupted run's status is not mislabeled 'failed'.
      // Otherwise 'failed' is the safe, never-wrongly-successful default (same
      // reason as 'complete' above: a run whose connection stayed open its
      // whole lifetime would otherwise leave `status` at its initial 'running'
      // value straight through to finishRun()'s lastTerminalStatus capture).
      return { ...state, status: event.terminal_status ?? 'failed' };
    }

    case 'cancelled': {
      if (!state) return state;
      // event.detail carries only free text — 'cancelled' is the one
      // structured outcome this event type ever represents, so folding it
      // unconditionally (unlike 'error') keeps lastTerminalStatus() accurate
      // for a run whose SSE connection stayed open its whole lifetime.
      return { ...state, status: 'cancelled' };
    }

    // These event types carry no StrategyLabRunStatus field — the component
    // reacts to them for other state (completionWarning, error, activity log)
    // outside this reducer.
    case 'batch_warning':
    case 'done':
      return state;
  }
}
