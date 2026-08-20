import { Injectable, inject } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { Observable, of } from 'rxjs';
import { finalize, map } from 'rxjs/operators';

import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from './confirm-dialog/confirm-dialog.component';

/**
 * Lightweight shared service for destructive-action confirmation dialogs.
 *
 * Provides a re-entrancy guard that collapses rapid double-activation
 * (e.g. Enter pressed twice before the dialog traps focus) into a single
 * dialog open. Emits `true` on confirm, `false` on cancel/dismiss/guard.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle (and guard state) is scoped to that component.
 */
@Injectable()
export class ConfirmDestructiveService {
  private readonly dialog = inject(MatDialog);

  /** True while a destructive confirm dialog is open — blocks re-entrant opens. */
  private confirming = false;

  /**
   * Open the shared Material confirm dialog for a destructive action.
   *
   * Returns an observable that emits exactly once: `true` when the user
   * confirms, `false` on cancel, backdrop/ESC dismissal, or when a
   * confirmation is already pending. The re-entrancy guard is released
   * when the dialog closes.
   */
  confirm(data: ConfirmDialogData): Observable<boolean> {
    if (this.confirming) return of(false);
    this.confirming = true;
    return this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(
        ConfirmDialogComponent,
        { data },
      )
      .afterClosed()
      .pipe(
        map((result) => result === true),
        finalize(() => {
          this.confirming = false;
        }),
      );
  }
}
