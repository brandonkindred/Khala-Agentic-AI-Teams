import { Injectable, OnDestroy, inject, signal } from '@angular/core';
import { Observable, Subject, Subscription } from 'rxjs';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { buildLogMessage, describeRunStatus, CONNECTION_LOST_MESSAGE } from './strategy-lab-log-message';
import type { StrategyLabProgressEvent, StrategyLabStreamEvent } from '../models';

export interface ActivityLogEntry {
  time: string;
  status: 'active' | 'done' | 'error';
  message: string;
}

/**
 * Reacts to a `StrategyLabRunService.events$` emission for the side effects
 * that aren't `StrategyLabRunStatus` fields: activity-log bookkeeping, the
 * completion/error/warning banners, and requesting a results refresh after a
 * completed cycle. Run-status folding itself is `StrategyLabRunService`'s
 * job (via `strategy-lab-run.reducer.ts`); this service only handles what's
 * left over from `strategy-lab.component.ts`'s original `handleStreamEvent`.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component (alongside `StrategyLabRunService`) so its lifecycle (and thus
 * its `events$` subscription and pending auto-scroll timer) is scoped to
 * that component via `ngOnDestroy`.
 *
 * The component has no direct DOM/API access into this service — auto-scroll
 * requests and result-refresh requests are surfaced as observables
 * (`scrollRequested$`, `resultsRefreshRequested$`) the component subscribes
 * to once, rather than this service reaching into an `ElementRef` or calling
 * a component method directly.
 */
@Injectable()
export class StrategyLabActivityLogService implements OnDestroy {
  private readonly runService = inject(StrategyLabRunService);

  private readonly _activityLog = signal<ActivityLogEntry[]>([]);
  readonly activityLog = this._activityLog;
  private lastCycleIndex = -1;

  /**
   * Non-fatal notice banner (dismissible, non-error styling): shown when a run
   * finishes with errored/skipped cycles, when a non-fatal `batch_warning`
   * arrives mid-run, or when a run is cancelled by the user (a deliberate stop
   * is a notice, not a red-banner error).
   */
  readonly completionWarning = signal<string | null>(null);

  /**
   * Terminal-outcome text for the aria-live status region
   * (`StrategyLabComponent.runAnnouncement`). Set from this service's
   * `complete`/`error`/`cancelled` handling — using the terminal event's own
   * data, while `runService.runStatus()` is still populated — or, when
   * neither fires (SSE degrades to polling, or a reconnect gets only a
   * terminal snapshot then `done`), backstopped by the component's
   * `refreshResultsOnRunFinish` fallback once `runStatus()` has already
   * cleared.
   */
  readonly runOutcomeAnnouncement = signal<string | null>(null);

  /**
   * The red error-banner message a terminal 'error' stream event set for THIS
   * run (the failure/interrupt detail, or the connection-lost message), or
   * null if the run has not ended in an error. `refreshResultsOnRunFinish`
   * re-asserts exactly this after `loadResults()` clears `error`, so ONLY a
   * genuine terminal run error survives the run-finish refresh — an
   * unrelated ambient error still showing (e.g. an `errors$` paper-trading
   * poll failure) is left to be cleared, not resurrected onto a
   * cleanly-completed run. A plain field (not a signal) so reading it in the
   * component's effect adds no reactive dependency.
   */
  terminalErrorBanner: string | null = null;

  /** Emits once per completed cycle, so the component reloads results. */
  private readonly _resultsRefreshRequested = new Subject<void>();
  readonly resultsRefreshRequested$: Observable<void> = this._resultsRefreshRequested.asObservable();

  /** Emits the red error-banner message for a terminal run error. */
  private readonly _terminalError = new Subject<string>();
  readonly terminalError$: Observable<string> = this._terminalError.asObservable();

  /** Emits after a debounced activity-log append, so the component scrolls its log container. */
  private readonly _scrollRequested = new Subject<void>();
  readonly scrollRequested$: Observable<void> = this._scrollRequested.asObservable();

  /** Pending auto-scroll timer id, cleared on destroy. */
  private autoScrollTimeoutId: ReturnType<typeof setTimeout> | null = null;

  private readonly eventsSub: Subscription;

  constructor() {
    this.eventsSub = this.runService.events$.subscribe((event) => this.handleStreamEvent(event));
  }

  /**
   * Postconditions: unsubscribes from `runService.events$` and cancels a
   * pending auto-scroll timer, if any. Idempotent.
   */
  ngOnDestroy(): void {
    this.eventsSub.unsubscribe();
    if (this.autoScrollTimeoutId !== null) {
      clearTimeout(this.autoScrollTimeoutId);
      this.autoScrollTimeoutId = null;
    }
  }

