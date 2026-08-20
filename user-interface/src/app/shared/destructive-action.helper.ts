import { DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatDialog } from '@angular/material/dialog';
import { Observable, of } from 'rxjs';
import { finalize, map } from 'rxjs/operators';

import { NotificationService } from '../core/notification.service';
import { extractErrorDetail } from './extract-error-detail';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from './confirm-dialog/confirm-dialog.component';

/**
 * Options for a single destructive-action invocation.
 *
 * Generic parameter `T` is the API call's emission type on success.
 */
export interface DestructiveActionOptions<T = unknown> {
  /** Data for the confirm dialog (title, message, variant, labels). */
  dialogData: ConfirmDialogData;
  /** Factory returning the API observable to subscribe to on confirmation. */
  apiCall: () => Observable<T>;
  /** Called on successful API response. */
  onSuccess: (result: T) => void;
  /** Fallback error string when extractErrorDetail cannot find one. */
  errorFallback: string;
}

/**
 * Reusable destructive-action orchestrator.
 *
 * Encapsulates the confirm-dialog → re-entrancy guard → API call → error/toast
 * flow shared across all component-scoped destructive-action services.
 *
 * Usage: instantiate once per service (in the constructor or as a field), then
 * call `execute()` for each destructive action. The helper owns no signals or
 * subjects — callers supply callbacks so they remain in control of state shape.
 */
export class DestructiveActionHelper {
  /** True while a confirm dialog is open — blocks re-entrant opens. */
  private confirming = false;

  constructor(
    private readonly dialog: MatDialog,
    private readonly notify: NotificationService,
    private readonly destroyRef: DestroyRef,
    private readonly onError: (message: string | null) => void,
  ) {}

  /**
   * Opens the confirm dialog and, on confirmation, executes the API call.
   *
   * Re-entrancy guard prevents stacked dialogs from rapid double-activation.
   * On success calls `opts.onSuccess` and shows a toast via NotificationService.
   * On failure calls the `onError` callback provided at construction.
   *
   * @param opts Configuration for this specific destructive action.
   * @param successToast Message for the success notification toast.
   * @param onStart Optional callback fired just before the API call (e.g. set loading signal).
   * @param onFinally Optional callback fired after both success and error (e.g. clear loading signal).
   */
  execute<T>(
    opts: DestructiveActionOptions<T>,
    successToast: string,
    onStart?: () => void,
    onFinally?: () => void,
  ): void {
    this.confirmDestructive(opts.dialogData)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this.onError(null);
        onStart?.();
        opts
          .apiCall()
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: (result) => {
              onFinally?.();
              opts.onSuccess(result);
              this.notify.saved(successToast);
            },
            error: (err) => {
              onFinally?.();
              this.onError(extractErrorDetail(err, opts.errorFallback));
            },
          });
      });
  }

  private confirmDestructive(data: ConfirmDialogData): Observable<boolean> {
    if (this.confirming) return of(false);
    this.confirming = true;
    return this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .pipe(
        map((result) => result === true),
        finalize(() => {
          this.confirming = false;
        }),
      );
  }
}
