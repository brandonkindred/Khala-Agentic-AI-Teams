import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { Observable, Subject } from 'rxjs';

import { InvestmentApiService } from './investment-api.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { NotificationService } from '../core/notification.service';
import { DestructiveActionHelper } from '../shared/destructive-action.helper';
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
 * Uses the shared `DestructiveActionHelper` for the confirm → execute flow.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component (alongside `StrategyLabRunService`) so its lifecycle is scoped
 * to that component.
 */
@Injectable()
export class StrategyLabDestructiveActionsService {
  private readonly api = inject(InvestmentApiService);
  private readonly runService = inject(StrategyLabRunService);
  private readonly destroyRef = inject(DestroyRef);

  /** Error-banner messages for this service's own concerns; `null` clears. */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  private readonly helper = new DestructiveActionHelper(
    inject(MatDialog),
    inject(NotificationService),
    this.destroyRef,
    (msg) => this._errors.next(msg),
  );

  readonly clearingAll = signal(false);
  /** Lab record id currently being deleted (disables actions on that card). */
  readonly deletingLabRecordId = signal<string | null>(null);

  /** Emits after a successful delete/clear-all, so the component reloads results. */
  private readonly _resultsRefreshRequested = new Subject<void>();
  readonly resultsRefreshRequested$: Observable<void> = this._resultsRefreshRequested.asObservable();

  /**
   * Deletes a strategy lab record after user confirmation.
   */
  deleteRecord(record: StrategyLabRecord): void {
    const id = record.lab_record_id;
    const hypothesis = record.strategy?.hypothesis ?? '';
    const shortHyp = hypothesis.slice(0, 60) + (hypothesis.length > 60 ? '…' : '');
    this.helper.execute(
      {
        dialogData: {
          title: 'Delete strategy lab run',
          message: `Delete this strategy lab run?\n\n${shortHyp}\n\nThis removes the record, its backtest, and any paper-trading sessions for it. This cannot be undone.`,
          confirmLabel: 'Delete',
          variant: 'danger',
        },
        apiCall: () => this.api.deleteStrategyLabRecord(id),
        onSuccess: () => this._resultsRefreshRequested.next(),
        errorFallback: 'Failed to delete strategy.',
      },
      'Strategy lab run deleted.',
      () => this.deletingLabRecordId.set(id),
      () => this.deletingLabRecordId.set(null),
    );
  }

  /**
   * Prompts the user to confirm before wiping all strategy lab data, then calls the API to
   * delete every lab run, lab strategy/backtest, and paper-trading session and refreshes the view.
   */
  clearAllLabData(): void {
    this.helper.execute(
      {
        dialogData: {
          title: 'Clear all strategy lab data',
          message:
            'Delete ALL strategy lab runs, lab strategies/backtests, and paper-trading sessions?\n\nThis cannot be undone.',
          confirmLabel: 'Delete all',
          variant: 'danger',
        },
        apiCall: () => this.api.clearStrategyLabStorage(),
        onSuccess: () => {
          this.runService.clearPaperTradingSessions();
          this._resultsRefreshRequested.next();
        },
        errorFallback: 'Failed to clear strategy lab data.',
      },
      'Strategy lab data cleared.',
      () => this.clearingAll.set(true),
      () => this.clearingAll.set(false),
    );
  }
}
