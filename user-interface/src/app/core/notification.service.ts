import { Injectable, inject } from '@angular/core';
import { MatSnackBar } from '@angular/material/snack-bar';

/** Lifetime (ms) of a transient success confirmation. */
const SAVED_DURATION_MS = 3000;

/**
 * App-wide confirmation toasts. Centralizes the "saved" convention (label +
 * duration) so every screen confirms an action the same way and future screens
 * don't each re-derive the snackbar call.
 *
 * Invariants: a `saved()` toast always uses the same 'Dismiss' action and
 * {@link SAVED_DURATION_MS} duration.
 */
@Injectable({ providedIn: 'root' })
export class NotificationService {
  private readonly snackBar = inject(MatSnackBar);

  /**
   * Show a transient success confirmation.
   *
   * Preconditions: `message` is a non-empty, user-facing string.
   * Postconditions: opens a dismissible snackbar for {@link SAVED_DURATION_MS};
   * does not block and returns nothing.
   */
  saved(message: string): void {
    this.snackBar.open(message, 'Dismiss', { duration: SAVED_DURATION_MS });
  }
}
