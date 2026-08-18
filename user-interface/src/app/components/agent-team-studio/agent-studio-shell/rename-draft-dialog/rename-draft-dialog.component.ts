import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { extractErrorDetail } from '../../../../shared/extract-error-detail';
import { AgentStudioFacade } from '../../../../services/agent-studio.facade';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

export interface RenameDraftDialogData {
  draftId: string;
  initialName: string;
}

export type RenameDraftDialogResult = AgentStudioDraftSummary;

/**
 * Name-only rename dialog. Owns its PATCH call and stays open on failure.
 *
 * Invariants: never writes the draft payload; `busy()` true means both submit()
 * and cancel() are no-ops until the in-flight request settles.
 */
@Component({
  selector: 'app-rename-draft-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './rename-draft-dialog.component.html',
  styleUrl: './rename-draft-dialog.component.scss',
})
export class RenameDraftDialogComponent {
  private readonly facade = inject(AgentStudioFacade);
  private readonly destroyRef = inject(DestroyRef);
  readonly data = inject<RenameDraftDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<RenameDraftDialogComponent, RenameDraftDialogResult>>(
    MatDialogRef,
  );

  readonly name = signal<string>('');
  readonly busy = signal<boolean>(false);
  readonly serverError = signal<string | null>(null);

  constructor() {
    this.name.set(this.data.initialName);
  }

  /**
   * Preconditions: none — a blank/whitespace name is ordinary invalid user input.
   * Postconditions: no-op while `busy()` is already true — a second in-flight
   *   request would race the first and finish `busy()` while the first is
   *   still pending. Otherwise, on success, `ref.close(summary)`. On failure,
   *   `busy() === false`, `serverError()` is non-empty, dialog stays open.
   */
  submit(): void {
    if (this.busy()) return;
    const trimmed = this.name().trim();
    if (!trimmed) {
      this.serverError.set('Name is required.');
      return;
    }
    this.busy.set(true);
    this.serverError.set(null);
    this.facade
      .renameDraft(this.data.draftId, trimmed)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (summary) => this.ref.close(summary),
        error: (err) => {
          this.busy.set(false);
          this.serverError.set(extractErrorDetail(err, 'Failed to rename draft.'));
        },
      });
  }

  /** Preconditions: none. Postconditions: no-op while `busy()`; otherwise closes with no result. */
  cancel(): void {
    if (this.busy()) return;
    this.ref.close();
  }
}
