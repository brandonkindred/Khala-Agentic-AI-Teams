import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MatDialogModule,
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';

/**
 * Data handed to the dialog. `initialName`/`initialDescription` are populated
 * when the user clicks "rename" on an existing saved input.
 */
export interface SaveInputDialogData {
  mode: 'create' | 'rename';
  initialName?: string;
  initialDescription?: string;
}

/** Result returned when the user clicks Save. */
export interface SaveInputDialogResult {
  name: string;
  description: string | null;
}

@Component({
  selector: 'app-save-input-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './save-input-dialog.component.html',
  styleUrl: './save-input-dialog.component.scss',
})
export class SaveInputDialogComponent {
  readonly data = inject<SaveInputDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<SaveInputDialogComponent, SaveInputDialogResult>>(
    MatDialogRef,
  );

  readonly name = signal<string>('');
  readonly description = signal<string>('');
  readonly serverError = signal<string | null>(null);
  readonly busy = signal<boolean>(false);

  constructor() {
    this.name.set(this.data.initialName ?? '');
    this.description.set(this.data.initialDescription ?? '');
  }

  submit(): void {
    const trimmed = this.name().trim();
    if (!trimmed) {
      this.serverError.set('Name is required.');
      return;
    }
    this.ref.close({
      name: trimmed,
      description: this.description().trim() || null,
    });
  }

  setServerError(message: string): void {
    this.serverError.set(message);
    this.busy.set(false);
  }

  cancel(): void {
    this.ref.close();
  }
}
