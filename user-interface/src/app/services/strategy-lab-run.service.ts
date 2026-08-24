import { Injectable, OnDestroy, inject, signal } from '@angular/core';
import { Observable, Subject, Subscription, switchMap, takeWhile, timer } from 'rxjs';
import { InvestmentApiService } from './investment-api.service';
import { reduce as reduceStrategyLabRun } from './strategy-lab-run.reducer';
import { isPaperTradingStatusTerminal } from '../models';
import type { PaperTradingSession, StrategyLabRunStatus, StrategyLabStreamEvent } from '../models';
import { extractErrorDetail } from '../shared/extract-error-detail';

/**
 * Owns SSE connect/disconnect for a run's status stream, active-run
 * detection on load, the REST-polling fallback when SSE drops, and
 * per-record paper-trading-session polling — the four continuously-ticking
 * mechanisms `strategy-lab.component.ts` used to own directly. Component
 * consumption (injecting this service, binding its signals) is wired up
 * separately; this service only needs to stand on its own.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle (and thus its timers/subscriptions) is scoped
 * to that component via `ngOnDestroy`.
 *
 * Invariants: `runStatus`/`running`/`activeRunId` only change together, via
 *   `startRun()`, an SSE/poll update, or `finishRun()` — never independently.
 *   Every `Subscription` this service starts is tracked and unsubscribed by
 *   `ngOnDestroy()`.
 */
@Injectable()
export class StrategyLabRunService implements OnDestroy {
  private readonly api = inject(InvestmentApiService);

  private readonly _runStatus = signal<StrategyLabRunStatus | null>(null);
  private readonly _running = signal(false);
  private readonly _activeRunId = signal<string | null>(null);
  private readonly _paperTradingSessions = signal<Record<string, PaperTradingSession>>({});
  /** lab_record_id of the paper-trade session a caller-initiated trackPaperTradingSession() is following, or null. */
  private readonly _paperTradingLabRecordId = signal<string | null>(null);
  /**
   * `runStatus`'s value at the instant `finishRun()` clears it — the only way
   * to learn how a run ended when that happens without an explicit
   * `complete`/`error` stream event to carry the outcome (SSE degrades to
   * polling and polling itself observes the terminal status; or a
   * reconnect's terminal `snapshot` is followed straight by `done`, closing
   * the stream with no distinct terminal event of its own).
   */
  private readonly _lastTerminalStatus = signal<StrategyLabRunStatus | null>(null);

  readonly runStatus = this._runStatus.asReadonly();
  readonly running = this._running.asReadonly();
  readonly activeRunId = this._activeRunId.asReadonly();
  readonly paperTradingSessions = this._paperTradingSessions.asReadonly();
  readonly paperTradingLabRecordId = this._paperTradingLabRecordId.asReadonly();
  readonly lastTerminalStatus = this._lastTerminalStatus.asReadonly();

  /** Every raw SSE event, for side effects the run-status shape doesn't carry (e.g. reload results on `cycle_complete`). */
  private readonly _events = new Subject<StrategyLabStreamEvent>();
  readonly events$: Observable<StrategyLabStreamEvent> = this._events.asObservable();

  /** Out-of-band error strings from paper-trading-session polling (not an SSE event). */
  private readonly _errors = new Subject<string>();
  readonly errors$: Observable<string> = this._errors.asObservable();

  private sseSub: Subscription | null = null;
  private pollSub: Subscription | null = null;
  private activeRunCheckSub: Subscription | null = null;
  private readonly paperTradingPollSubs = new Map<string, Subscription>();

  /**
   * Postconditions: unsubscribes every subscription this service holds
   *   (run stream, polling fallback, active-run check, all per-record
   *   paper-trading polls) and completes `events$`/`errors$`. Idempotent.
   */
  ngOnDestroy(): void {
    this.sseSub?.unsubscribe();
    this.sseSub = null;
    this.pollSub?.unsubscribe();
    this.pollSub = null;
    this.activeRunCheckSub?.unsubscribe();
    this.activeRunCheckSub = null;
    for (const sub of this.paperTradingPollSubs.values()) {
      sub.unsubscribe();
    }
    this.paperTradingPollSubs.clear();
    this._events.complete();
    this._errors.complete();
  }

  // ---------------------------------------------------------------------------
  // Active run detection (for navigate-away-and-back)
  // ---------------------------------------------------------------------------

