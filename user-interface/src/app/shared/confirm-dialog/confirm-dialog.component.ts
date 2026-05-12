import { Component, inject } from '@angular/core';
import {
  MAT_DIALOG_DATA,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';

export type ConfirmDialogVariant = 'danger' | 'warn' | 'default';

export interface ConfirmDialogData {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmDialogVariant;
}

@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [MatDialogModule, MatButtonModule],
  templateUrl: './confirm-dialog.component.html',
  styleUrl: './confirm-dialog.component.scss',
})
export class ConfirmDialogComponent {
  readonly data = inject<ConfirmDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<ConfirmDialogComponent, boolean>>(MatDialogRef);

  get confirmLabel(): string {
    return this.data.confirmLabel ?? 'Confirm';
  }

  get cancelLabel(): string {
    return this.data.cancelLabel ?? 'Cancel';
  }

  get variant(): ConfirmDialogVariant {
    return this.data.variant ?? 'default';
  }

  get confirmColor(): 'warn' | 'primary' {
    return this.variant === 'danger' ? 'warn' : 'primary';
  }

  /** True when Cancel should receive initial focus — safer default for destructive prompts. */
  get cancelIsInitiallyFocused(): boolean {
    return this.variant === 'danger';
  }

  confirm(): void {
    this.ref.close(true);
  }

  cancel(): void {
    this.ref.close(false);
  }
}
