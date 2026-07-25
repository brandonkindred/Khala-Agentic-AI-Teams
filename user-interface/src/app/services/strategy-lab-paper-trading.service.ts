import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable, Subject } from 'rxjs';
import { InvestmentApiService } from './investment-api.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { extractErrorDetail } from '../shared/extract-error-detail';
import { publishabilitySkipLabel } from '../components/strategy-lab/strategy-lab.formatters';
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

  /** Sortable recency key for a paper-trading session. */
  private paperSessionRecencyKey(s: PaperTradingSession): string {
    return s.started_at || s.completed_at || '';
  }

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
          // Backend returns a "running" session immediately; runService stores it
          // so the UI shows in-progress state, then polls until the worker finishes.
          this.runService.trackPaperTradingSession(record.lab_record_id, res.session);
          this.startingPaperTrade.set(null);
        },
        error: (err) => {
          this.startingPaperTrade.set(null);
          this._errors.next(extractErrorDetail(err, 'Paper trading failed.'));
        },
      });
  }

  getPaperSession(record: StrategyLabRecord): PaperTradingSession | null {
    return this.runService.paperTradingSessions()[record.lab_record_id] ?? null;
  }
}
