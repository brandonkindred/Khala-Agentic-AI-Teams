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

/**
 * Data handed to the dialog by the shell. `draftId` selects create vs update
 * (spec §3.5: "Re-saving the same draft updates it in place") — `null` means
 * this session has no server draft yet (POST), non-null means re-save (PUT).
 * `initialName` pre-fills the field: the current draft's name on re-save, or
 * `null` on first save (the dialog then falls back to a timestamp default).
 */
export interface SaveDraftDialogData {
  draftId: string | null;
  initialName: string | null;
  payload: Record<string, unknown>;
}

/** Result on Save: the server's summary, so the shell can bind the session to it. */
export type SaveDraftDialogResult = AgentStudioDraftSummary;

/**
 * Save-draft name popover (spec §3.5). Unlike `SaveInputDialogComponent`,
 * this dialog performs its own create/update HTTP call and stays open on
 * failure so the user can retry without retyping the name — the same
 * "dialog owns its own I/O" pattern already used by
 * `AddAgentFromRegistryDialogComponent`.
 */
@Component({
  selector: 'app-save-draft-dialog',
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
  templateUrl: './save-draft-dialog.component.html',
  styleUrl: './save-draft-dialog.component.scss',
})
export class SaveDraftDialogComponent {
  private readonly api = inject(AgentStudioApiService);
  readonly data = inject<SaveDraftDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<SaveDraftDialogComponent, SaveDraftDialogResult>>(
    MatDialogRef,
  );

  readonly name = signal<string>('');
  readonly busy = signal<boolean>(false);
  readonly serverError = signal<string | null>(null);

  constructor() {
    this.name.set(this.data.initialName ?? SaveDraftDialogComponent.defaultTimestampName());
  }

  /**
   * Client-side timestamp default shown when no draft name exists yet (spec
   * §3.5: "pre-filled with a timestamp default, editable"). Purely a UI
   * default — the backend independently generates its own default when
   * `name` is omitted on create, but this dialog always sends `name`
   * explicitly, so the two never need to agree on format.
   */
  static defaultTimestampName(): string {
    return new Date().toLocaleString();
  }

  /**
   * Confirm: create or update the server draft with the current name/payload.
   *
   * Preconditions: none — a blank/whitespace name is ordinary invalid user
   *   input, rejected below rather than a caller contract violation.
   * Postconditions: on success, `ref.close(summary)` is called and this
   *   instance is destroyed. On failure, `busy() === false`, `serverError()`
   *   holds a non-empty message, and the dialog stays open.
   */
  submit(): void {
    const trimmed = this.name().trim();
    if (!trimmed) {
      this.serverError.set('Name is required.');
      return;
    }
    this.busy.set(true);
    this.serverError.set(null);
    const req = { name: trimmed, payload: this.data.payload };
    const call = this.data.draftId
      ? this.api.updateDraft(this.data.draftId, req)
      : this.api.createDraft(req);
    call.subscribe({
      next: (summary) => this.ref.close(summary),
      error: (err) => {
        this.busy.set(false);
        this.serverError.set(err?.error?.detail ?? err?.message ?? 'Failed to save draft.');
      },
    });
  }

  /**
   * Dismiss without saving. No-op while a save is in flight, so an in-flight
   * response never lands after the dialog is already gone.
   */
  cancel(): void {
    if (this.busy()) return;
    this.ref.close();
  }
}
