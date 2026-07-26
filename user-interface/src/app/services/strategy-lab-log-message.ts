import type { StrategyLabProgressEvent } from '../models';

/**
 * Shared text for both places the run's fate is genuinely unknown (not a
 * known failure/cancellation/completion) — `describeRunStatus()`'s
 * null-status branch and `StrategyLabActivityLogService`'s SSE-reclaim
 * branch — so the wording can't drift between the two.
 */
export const CONNECTION_LOST_MESSAGE = 'Strategy Lab lost track of the run — status unavailable.';

/**
 * Render a human-readable activity-log message for one progress-event
 * phase/sub-phase pair, used by `StrategyLabActivityLogService.addLogEntry`
 * to populate the Strategy Lab run's live activity feed.
 *
 * Preconditions: `phase`/`subPhase` are the `phase`/`sub_phase` of a
 *   `StrategyLabProgressEvent`, and `data` is that same event (the three
 *   are never mixed from different events).
 * Postconditions: always returns a non-empty string — every known
 *   phase/sub-phase combination returns a specific message; an unrecognized
 *   sub-phase within a known phase falls back to that phase's generic
 *   in-progress message, and an unrecognized phase falls back to
 *   `` `${phase} — ${subPhase ?? 'processing'}` ``. Never mutates `data`.
 */
export function buildLogMessage(phase: string, subPhase: string | undefined, data: StrategyLabProgressEvent): string {
  const strategy = data['strategy'] as { asset_class?: string; hypothesis?: string } | undefined;
  const round = data['refinement_round'] as number | undefined;

  switch (phase) {
    case 'ideating':
      if (subPhase === 'started') return 'Ideating new trading strategy & generating code...';
      if (subPhase === 'completed') return `Strategy ideated — ${strategy?.asset_class ?? 'unknown'} asset class`;
      return 'Ideating...';
    case 'coding':
      if (subPhase === 'started') return 'Validating strategy spec and code safety...';
      if (subPhase === 'completed') return `Code validated (${data['checks_total'] ?? '?'} checks, ${data['checks_passed'] ?? '?'} passed)`;
      if (subPhase === 'failed') return `Validation failed (${(data['checks_total'] as number ?? 0) - (data['checks_passed'] as number ?? 0)} critical issue(s))`;
      if (subPhase === 'refining') return `Refining code (round ${(round ?? 0) + 1}/10) — fixing ${data['failure_phase'] ?? 'issues'}...`;
      if (subPhase === 'refined') return `Code refined — ${data['changes_made'] ?? 'code updated'}`;
      return 'Coding...';
    case 'backtesting':
      if (subPhase === 'fetching_data') return 'Fetching historical market data...';
      if (subPhase === 'data_loaded') return `Market data loaded (${data['symbols_count'] ?? '?'} symbols, ${(data['bars_count'] as number ?? 0).toLocaleString()} bars)`;
      if (subPhase === 'running_code') return 'Executing strategy backtest in sandbox...';
      if (subPhase === 'completed') return `Backtest complete — ${data['trades_count'] ?? '?'} trades in ${((data['execution_time'] as number) ?? 0).toFixed(1)}s`;
      return 'Backtesting...';
    case 'analyzing':
      if (subPhase === 'draft') return 'Generating analysis narrative...';
      if (subPhase === 'review') return 'Self-reviewing analysis against metrics...';
      if (subPhase === 'completed') return `Analysis complete — ${data['is_winning'] ? 'WINNING' : 'LOSING'}`;
      return 'Analyzing...';
    default:
      return `${phase} — ${subPhase ?? 'processing'}`;
  }
}

/**
 * The single source of terminal-outcome sentences for the aria-live region.
 * Both `StrategyLabActivityLogService`'s terminal branches (fed a minimal
 * object built from the terminal event's own fields) and
 * `strategy-lab.component.ts`'s `refreshResultsOnRunFinish` fallback (fed
 * `runService.lastTerminalStatus()`) route through here, so the live-SSE
 * announcement and the poll-fallback announcement can never word the same
 * outcome differently.
 *
 * Preconditions: `status` is `null` ONLY to mean "the run's fate is
 *   genuinely unknown" (`StrategyLabRunService` clears `runStatus` before
 *   capturing it into `lastTerminalStatus` specifically when its polling
 *   fallback itself errors) — never "no run happened"; callers only invoke
 *   this at run end.
 * Postconditions: returns a sentence reflecting `status.status`/
 *   `errored_cycles`/`skipped_cycles` when `status` is non-null (errors
 *   outrank skips outrank a clean finish); a distinct connection-lost
 *   sentence when `status` is null — never the generic "complete" sentence
 *   for an outcome that isn't actually known. Pure; always non-empty.
 */
export function describeRunStatus(
  status: { status?: string; errored_cycles?: number; skipped_cycles?: number } | null,
): string {
  if (!status) return CONNECTION_LOST_MESSAGE;
  if (status.status === 'failed') return 'Strategy Lab run failed.';
  if (status.status === 'cancelled') return 'Strategy Lab run cancelled.';
  if (status.status === 'interrupted') return 'Strategy Lab run interrupted.';
  if (status.status === 'completed_with_errors' || (status.errored_cycles ?? 0) > 0) {
    return 'Strategy Lab run finished with errors.';
  }
  if ((status.skipped_cycles ?? 0) > 0) {
    return 'Strategy Lab run finished with some strategies skipped.';
  }
  return 'Strategy Lab run complete.';
}
