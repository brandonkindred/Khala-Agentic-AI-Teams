import { DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable, defer } from 'rxjs';
import { finalize } from 'rxjs/operators';

import { NotificationService } from '../core/notification.service';
import { extractErrorDetail } from './extract-error-detail';
import { ConfirmDestructiveService } from './confirm-destructive.service';
import type { ConfirmDialogData } from './confirm-dialog/confirm-dialog.component';

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
 * Delegates dialog opening and re-entrancy guard to `ConfirmDestructiveService`.
 *
 * Usage: instantiate once per service (in the constructor or as a field), then
 * call `execute()` for each destructive action. The helper owns no signals or
 * subjects — callers supply callbacks so they remain in control of state shape.
 *
 * Note: before each confirmed action the helper resets caller error state by
 * invoking `onError(null)`, ensuring stale errors are cleared before a new
 * request is attempted.
 */
export class DestructiveActionHelper {
  constructor(
    private readonly confirmService: ConfirmDestructiveService,
    private readonly notify: NotificationService,
    private readonly destroyRef: DestroyRef,
    private readonly onError: (message: string | null) => void,
  ) {}

  /**
   * Opens the confirm dialog and, on confirmation, executes the API call.
   *
   * Re-entrancy guard (owned by `ConfirmDestructiveService`) prevents
   * stacked dialogs from rapid double-activation.
   * On confirmation, clears any previous error by calling `onError(null)`,
   * then invokes `onStart`, runs `apiCall` (wrapped in `defer` to catch
   * synchronous throws), and calls `onFinally` on completion via `finalize`.
   * On success calls `opts.onSuccess` and shows a toast via NotificationService.
   * On failure calls the `onError` callback provided at construction with
   * the extracted error message.
   *
   * @param opts Configuration for this specific destructive action.
   * @param successToast Message for the success notification toast.
   * @param onStart Optional callback fired just before the API call (e.g. set loading signal).
   * @param onFinally Optional callback fired after success, error, or unsubscribe (e.g. clear loading signal).
   */
  execute<T>(
    opts: DestructiveActionOptions<T>,
    successToast: string,
    onStart?: () => void,
    onFinally?: () => void,
  ): void {
    this.confirmService
      .confirm(opts.dialogData)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this.onError(null);
        onStart?.();
        defer(() => opts.apiCall())
          .pipe(
            takeUntilDestroyed(this.destroyRef),
            finalize(() => onFinally?.()),
          )
          .subscribe({
            next: (result) => {
              opts.onSuccess(result);
              this.notify.saved(successToast);
            },
            error: (err) => {
              this.onError(extractErrorDetail(err, opts.errorFallback));
            },
          });
      });
  }
}