  /**
   * Poll for active runs a few times so a running job is always picked up —
   * even if the first request races with the backend becoming ready or the
   * in-memory cache being repopulated.
   *
   * Postconditions: issues up to 4 `getActiveRuns()` calls (at 0s, 3s, 6s,
   *   9s), stopping early once `running()` is true (e.g. `startRun()` was
   *   called locally in the meantime) or once a running run is found — at
   *   which point tracking begins for it via `startRun()`.
   */
  checkForActiveRun(): void {
    this.activeRunCheckSub?.unsubscribe();
    let attempts = 0;
    this.activeRunCheckSub = timer(0, 3000)
      .pipe(
        takeWhile(() => attempts < 4 && !this._running()),
        switchMap(() => {
          attempts++;
          return this.api.getActiveRuns();
        }),
      )
      .subscribe({
        next: (res) => {
          const active = res.runs.find((r) => r.status === 'running');
          if (active) {
            this.startRun(active.run_id, active);
            this.activeRunCheckSub?.unsubscribe();
          }
        },
      });
  }

  // ---------------------------------------------------------------------------
  // SSE streaming + polling fallback
  // ---------------------------------------------------------------------------

  /**
   * Begin tracking a run's live status: `runStatus`/`activeRunId` are set
   * immediately and the SSE stream is connected.
   *
   * Preconditions: `runId` is the id `initialStatus` itself describes.
   * Postconditions: `running()` is true, `runStatus()`/`activeRunId()`
   *   reflect the given values, `lastTerminalStatus()` is reset to null (so a
   *   prior run's captured outcome can't leak into this one), and the run's SSE
   *   stream is connected.
   */
  startRun(runId: string, initialStatus: StrategyLabRunStatus): void {
    this._lastTerminalStatus.set(null);
    this._activeRunId.set(runId);
    this._runStatus.set(initialStatus);
    this._running.set(true);
    this.connectToStream(runId);
  }

  private connectToStream(runId: string): void {
    this.sseSub?.unsubscribe();
    this.sseSub = this.api.streamRunStatus(runId).subscribe({
      next: (event) => this.handleStreamEvent(event),
      error: () => this.fallbackToPolling(runId),
      complete: () => this.finishRun(),
    });
  }

  /**
   * Fold one SSE event into `runStatus` and re-emit it on `events$` for the
   * component's own side effects; finish the run on a terminal event.
   *
   * Preconditions: called only as the SSE subscription's `next` handler.
   * Postconditions: `runStatus()` is the reducer's fold of the prior status and
   *   `event`; the event is forwarded on `events$` synchronously; and on a
   *   terminal event (`complete`/`error`/`cancelled`) `finishRun()` runs
   *   afterward (so `lastTerminalStatus` captures the just-folded status and the
   *   SSE/poll subscription is torn down).
   */
  private handleStreamEvent(event: StrategyLabStreamEvent): void {
    this._runStatus.set(reduceStrategyLabRun(this._runStatus(), event));
    this._events.next(event);
    if (event.type === 'complete' || event.type === 'error' || event.type === 'cancelled') {
      this.finishRun();
    }
  }

  /**
   * Preconditions: called only from `connectToStream()`'s `complete`
   *   callback, `handleStreamEvent()`'s terminal-event branch, or
   *   `fallbackToPolling()`'s terminal/error branches — never directly by a
   *   component.
   * Postconditions: `lastTerminalStatus()` captures `runStatus()`'s value at
   *   the instant this runs (before it's cleared), so callers see the true
   *   terminal outcome even when no explicit `complete`/`error` event
   *   reaches them. `running()` is false, `activeRunId()`/`runStatus()` are
   *   null, and any live SSE/poll subscription is unsubscribed.
   */
  private finishRun(): void {
    this._lastTerminalStatus.set(this._runStatus());
    this._running.set(false);
    this._activeRunId.set(null);
    this._runStatus.set(null);
    this.sseSub?.unsubscribe();
    this.sseSub = null;
    this.pollSub?.unsubscribe();
    this.pollSub = null;
  }

