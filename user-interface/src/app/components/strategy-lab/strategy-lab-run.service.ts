import { Injectable, OnDestroy, inject, signal } from '@angular/core';
import { Observable, Subject, Subscription, timer, switchMap, takeWhile } from 'rxjs';
import { InvestmentApiService } from '../../services/investment-api.service';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import type { PaperTradingSession, StrategyLabRunStatus, StrategyLabStreamEvent } from '../../models';
import { reduce } from './strategy-lab-run.reducer';

/**
 * Owns Strategy Lab's live run-tracking mechanics, extracted out of
 * `StrategyLabComponent`: SSE connect/disconnect for the run stream, the
 * on-load "is a run already active?" poll (`checkForActiveRun`), the
 * SSE-error REST-polling fallback (`fallbackToPolling`), and per-record
 * paper-trading-session polling (`pollPaperTradingSession`). All four ticked
 * continuously as zone-patched timers directly inside the component; this
 * service is the sole owner of that plumbing now.
 *
 * `StrategyLabComponent` does not consume this service yet — a follow-up
 * change will inject it, bind its state via `signal()`/`async` in the
 * template, and delete the component's own now-duplicate copy of this logic.
 * This change only stands the service up and proves it in isolation via its
 * own unit tests.
 *
 * Intended to be provided at `StrategyLabComponent`'s own component level
 * (not `providedIn: 'root'`) once wired up, matching `AgentStudioStateService`
 * and `PrReviewRunsService`: each visit to the Strategy Lab page should get a
 * fresh instance whose run-tracking state resets cleanly on navigation away
 * and back, instead of one app-wide instance leaking state across visits.
 * Co-located with `strategy-lab.component.ts` and `strategy-lab-run.reducer.ts`
 * (its sole consumer and its sole reducer dependency) rather than under the
 * shared `services/` directory, matching `PrReviewRunsService`'s placement.
 *
 * State is exposed as signals (`runStatus`, `running`, `activeRunId`,
 * `paperTradingSessions`) rather than Observables, per recent convention
 * (`UserProfileStore`, `AgentStudioStateService`). Every `runStatus`
 * transition driven by an SSE event goes through the pure `reduce()` — never
 * mutated in place. The REST polling fallback replaces `runStatus` wholesale
 * instead, because `getRunStatus()` already returns a full snapshot, not an
 * incremental delta (see `fallbackToPolling`).
 *
 * Two Subject-backed Observables round out the surface for discrete *events*
 * rather than settled state: `events$` forwards every stream event verbatim
 * so a future consumer can replicate `StrategyLabComponent.handleStreamEvent`'s
 * side-effect switch (activity log, `loadResults()`, warning banners — all
 * page-level business logic that stays component-owned) without this service
 * needing to know anything about it; `errors$` carries human-readable
 * paper-trading poll failures — the *only* error path the original component
 * surfaces as a message. SSE-connection-loss (`connectToStream`'s `error:`),
 * the REST-poll-fallback's own failure (`fallbackToPolling`'s `error:`), and
 * `checkForActiveRun`'s `getActiveRuns()` call (no `error:` callback at all)
 * are all silent in the original component — no user-facing message on any
 * of those three paths — and stay silent here. Pre-existing quirks this
 * extraction preserves verbatim, not fixes.
 *
 * Invariants: `runStatus`/`running`/`activeRunId` only ever change together
 * as a unit — a run is either fully tracked (`running` true, `activeRunId`
 * and `runStatus` both non-null) or fully not (`running` false, both null).
 * `paperTradingPollSubs` has an entry for `labRecordId` if and only if that
 * record currently has a live poller; the entry is always removed in the
 * same synchronous step that stops the poller (terminal status, error, or
 * `ngOnDestroy`), so it can never point at a dead subscription.
 */
@Injectable()
export class StrategyLabRunService implements OnDestroy {
  private readonly api = inject(InvestmentApiService);

