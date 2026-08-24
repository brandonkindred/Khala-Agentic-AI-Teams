import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { Observable, Subject } from 'rxjs';

import { AgentConsoleApiService } from '../../../../services/agent-console-api.service';
import { NotificationService } from '../../../../core/notification.service';
import { ConfirmDestructiveService } from '../../../../shared/confirm-destructive.service';
import { DestructiveActionHelper } from '../../../../shared/destructive-action.helper';
import type { RunSummary } from '../../../../models/agent-history.model';

/**
 * Owns run deletion for `AgentRunHistoryComponent`, including the shared
 * destructive-action confirmation dialog. Mirrors
 * `StrategyLabDestructiveActionsService`'s relationship to its host: the
 * component has no direct dialog/API access into this service's concerns —
 * a successful delete is surfaced as `runDeleted$` for the component to
 * subscribe to once, rather than this service reaching into the component's
 * `runs` signal directly.
 *
 * Uses the shared `DestructiveActionHelper` for the confirm -> execute flow.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle is scoped to that component.
 */
@Injectable()
export class AgentRunHistoryDestructiveActionsService {
  private readonly api = inject(AgentConsoleApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** Error-banner message for this service's own concerns; `null` clears. */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  private readonly helper = new DestructiveActionHelper(
    inject(ConfirmDestructiveService),
    inject(NotificationService),
    this.destroyRef,
    (msg) => this._errors.next(msg),
  );

  /** Run id currently being deleted (disables actions on that row). */
  readonly deletingRunId = signal<string | null>(null);

  /** Emits the deleted run's id after a successful delete. */
  private readonly _runDeleted = new Subject<string>();
  readonly runDeleted$: Observable<string> = this._runDeleted.asObservable();

  /**
   * Deletes a run after user confirmation.
   *
   * Preconditions: `run.id` and `run.trace_id` are non-empty.
   * Postconditions: on completion (success or failure) `deletingRunId` is
   *   reset to `null`. On success, emits `run.id` through `runDeleted$` and
   *   shows a success toast. On failure, emits an error message through
   *   `errors$` and no toast is shown.
   */
  deleteRun(run: RunSummary): void {
    this.helper.execute(
      {
        dialogData: {
          title: 'Delete run',
          message: `Delete run ${run.trace_id.slice(0, 8)}? This can't be undone.`,
          confirmLabel: 'Delete',
          variant: 'danger',
        },
        apiCall: () => this.api.deleteRun(run.id),
        onSuccess: () => this._runDeleted.next(run.id),
        errorFallback: 'Failed to delete run.',
      },
      'Run deleted.',
      () => this.deletingRunId.set(run.id),
      () => this.deletingRunId.set(null),
    );
  }
}
