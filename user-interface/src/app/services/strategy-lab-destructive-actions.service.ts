import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable, Subject } from 'rxjs';

import { InvestmentApiService } from './investment-api.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { NotificationService } from '../core/notification.service';
import { extractErrorDetail } from '../shared/extract-error-detail';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';
import type { ConfirmDialogData } from '../shared/confirm-dialog/confirm-dialog.component';
import type { StrategyLabRecord } from '../models';

/**
 * Owns per-record deletion and "clear all" strategy lab data, including the
 * shared destructive-action confirmation dialog. Extracted from
 * `StrategyLabComponent` (previously `deleteRecord`/`clearAllLabData`/
 * `confirmDestructive`), mirroring `StrategyLabPaperTradingService`'s
 * relationship to its host: the host has no direct dialog/API access into
 * this service's concerns — success/refresh and error banner needs are
 * surfaced as observables (`resultsRefreshRequested$`, `errors$`) the
 * component subscribes to once, rather than this service reaching into a
 * component method or signal directly.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component (alongside `StrategyLabRunService`) so its lifecycle is scoped
 * to that component.
 */
@Injectable()
export class StrategyLabDestructiveActionsService {
  private readonly api = inject(InvestmentApiService);
  private readonly confirmService = inject(ConfirmDestructiveService);
  private readonly notify = inject(NotificationService);
  private readonly runService = inject(StrategyLabRunService);
  private readonly destroyRef = inject(DestroyRef);

  readonly clearingAll = signal(false);
  /** Lab record id currently being deleted (disables actions on that card). */
  readonly deletingLabRecordId = signal<string | null>(null);

  /** Error-banner messages for this service's own concerns; `null` clears. */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  /** Emits after a successful delete/clear-all, so the component reloads results. */
  private readonly _resultsRefreshRequested = new Subject<void>();
  readonly resultsRefreshRequested$: Observable<void> = this._resultsRefreshRequested.asObservable();

  /**
   * Open the shared Material confirm dialog for a destructive action.
   *
   * Delegates to the generic `ConfirmDestructiveService` which owns the
   * re-entrancy guard and dialog orchestration.
   *
   * Preconditions: `data.title` and `data.message` are non-empty; the caller
   *   treats a `false` emission as "do not proceed".
   * Postconditions: emits exactly once — `true` only when the user confirms,
   *   `false` on cancel, backdrop/ESC dismissal, or when a confirmation is
   *   already pending.
   */
  private confirmDestructive(data: ConfirmDialogData) {
    return this.confirmService.confirm(data);
  }

  /**
   * Deletes a strategy lab record after user confirmation.
   *
   * Opens a destructive-action confirmation dialog describing the record's
   * hypothesis; if the user cancels or dismisses it, no request is sent and
   * state is left untouched. On confirmation, calls the API to delete the
   * record (and its backtest and paper-trading sessions), tracks the
   * in-flight deletion via `deletingLabRecordId`, and on success clears the
   * error banner, requests a results refresh, and shows a success toast. On
   * failure, clears the in-flight marker and surfaces the error via `errors$`.
   *
   * Preconditions:
   *   `record` must be a valid `StrategyLabRecord` with a populated
   *   `lab_record_id`. `strategy.hypothesis` may be missing on legacy
   *   records; the confirmation message falls back to an empty string
   *   in that case rather than throwing.
   *
   * Postconditions:
   *   Either no observable change occurs (cancelled), or the record is
   *   deleted server-side, `deletingLabRecordId` returns to `null`, and
   *   either `resultsRefreshRequested$` emits with a success notification or
   *   `errors$` emits the failure detail.
   */
  deleteRecord(record: StrategyLabRecord): void {
    const id = record.lab_record_id;
    const hypothesis = record.strategy?.hypothesis ?? '';
    const shortHyp = hypothesis.slice(0, 60) + (hypothesis.length > 60 ? '…' : '');
    this.confirmDestructive({
      title: 'Delete strategy lab run',
      message: `Delete this strategy lab run?\n\n${shortHyp}\n\nThis removes the record, its backtest, and any paper-trading sessions for it. This cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this._errors.next(null);
        this.deletingLabRecordId.set(id);
        this.api
          .deleteStrategyLabRecord(id)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.deletingLabRecordId.set(null);
              this._resultsRefreshRequested.next();
              this.notify.saved('Strategy lab run deleted.');
            },
            error: (err) => {
              this.deletingLabRecordId.set(null);
              this._errors.next(extractErrorDetail(err, 'Failed to delete strategy.'));
            },
          });
      });
  }

  /**
   * Prompts the user to confirm before wiping all strategy lab data, then calls the API to
   * delete every lab run, lab strategy/backtest, and paper-trading session and refreshes the view.
   */
  clearAllLabData(): void {
    this.confirmDestructive({
      title: 'Clear all strategy lab data',
      message:
        'Delete ALL strategy lab runs, lab strategies/backtests, and paper-trading sessions?\n\nThis cannot be undone.',
      confirmLabel: 'Delete all',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this._errors.next(null);
        this.clearingAll.set(true);
        this.api
          .clearStrategyLabStorage()
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.clearingAll.set(false);
              this.runService.clearPaperTradingSessions();
              this._resultsRefreshRequested.next();
              this.notify.saved('Strategy lab data cleared.');
            },
            error: (err) => {
              this.clearingAll.set(false);
              this._errors.next(extractErrorDetail(err, 'Failed to clear strategy lab data.'));
            },
          });
      });
  }
}
