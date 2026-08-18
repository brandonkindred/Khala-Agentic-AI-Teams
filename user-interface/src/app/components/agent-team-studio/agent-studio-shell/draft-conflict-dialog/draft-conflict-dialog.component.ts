import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatDialogModule, MatDialogRef } from '@angular/material/dialog';

/** Result of an explicit choice. Cancel / Escape / backdrop yield `undefined`. */
export type DraftConflictResult = 'save' | 'discard' | undefined;

/**
 * Load-conflict prompt (UX spec §2.4). Three actions; Cancel is initially
 * focused so Enter does not save or discard.
 *
 * Invariants: this dialog performs no HTTP and does not read Studio state.
 */
@Component({
  selector: 'app-draft-conflict-dialog',
  standalone: true,
  imports: [MatDialogModule, MatButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './draft-conflict-dialog.component.html',
  styleUrl: './draft-conflict-dialog.component.scss',
})
export class DraftConflictDialogComponent {
  readonly ref = inject<MatDialogRef<DraftConflictDialogComponent, DraftConflictResult>>(
    MatDialogRef,
  );

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with `'save'`.
   */
  save(): void {
    this.ref.close('save');
  }

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with `'discard'`.
   */
  discard(): void {
    this.ref.close('discard');
  }

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with no result (`undefined`).
   */
  cancel(): void {
    this.ref.close();
  }
}