  private readonly _runStatus = signal<StrategyLabRunStatus | null>(null);
  private readonly _running = signal(false);
  private readonly _activeRunId = signal<string | null>(null);
  private readonly _paperTradingSessions = signal<Record<string, PaperTradingSession>>({});

  /** Current run's status snapshot, or null when no run is tracked. */
  readonly runStatus = this._runStatus.asReadonly();
  /** True while a run is actively being tracked (via SSE or the REST fallback). */
  readonly running = this._running.asReadonly();
  /** The currently-tracked run's id, or null when none. */
  readonly activeRunId = this._activeRunId.asReadonly();
  /** Latest known paper-trading session per `lab_record_id`. */
  readonly paperTradingSessions = this._paperTradingSessions.asReadonly();

  private readonly _events = new Subject<StrategyLabStreamEvent>();
  /** Every SSE stream event, forwarded verbatim after it has already been folded into `runStatus`. */
  readonly events$: Observable<StrategyLabStreamEvent> = this._events.asObservable();

  private readonly _errors = new Subject<string>();
  /** Human-readable paper-trading poll failures (see class doc — the sole error path this service surfaces). */
  readonly errors$: Observable<string> = this._errors.asObservable();

  private sseSub: Subscription | null = null;
  private pollSub: Subscription | null = null;
  private activeRunCheckSub: Subscription | null = null;
  private paperTradingPollSubs: Record<string, Subscription> = {};

