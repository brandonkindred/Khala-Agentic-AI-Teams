import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatDialogModule, MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import type { SystemicFinding } from '../../../models/coding-team.model';

/** Data handed to the dialog: the systemic findings synthesized for one review run. */
export interface CodeReviewSystemicFindingsDialogData {
  findings: readonly SystemicFinding[];
}

/**
 * Read-only view of one review run's synthesized systemic/cross-cutting
 * findings (`CodeReviewSummary.systemic_findings`): each pattern's title,
 * description, and related locations. Unlike the transcript dialog, the data
 * is already in hand (persisted on the review summary) — no fetch on open.
 */
@Component({
  selector: 'app-code-review-systemic-findings-dialog',
  standalone: true,
  imports: [CommonModule, MatDialogModule, MatButtonModule],
  templateUrl: './code-review-systemic-findings-dialog.component.html',
  styleUrl: './code-review-systemic-findings-dialog.component.scss',
})
export class CodeReviewSystemicFindingsDialogComponent {
  readonly data = inject<CodeReviewSystemicFindingsDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<CodeReviewSystemicFindingsDialogComponent>>(MatDialogRef);

  close(): void {
    this.ref.close();
  }
}