  /**
   * Preconditions: none — every branch is safe regardless of `runService`
   *   state.
   * Postconditions: `activityLog`, `completionWarning`, `terminalErrorBanner`,
   *   and `runOutcomeAnnouncement` are updated for event types that carry
   *   them (`complete`/`error`/`cancelled` set `runOutcomeAnnouncement`
   *   directly from the terminal event's own data); `resultsRefreshRequested$`
   *   emits after a completed cycle (in addition to the component's
   *   `refreshResultsOnRunFinish` once-per-run refresh — a multi-cycle run's
   *   earlier cycles need this mid-run signal since `running()` stays true
   *   until the whole run ends).
   */
  private handleStreamEvent(event: StrategyLabStreamEvent): void {
    if (event.type === 'progress' && this.runService.runStatus()) {
      // Reset activity log when a new cycle starts.
      if (event.cycle_index !== this.lastCycleIndex) {
        this._activityLog.set([]);
        this.lastCycleIndex = event.cycle_index;
      }
      this.addLogEntry(event.phase, event.sub_phase, event);
    }

    if (event.type === 'cycle_complete' && this.runService.runStatus()) {
      this._activityLog.set([]);
      this.lastCycleIndex = -1;
      this._resultsRefreshRequested.next();
    }

    if (event.type === 'batch_warning' && this.runService.runStatus()) {
      // Non-fatal pre-batch issue (e.g. signal-brief failure). Surface as a
      // gentle warning; the run is still progressing. Guarded like the
      // 'progress'/'cycle_complete' branches above so a stale event for a
      // run that has already ended can't resurrect the warning banner.
      this.completionWarning.set(
        event.reason === 'signal_brief_failed'
          ? 'Signal brief unavailable for a batch; strategies continued without it.'
          : event.reason || 'A non-fatal warning occurred during a batch.',
      );
    }

    if (event.type === 'complete') {
      // completionWarning is the sighted dismissible banner — kept scoped to
      // genuine errors only (its long-standing condition): a skip-only
      // completion already has a dedicated in-progress skipped-badge, so a
      // banner re-announcing it at the end would be new behavior. The aria-live
      // sentence, by contrast, covers skips too — the badge disappears once
      // `running()` goes false, so this terminal announcement is a
      // screen-reader user's only remaining signal that not every requested
      // strategy was produced.
      const hasErrors = event.errored_count > 0 || event.status === 'completed_with_errors';
      const hasSkips = event.skipped_count > 0;
      if (hasErrors) {
        const parts: string[] = [`${event.errored_count} cycle(s) errored`];
        if (hasSkips) parts.push(`${event.skipped_count} cycle(s) skipped`);
        this.completionWarning.set(`Run finished with ${parts.join(' and ')}. See details below.`);
      }
      // Route through describeRunStatus (fed the event's own counts) so the
      // live-SSE sentence can never drift from the poll-fallback one.
      this.runOutcomeAnnouncement.set(
        describeRunStatus({
          status: event.status,
          errored_cycles: event.errored_count,
          skipped_cycles: event.skipped_count,
        }),
      );
    }

    if (event.type === 'error') {
      if (event.detail === undefined) {
        // The shared-infra "subscription reclaimed" wire shape
        // (StrategyLabErrorReclaimEvent) carries only `.error`, never
        // `.detail` — a connection-level event (e.g. eviction under load),
        // not necessarily a job failure, so it gets its own message rather
        // than confidently announcing a failure the run may not have had.
        this.setTerminalError(CONNECTION_LOST_MESSAGE);
        this.runOutcomeAnnouncement.set(CONNECTION_LOST_MESSAGE);
      } else {
        // A genuine user cancellation is never routed through 'error' — it's
        // its own 'cancelled' event type (branch below) — so every 'error'
        // event reaching here is a real failure or an external stop. The
        // backend marks an external stop 'failed' or 'interrupted' and carries
        // that on `terminal_status`; describeRunStatus announces the two
        // distinctly so an externally-interrupted run isn't spoken as "failed".
        this.setTerminalError(event.detail || 'Run failed');
        this.runOutcomeAnnouncement.set(describeRunStatus({ status: event.terminal_status ?? 'failed' }));
      }
    }

    if (event.type === 'cancelled') {
      // A deliberate user cancellation is not an error — surface it in the
      // non-error (dismissible warning) banner rather than the red error one,
      // now that cancellation has its own event type. The aria-live region
      // announces the outcome regardless.
      this.completionWarning.set(event.detail || 'Run cancelled by user.');
      this.runOutcomeAnnouncement.set(describeRunStatus({ status: 'cancelled' }));
    }
  }

  /**
   * Set the red error banner for a terminal run error AND record it in
   * `terminalErrorBanner` so the component's `refreshResultsOnRunFinish`
   * re-asserts it across the run-finish `loadResults()` clear.
   *
   * Preconditions: called only from `handleStreamEvent()`'s terminal 'error'
   *   branches (a real failure/interrupt, or a connection-lost reclaim).
   * Postconditions: `terminalError$` emits `message` and `terminalErrorBanner`
   *   holds it.
   */
  private setTerminalError(message: string): void {
    this.terminalErrorBanner = message;
    this._terminalError.next(message);
  }

  private addLogEntry(phase: string, subPhase: string | undefined, data: StrategyLabProgressEvent): void {
    const now = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    const msg = buildLogMessage(phase, subPhase, data);
    if (!msg) return;

    const isTerminal = subPhase === 'completed' || subPhase === 'data_loaded';
    const newEntry: ActivityLogEntry = { time: now, status: isTerminal ? 'done' : 'active', message: msg };

    this._activityLog.update((log) => {
      // Mark the previous active entry as done (if it's still active when a new entry arrives).
      let lastActiveIndex = -1;
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].status === 'active') {
          lastActiveIndex = i;
          break;
        }
      }
      const closed = lastActiveIndex === -1
        ? log
        : log.map((entry, i) => (i === lastActiveIndex ? { ...entry, status: 'done' as const } : entry));
      return [...closed, newEntry];
    });

    // Request an auto-scroll of the log container. Track the timer so a
    // destroy mid-wait cancels it — the component would otherwise scroll a
    // detached element.
    if (this.autoScrollTimeoutId !== null) {
      clearTimeout(this.autoScrollTimeoutId);
    }
    this.autoScrollTimeoutId = setTimeout(() => {
      this.autoScrollTimeoutId = null;
      this._scrollRequested.next();
    }, 50);
  }
}
