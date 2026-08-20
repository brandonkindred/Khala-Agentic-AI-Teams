import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatDialog } from '@angular/material/dialog';
import { Observable, Subject, of } from 'rxjs';
import { finalize, map } from 'rxjs/operators';

import { AgentRunnerApiService } from './agent-runner-api.service';
import { NotificationService } from '../core/notification.service';
import { extractErrorDetail } from '../shared/extract-error-detail';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from '../shared/confirm-dialog/confirm-dialog.component';

/**
 * Owns destructive-action concerns (confirm dialog, re-entrancy guard, API
 * call, loading state, error surfacing) for the Agent Runner tab.
 *
 * Mirrors `StrategyLabDestructiveActionsService`: the host component delegates
 * `deleteSavedInput` and `tearDownSandbox` here rather than opening dialogs or
 * calling APIs itself. Success/refresh and error needs are surfaced as
 * observables that the component subscribes to once.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle is scoped to that component.
 */
@Injectable()
export class AgentRunnerDestructiveActionsService {
  private readonly api = inject(AgentRunnerApiService);
  private readonly dialog = inject(MatDialog);
  private readonly notify = inject(NotificationService);
  private readonly destroyRef = inject(DestroyRef);

  /** True while a destructive confirm dialog is open — blocks re-entrant opens. */
  private confirmingDestructive = false;

  /** Saved-input id currently being deleted (disables actions on that row). */
  readonly deletingSavedInputId = signal<string | null>(null);
  /** True while a sandbox teardown is in flight. */
  readonly tearingDown = signal(false);

  /** Error-banner messages for this service's concerns; `null` clears. */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  /** Emits the deleted saved-input id after a successful deletion. */
  private readonly _savedInputDeleted = new Subject<string>();
  readonly savedInputDeleted$: Observable<string> = this._savedInputDeleted.asObservable();

  /** Emits after a successful sandbox teardown so the component can update state. */
  private readonly _sandboxTornDown = new Subject<void>();
  readonly sandboxTornDown$: Observable<void> = this._sandboxTornDown.asObservable();

  /**
   * Open the shared Material confirm dialog for a destructive action.
   *
   * If a confirmation is already pending the method returns `of(false)`,
   * collapsing the window for rapid double-activation. The guard is released
   * in `finalize()` regardless of how the dialog closes.
   */
  private confirmDestructive(data: ConfirmDialogData): Observable<boolean> {
    if (this.confirmingDestructive) return of(false);
    this.confirmingDestructive = true;
    return this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .pipe(
        map((result) => result === true),
        finalize(() => {
          this.confirmingDestructive = false;
        }),
      );
  }

  /**
   * Deletes a saved input after user confirmation.
   *
   * Opens a destructive-action dialog; on confirmation calls the API, tracks
   * in-flight state via `deletingSavedInputId`, and on success emits through
   * `savedInputDeleted$` and shows a toast. On failure, surfaces the error
   * via `errors$`.
   */
  deleteSavedInput(savedId: string, savedName: string): void {
    this.confirmDestructive({
      title: 'Delete saved input',
      message: `Delete saved input "${savedName}"?\n\nThis cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this._errors.next(null);
        this.deletingSavedInputId.set(savedId);
        this.api
          .deleteSavedInput(savedId)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.deletingSavedInputId.set(null);
              this._savedInputDeleted.next(savedId);
              this.notify.saved('Saved input deleted.');
            },
            error: (err) => {
              this.deletingSavedInputId.set(null);
              this._errors.next(extractErrorDetail(err, 'Failed to delete saved input.'));
            },
          });
      });
  }

  /**
   * Tears down a sandbox after user confirmation.
   *
   * Opens a destructive-action dialog; on confirmation calls the API, tracks
   * in-flight state via `tearingDown`, and on success emits through
   * `sandboxTornDown$` and shows a toast. On failure, surfaces the error
   * via `errors$`.
   */
  tearDownSandbox(agentId: string, agentLabel: string): void {
    this.confirmDestructive({
      title: 'Tear down sandbox',
      message: `Tear down the ${agentLabel} sandbox?\n\nThe sandbox will need to be re-warmed before the next invocation.`,
      confirmLabel: 'Tear down',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this._errors.next(null);
        this.tearingDown.set(true);
        this.api
          .teardown(agentId)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.tearingDown.set(false);
              this._sandboxTornDown.next();
              this.notify.saved('Sandbox torn down.');
            },
            error: (err) => {
              this.tearingDown.set(false);
              this._errors.next(extractErrorDetail(err, 'Failed to tear down sandbox.'));
            },
          });
      });
  }
}