  /**
   * Preconditions: `runId` identifies the run whose SSE stream just errored
   *   (called only from `connectToStream()`'s `error` callback).
   * Postconditions: begins polling `getRunStatus(runId)` every 5s until a
   *   non-`'running'` status arrives, at which point `finishRun()` runs.
   *   `runStatus()` is updated on every successful poll. If polling itself
   *   errors, `runStatus()` is cleared to null first so `finishRun()`
   *   captures a null `lastTerminalStatus` (a genuinely unknown outcome)
   *   rather than a stale `'running'` status a caller could mistake for a
   *   real terminal outcome.
   */
  private fallbackToPolling(runId: string): void {
    this.pollSub?.unsubscribe();
    this.pollSub = timer(0, 5000)
      .pipe(
        switchMap(() => this.api.getRunStatus(runId)),
        takeWhile((status) => status.status === 'running', true),
      )
      .subscribe({
        next: (status) => {
          this._runStatus.set(status);
          if (status.status !== 'running') {
            this.finishRun();
          }
        },
        error: () => {
          // Polling itself failed — the run's fate is genuinely unknown, not
          // "still running" (the last value takeWhile let through before this
          // error). Clear runStatus before finishRun() captures it into
          // lastTerminalStatus, so that signal reads null here rather than a
          // stale 'running' status a caller could mistake for a real
          // terminal outcome (e.g. announcing success for a lost connection).
          this._runStatus.set(null);
          this.finishRun();
        },
      });
  }

  // ---------------------------------------------------------------------------
  // Paper trading session polling
  // ---------------------------------------------------------------------------

  /**
   * Drop all known paper-trading sessions (e.g. after "Clear all lab data"
   * deletes them server-side). Does not unsubscribe in-flight polls for
   * previously-tracked sessions — matching the pre-extraction component's
   * own behavior, where a still-running poll simply errors on its next tick
   * against the now-deleted session.
   *
   * Postconditions: `paperTradingSessions()` is `{}`.
   */
  clearPaperTradingSessions(): void {
    this._paperTradingSessions.set({});
  }

  /**
   * Adopt a batch of previously-run paper-trading sessions (e.g. on page
   * load) and resume polling for any still `running`. Does not touch
   * `paperTradingLabRecordId` — this is a silent resume, not a
   * caller-initiated action a button spinner should reflect.
   *
   * Postconditions: `paperTradingSessions()` equals `sessions`; polling is
   *   (re)started for every entry whose `status` is not yet terminal (see
   *   `isPaperTradingStatusTerminal` — covers the legacy `'running'` value
   *   as well as the PR-2 live-mode `'opening'` | `'warming_up'` | `'live'`
   *   states).
   */
  hydratePaperTradingSessions(sessions: Record<string, PaperTradingSession>): void {
    this._paperTradingSessions.set(sessions);
    for (const [labRecordId, session] of Object.entries(sessions)) {
      if (!isPaperTradingStatusTerminal(session.status)) {
        this.pollPaperTradingSession(labRecordId, session.session_id);
      }
    }
  }

  /**
   * Track a freshly-started paper-trading session: stores it and begins
   * polling for it.
   *
   * Preconditions: `session` is a just-started session — the backend
   *   returns one with a non-terminal `status` immediately on POST
   *   (`'opening'` when live paper trading is enabled, `'running'` on the
   *   legacy path).
   * Postconditions: `paperTradingSessions()[labRecordId]` is `session`;
   *   `paperTradingLabRecordId()` is `labRecordId`; polling starts.
   */
  trackPaperTradingSession(labRecordId: string, session: PaperTradingSession): void {
    this._paperTradingSessions.update((s) => ({ ...s, [labRecordId]: session }));
    this._paperTradingLabRecordId.set(labRecordId);
    this.pollPaperTradingSession(labRecordId, session.session_id);
  }

  /** Poll GET /strategy-lab/paper-trade/{session_id} until status is terminal. */
  private pollPaperTradingSession(labRecordId: string, sessionId: string): void {
    this.paperTradingPollSubs.get(labRecordId)?.unsubscribe();
    const sub = timer(3000, 3000)
      .pipe(
        switchMap(() => this.api.getPaperTradingSession(sessionId)),
        takeWhile((res) => !isPaperTradingStatusTerminal(res.session.status), true),
      )
      .subscribe({
        next: (res) => {
          this._paperTradingSessions.update((s) => ({ ...s, [labRecordId]: res.session }));
          if (isPaperTradingStatusTerminal(res.session.status)) {
            this._paperTradingLabRecordId.set(null);
            this.paperTradingPollSubs.delete(labRecordId);
          }
        },
        error: (err) => {
          this._paperTradingLabRecordId.set(null);
          this.paperTradingPollSubs.delete(labRecordId);
          this._errors.next(extractErrorDetail(err, 'Paper trading polling failed.'));
        },
      });
    this.paperTradingPollSubs.set(labRecordId, sub);
  }
}
