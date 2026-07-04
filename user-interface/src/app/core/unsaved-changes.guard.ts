import { inject } from '@angular/core';
import { CanDeactivateFn } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import {
  ConfirmDialogComponent,
  ConfirmDialogData,
} from '../shared/confirm-dialog/confirm-dialog.component';

/**
 * A component that can veto navigation away while it holds unsaved edits.
 * Implement `hasUnsavedChanges` to return true when leaving would lose work.
 */
export interface HasUnsavedChanges {
  hasUnsavedChanges(): boolean;
}

/**
 * Route guard that prompts before navigating away from a component with
 * unsaved edits, using the shared confirm dialog.
 *
 * Preconditions: attach only to routes whose component implements
 * `HasUnsavedChanges`.
 * Postconditions: returns `true` immediately when the component reports no
 * unsaved changes; otherwise opens a Discard/Keep-editing dialog and resolves
 * to the user's choice (true = discard and leave, false = stay). Never throws.
 */
export const unsavedChangesGuard: CanDeactivateFn<HasUnsavedChanges> = (
  component,
): Observable<boolean> | boolean => {
  if (!component?.hasUnsavedChanges?.()) return true;

  const data: ConfirmDialogData = {
    title: 'Discard unsaved changes?',
    message: 'You have edits that have not been saved. Leaving now discards them.',
    confirmLabel: 'Discard changes',
    cancelLabel: 'Keep editing',
    // 'danger' focuses Cancel and colors the destructive button, so an
    // accidental Enter keeps editing rather than discarding unsaved work.
    variant: 'danger',
  };
  return inject(MatDialog)
    .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
    .afterClosed()
    .pipe(map((confirmed) => confirmed === true));
};
