import {
  ChangeDetectionStrategy,
  Component,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import {
  MAT_DIALOG_DATA,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { ProductDeliveryService } from '../../../services/product-delivery.service';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';
import type {
  GroomMethod,
  GroomResult,
} from '../../../models/product-delivery.model';

export interface GroomModalData {
  productId: string;
}

export interface GroomModalResult {
  /** True iff the user clicked Apply and the persist call succeeded. */
  applied: boolean;
}

/**
 * Groom modal — preview-then-commit grooming.
 *
 * On open: call `POST /groom` with `persist=false` to fetch the ranked
 * preview. The user reviews the per-item rationale, optionally swaps
 * the method (WSJF ↔ RICE), then clicks Apply to commit. Apply re-fires
 * `POST /groom` with `persist=true`. "Discard" closes the dialog
 * without writing.
 */
@Component({
  selector: 'app-groom-modal',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatButtonToggleModule,
    MatDialogModule,
    MatIconModule,
    MatProgressSpinnerModule,
    InlineBannerComponent,
  ],
  templateUrl: './groom-modal.component.html',
  styleUrl: './groom-modal.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GroomModalComponent {
  private readonly api = inject(ProductDeliveryService);
  readonly data = inject<GroomModalData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<GroomModalComponent, GroomModalResult>>(MatDialogRef);

  readonly method = signal<GroomMethod>('wsjf');
  readonly preview = signal<GroomResult | null>(null);
  readonly previewing = signal<boolean>(false);
  readonly applying = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  constructor() {
    this.runPreview();
  }

  setMethod(method: GroomMethod): void {
    if (this.method() === method) return;
    this.method.set(method);
    this.runPreview();
  }

  runPreview(): void {
    this.previewing.set(true);
    this.error.set(null);
    this.api.groom(this.data.productId, this.method(), false).subscribe({
      next: (res) => {
        this.preview.set(res);
        this.previewing.set(false);
      },
      error: (err) => {
        this.preview.set(null);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to preview grooming.');
        this.previewing.set(false);
      },
    });
  }

  apply(): void {
    if (this.applying() || this.previewing()) return;
    this.applying.set(true);
    this.error.set(null);
    this.api.groom(this.data.productId, this.method(), true).subscribe({
      next: () => {
        this.applying.set(false);
        this.ref.close({ applied: true });
      },
      error: (err) => {
        this.applying.set(false);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to apply grooming.');
      },
    });
  }

  discard(): void {
    this.ref.close({ applied: false });
  }

  /** Score column to surface in the table, driven by the picker. */
  scoreFor(item: { wsjf_score: number | null; rice_score: number | null; score: number | null }):
    | number
    | null {
    if (item.score !== null) return item.score;
    return this.method() === 'wsjf' ? item.wsjf_score : item.rice_score;
  }
}
