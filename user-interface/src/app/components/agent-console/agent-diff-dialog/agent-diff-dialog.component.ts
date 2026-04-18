import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MatDialogModule,
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { AgentRunnerApiService } from '../../../services/agent-runner-api.service';
import type {
  DiffResult,
  DiffSide,
  RunSummary,
  SavedInput,
} from '../../../models/agent-history.model';

/**
 * Data handed to the dialog.
 *
 * - `agentId` scopes the candidate selectors (history + saved inputs).
 * - `initialLeft` / `initialRight` are the preselected sides. The right
 *   side is always optional — the user picks something to compare against.
 */
export interface AgentDiffDialogData {
  agentId: string;
  initialLeft: DiffSide;
  initialLeftLabel: string;
}

@Component({
  selector: 'app-agent-diff-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatSelectModule,
    MatFormFieldModule,
  ],
  templateUrl: './agent-diff-dialog.component.html',
  styleUrl: './agent-diff-dialog.component.scss',
})
export class AgentDiffDialogComponent implements OnInit {
  readonly data = inject<AgentDiffDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<AgentDiffDialogComponent>>(MatDialogRef);
  private readonly api = inject(AgentRunnerApiService);

  readonly runs = signal<RunSummary[]>([]);
  readonly savedInputs = signal<SavedInput[]>([]);
  readonly loadingCandidates = signal<boolean>(true);
  readonly candidateError = signal<string | null>(null);

  readonly rightSelection = signal<string | null>(null);
  readonly rightSideChoice = signal<'input' | 'output'>('output');

  readonly diffResult = signal<DiffResult | null>(null);
  readonly diffing = signal<boolean>(false);
  readonly diffError = signal<string | null>(null);

  ngOnInit(): void {
    this.loadCandidates();
  }

  private loadCandidates(): void {
    this.api.listRuns(this.data.agentId, null, 50).subscribe({
      next: (runs) => {
        this.runs.set(runs);
        this.api.listSavedInputs(this.data.agentId).subscribe({
          next: (inputs) => {
            this.savedInputs.set(inputs);
            this.loadingCandidates.set(false);
          },
          error: () => {
            this.savedInputs.set([]);
            this.loadingCandidates.set(false);
          },
        });
      },
      error: (err) => {
        this.candidateError.set(err?.error?.detail ?? err?.message ?? 'Failed to load candidates');
        this.loadingCandidates.set(false);
      },
    });
  }

  runDiff(): void {
    const right = this.resolveRightSide();
    if (right === null) {
      this.diffError.set('Pick something to compare against.');
      return;
    }
    this.diffing.set(true);
    this.diffError.set(null);
    this.api.diff({ left: this.data.initialLeft, right }).subscribe({
      next: (result) => {
        this.diffResult.set(result);
        this.diffing.set(false);
      },
      error: (err) => {
        this.diffError.set(err?.error?.detail ?? err?.message ?? 'Diff failed');
        this.diffing.set(false);
      },
    });
  }

  private resolveRightSide(): DiffSide | null {
    const token = this.rightSelection();
    if (!token) return null;
    if (token.startsWith('run:')) {
      return {
        kind: 'run',
        ref: token.slice(4),
        side: this.rightSideChoice(),
      };
    }
    if (token.startsWith('saved:')) {
      return { kind: 'saved_input', ref: token.slice(6) };
    }
    return null;
  }

  close(): void {
    this.ref.close();
  }

  /**
   * Angular template helper — returns the category for a unified-diff line
   * so the SCSS can colour-code it.
   */
  lineClass(line: string): string {
    if (line.startsWith('+++') || line.startsWith('---')) return 'diff-header';
    if (line.startsWith('@@')) return 'diff-hunk';
    if (line.startsWith('+')) return 'diff-add';
    if (line.startsWith('-')) return 'diff-del';
    return 'diff-ctx';
  }
}
