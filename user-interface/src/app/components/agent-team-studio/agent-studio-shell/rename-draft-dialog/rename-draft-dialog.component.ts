import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { AgentStudioApiService } from '../../../../services/agent-studio-api.service';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

export interface RenameDraftDialogData {
  draftId: string;
  initialName: string;
}

export type RenameDraftDialogResult = AgentStudioDraftSummary;

/**
 * Name-only rename dialog. Owns its PATCH call and stays open on failure.
 *
 * Invariants: never writes the draft payload; `busy()` true means cancel is a no-op.
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
  private readonly api = inject(AgentStudioApiService);
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
   * Postconditions: on success, `ref.close(summary)`. On failure, `busy() === false`,
   *   `serverError()` is non-empty, dialog stays open.
   */
  submit(): void {
    const trimmed = this.name().trim();
    if (!trimmed) {
      this.serverError.set('Name is required.');
      return;
    }
    this.busy.set(true);
    this.serverError.set(null);
    this.api.renameDraft(this.data.draftId, trimmed).subscribe({
      next: (summary) => this.ref.close(summary),
      error: (err) => {
        this.busy.set(false);
        this.serverError.set(err?.error?.detail ?? err?.message ?? 'Failed to rename draft.');
      },
    });
  }

  /** Preconditions: none. Postconditions: no-op while `busy()`; otherwise closes with no result. */
  cancel(): void {
    if (this.busy()) return;
    this.ref.close();
  }
}
