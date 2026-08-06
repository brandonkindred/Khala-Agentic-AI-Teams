import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable, Subject } from 'rxjs';
import { InvestmentApiService } from './investment-api.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { extractErrorDetail } from '../shared/extract-error-detail';
import { publishabilitySkipLabel } from '../shared/publishability';
import type { PaperTradingSession, StrategyLabRecord } from '../models';

/**
 * Owns paper-trading initiation: the publishability guard, the POST that
 * starts a session, and the initial results fetch/dedupe/hydrate on load.
 * Session storage and per-record polling remain `StrategyLabRunService`'s
 * job (injected here and delegated to) — this service only owns the
 * one-shot calls that feed it, mirroring `StrategyLabActivityLogService`'s
 * relationship to `StrategyLabRunService`.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component (alongside `StrategyLabRunService`) so its lifecycle is scoped
 * to that component.
 *
 * Invariants: `errors$` emits exactly once per `runPaperTrading()` call that
 *   fails the publishability guard or the POST, and once with `null` per call
 *   that passes the guard; it never emits as a side effect of
 *   `loadPaperTradingResults()` or `getPaperSession()`. `paperTradingLabRecordId()`
 *   merges this service's own in-flight-POST tracking with `runService`'s
 *   tracked session id via `??`; it is NOT re-entrancy-safe across overlapping
 *   `runPaperTrading()` calls for two different records — both share the one
 *   `startingPaperTrade` signal, so whichever call's POST settles first clears
 *   it for both, and the still-pending call's record can transiently read as
 *   not-in-progress (or vice versa) until it too settles. This is a pre-existing
 *   limitation carried over unchanged from the original component code, not
 *   introduced by this extraction, and is masked in the shipped UI by the
 *   paper-trade button disabling whenever any record is in progress.
 */
@Injectable()
export class StrategyLabPaperTradingService {
  private readonly api = inject(InvestmentApiService);
  private readonly runService = inject(StrategyLabRunService);
  private readonly destroyRef = inject(DestroyRef);

  /** True while a "run paper trading" POST is in flight for this record, before runService takes over. */
  private readonly startingPaperTrade = signal<string | null>(null);
  /** Lab record id currently being paper traded — merges local in-flight state with runService's. */
  readonly paperTradingLabRecordId = computed(
    () => this.startingPaperTrade() ?? this.runService.paperTradingLabRecordId(),
  );

  /** Error-banner messages for this service's own concerns; `null` clears (emitted right before a POST fires). */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  /**
   * Fetches paper trading sessions and hydrates run-service state from them.
   *
   * Preconditions: none.
   * Postconditions: for each `lab_record_id`, only the most recent session
   * (by `paperSessionRecencyKey`) is kept and handed to
   * `runService.hydratePaperTradingSessions`, so any still-running sessions
   * resume polling.
   */
  loadPaperTradingResults(): void {
    this.api
      .getPaperTradingResults()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          const sessions: Record<string, PaperTradingSession> = {};
          for (const s of res.items) {
            // Keep the newest session per lab record, using started_at as the
            // recency key (completed_at is empty for still-running sessions, so
            // relying on it would systematically lose to older completed ones).
            const existing = sessions[s.lab_record_id];
            if (!existing || this.paperSessionRecencyKey(s) > this.paperSessionRecencyKey(existing)) {
              sessions[s.lab_record_id] = s;
            }
          }
          // Resumes polling for any sessions still running (e.g. after a page reload).
          this.runService.hydratePaperTradingSessions(sessions);
        },
      });
  }

  /**
   * Sortable recency key for a paper-trading session.
   *
   * Preconditions: none — tolerates a session with neither timestamp set.
   * Postconditions: returns `started_at` when present (a running session has
   *   one but no `completed_at` yet), else `completed_at`, else `''` — an
   *   ISO-8601 string ordering that ranks a more-recently-started session
   *   above an older one under plain `>` comparison.
   */
  private paperSessionRecencyKey(s: PaperTradingSession): string {
    return s.started_at || s.completed_at || '';
  }

  /**
   * Preconditions: `record` is a loaded lab row.
   * Postconditions: when `!record.is_publishable`, no API call is made,
   *   `errors$` emits one non-null "not publishable" message, and
   *   `paperTradingLabRecordId()` is left untouched. Otherwise `errors$`
   *   emits `null` synchronously (clearing any stale error banner before the
   *   POST fires), `paperTradingLabRecordId()` becomes `record.lab_record_id`
   *   until the POST settles, and then: on success,
   *   `runService.trackPaperTradingSession` is called with the returned
   *   session (handing polling off to `runService`); on failure, `errors$`
   *   emits the failure detail. Either way, once settled,
   *   `paperTradingLabRecordId()` reverts to mirroring
   *   `runService.paperTradingLabRecordId()`.
   */
  runPaperTrading(record: StrategyLabRecord): void {
    if (!record.is_publishable) {
      const reason = publishabilitySkipLabel(record);
      this._errors.next(
        'This strategy is not publishable and cannot be paper traded' +
          (reason ? ` (${reason})` : '.'),
      );
      return;
    }
    this._errors.next(null);
    this.startingPaperTrade.set(record.lab_record_id);
    this.api
      .runPaperTrading({ lab_record_id: record.lab_record_id })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          // Backend returns a non-terminal session immediately ('opening' when
          // live paper trading is enabled, 'running' on the legacy path);
          // runService stores it so the UI shows in-progress state, then polls
          // until the worker finishes.
          this.runService.trackPaperTradingSession(record.lab_record_id, res.session);
          this.startingPaperTrade.set(null);
        },
        error: (err) => {
          this.startingPaperTrade.set(null);
          this._errors.next(extractErrorDetail(err, 'Paper trading failed.'));
        },
      });
  }

  /**
   * Preconditions: `record` is a loaded lab row.
   * Postconditions: returns the session `runService` currently holds for
   *   `record.lab_record_id` (whatever its status), or `null` if none has
   *   been tracked or hydrated for it yet.
   */
  getPaperSession(record: StrategyLabRecord): PaperTradingSession | null {
    return this.runService.paperTradingSessions()[record.lab_record_id] ?? null;
  }
}