  /**
   * Poll for active runs a few times on page load so a running job is always
   * picked up, even if the first request races with the backend becoming
   * ready. Stops after 4 attempts (0s/3s/6s/9s) or as soon as {@link startRun}
   * has been called — by this poll finding a run, or by the caller starting
   * a new one directly — whichever comes first.
   *
   * Preconditions: none.
   * Postconditions: no-op beyond issuing up to 4 `getActiveRuns()` requests
   * spaced 3s apart; when a response contains a run with `status ===
   * 'running'`, calls {@link startRun} with it and stops polling immediately,
   * without waiting for the next scheduled tick. A `getActiveRuns()` failure
   * is unhandled (no `error:` callback) and silently stops this poll — a
   * pre-existing quirk of the original component, preserved verbatim here,
   * not fixed.
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

  /**
   * Begin tracking a run: seed state from `initialStatus` and connect its SSE
   * stream. Called internally by {@link checkForActiveRun} when it finds an
   * already-running job, and externally by a future consumer once its own
   * "start a new run" POST resolves (mirroring the original component's
   * `runNewStrategy` calling `connectToStream(res.run_id)` right after a
   * successful POST — request construction/validation stays component-owned;
   * only the SSE/timer plumbing that follows a successful start moves here).
   *
   * Preconditions: `runId` identifies a run the backend recognizes;
   * `initialStatus` is that run's current known snapshot.
   * Postconditions: `activeRunId` is `runId`, `runStatus` is `initialStatus`,
   * `running` is true, and the SSE stream for `runId` is connected (replacing
   * any previously-connected stream).
   */
  startRun(runId: string, initialStatus: StrategyLabRunStatus): void {
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
   * Fold one stream event into `runStatus` via `reduce()`, forward it on
   * `events$`, and end the run immediately for a `'complete'` or `'error'`
   * event *type* — distinct from the SSE Observable itself completing, which
   * only happens on a separate `'done'`-type event (see `connectToStream`'s
   * own `complete:` callback, and `InvestmentApiService.streamRunStatus`,
   * which calls `subscriber.complete()` only when it sees a `'done'` frame).
   * Both paths converge on `finishRun()`, but from two structurally distinct
   * triggers, both preserved.
   */
  private handleStreamEvent(event: StrategyLabStreamEvent): void {
    this._runStatus.set(reduce(this._runStatus(), event));
    this._events.next(event);
    if (event.type === 'complete' || event.type === 'error') {
      this.finishRun();
    }
  }

  /**
   * Counterpart of the original component's `onRunComplete()`, minus its
   * `loadResults()` call — reloading the page's results list is component-
   * level business logic that stays in `StrategyLabComponent`.
   */
  private finishRun(): void {
    this._running.set(false);
    this._activeRunId.set(null);
    this._runStatus.set(null);
    this.sseSub?.unsubscribe();
    this.sseSub = null;
    this.pollSub?.unsubscribe();
    this.pollSub = null;
  }

  private fallbackToPolling(runId: string): void {
    this.pollSub?.unsubscribe();
    this.pollSub = timer(0, 5000)
      .pipe(
        switchMap(() => this.api.getRunStatus(runId)),
        takeWhile((status) => status.status === 'running', true),
      )
      .subscribe({
        next: (status) => {
          // Whole-object replace, not reduce(): getRunStatus() already
          // returns a full snapshot, not an incremental SSE delta.
          this._runStatus.set(status);
          if (status.status !== 'running') {
            this.finishRun();
          }
        },
        error: () => {
          // Polling also failed — stop tracking. Silent, matching the
          // original (no error surfaced here — see class doc).
          this.finishRun();
        },
      });
  }

  /**
   * Record a paper-trading session's snapshot and, if it is still running,
   * (re)start polling it to completion. A future consumer calls this both
   * right after its own "start paper trading" POST resolves and when its own
   * results-load discovers a still-running session on page load — unifying
   * the original component's two call sites (an unconditional poll-start
   * after a fresh POST, and a conditional one when resuming after reload)
   * behind one safe rule, since a freshly-started session is documented to
   * always come back `running` immediately.
   *
   * Preconditions: `session.lab_record_id` need not equal `labRecordId`'s
   * prior contents — this fully replaces that record's tracked session.
   * Postconditions: `paperTradingSessions()[labRecordId]` is `session`.
   * When `session.status === 'running'`, a poller for `session.session_id`
   * is (re)started under `labRecordId`, replacing any prior poller for that
   * record.
   */
  trackPaperTradingSession(labRecordId: string, session: PaperTradingSession): void {
    this._paperTradingSessions.update((sessions) => ({ ...sessions, [labRecordId]: session }));
    if (session.status === 'running') {
      this.pollPaperTradingSession(labRecordId, session.session_id);
    }
  }

  /** Poll GET /strategy-lab/paper-trade/{session_id} until status is terminal. */
  private pollPaperTradingSession(labRecordId: string, sessionId: string): void {
    this.paperTradingPollSubs[labRecordId]?.unsubscribe();
    this.paperTradingPollSubs[labRecordId] = timer(3000, 3000)
      .pipe(
        switchMap(() => this.api.getPaperTradingSession(sessionId)),
        takeWhile((res) => res.session.status === 'running', true),
      )
      .subscribe({
        next: (res) => {
          this._paperTradingSessions.update((sessions) => ({ ...sessions, [labRecordId]: res.session }));
          if (res.session.status !== 'running') {
            delete this.paperTradingPollSubs[labRecordId];
          }
        },
        error: (err) => {
          delete this.paperTradingPollSubs[labRecordId];
          this._errors.next(extractErrorDetail(err, 'Paper trading polling failed.'));
        },
      });
  }

  /**
   * Preconditions: none.
   * Postconditions: every subscription this service owns (SSE, REST
   * fallback, active-run check, and every per-record paper-trading poller)
   * is unsubscribed; `paperTradingPollSubs` is empty; `events$` and
   * `errors$` are completed. No timer scheduled by this service fires again
   * after this call returns.
   */
  ngOnDestroy(): void {
    this.sseSub?.unsubscribe();
    this.pollSub?.unsubscribe();
    this.activeRunCheckSub?.unsubscribe();
    for (const sub of Object.values(this.paperTradingPollSubs)) {
      sub.unsubscribe();
    }
    this.paperTradingPollSubs = {};
    this._events.complete();
    this._errors.complete();
  }
}
