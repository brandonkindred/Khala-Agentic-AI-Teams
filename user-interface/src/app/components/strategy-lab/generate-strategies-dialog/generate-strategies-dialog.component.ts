import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MAT_DIALOG_DATA,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import type { AssetCategoryOption } from '../strategy-lab.component';

export interface GenerateStrategiesDialogData {
  batchSize: number;
  batchCount: number;
  batchSizeMin: number;
  batchSizeMax: number;
  batchCountMin: number;
  batchCountMax: number;
  categoryOptions: AssetCategoryOption[];
  selectedCategories: string[];
}

export interface GenerateStrategiesDialogResult {
  batchSize: number;
  batchCount: number;
  selectedCategories: string[];
  /**
   * Whether the user actually edited the category toggles in this dialog.
   * When false, `selectedCategories` is just the seeded snapshot — the
   * caller should prefer its own current selection (which may have been
   * refreshed by a config update while the dialog was open) over this stale
   * copy rather than reconciling it as if it were a deliberate choice.
   */
  categoriesTouched: boolean;
}

/**
 * Modal form for configuring and launching a Strategy Lab run: strategies-per-
 * batch, number of batches, and the asset-category constraint. Opened from
 * `StrategyLabComponent`'s "Generate strategies" button; closes with a
 * `GenerateStrategiesDialogResult` on submit, or `undefined` on cancel.
 */
@Component({
  selector: 'app-generate-strategies-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatButtonToggleModule,
  ],
  templateUrl: './generate-strategies-dialog.component.html',
  styleUrl: './generate-strategies-dialog.component.scss',
})
export class GenerateStrategiesDialogComponent {
  readonly data = inject<GenerateStrategiesDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<
    MatDialogRef<GenerateStrategiesDialogComponent, GenerateStrategiesDialogResult>
  >(MatDialogRef);

  readonly BATCH_SIZE_MIN = this.data.batchSizeMin;
  readonly BATCH_SIZE_MAX = this.data.batchSizeMax;
  readonly BATCH_COUNT_MIN = this.data.batchCountMin;
  readonly BATCH_COUNT_MAX = this.data.batchCountMax;
  readonly categoryOptions = this.data.categoryOptions;

  batchSize = this.data.batchSize;
  batchCount = this.data.batchCount;
  readonly selectedCategories = signal<string[]>(this.data.selectedCategories);
  /** Set once the user touches the category toggles — see `GenerateStrategiesDialogResult.categoriesTouched`. */
  private categoriesTouched = false;

  /** A run requires at least one selected category. */
  get categoriesValid(): boolean {
    return this.selectedCategories().length > 0;
  }

  onCategoriesChanged(values: string[]): void {
    this.selectedCategories.set(values);
    this.categoriesTouched = true;
  }

  /**
   * Clamp on every edit (not just at submit time) so `runButtonLabel()` —
   * and the input itself — never display a value the run would silently
   * reduce; the native [min]/[max] attributes only constrain the spinner
   * arrows, not a typed or pasted value.
   */
  onBatchSizeChange(value: number): void {
    this.batchSize = this.clamp(value, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
  }

  onBatchCountChange(value: number): void {
    this.batchCount = this.clamp(value, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX);
  }

  /** Label for the submit button — adapts to single- vs multi-batch mode. */
  runButtonLabel(): string {
    if (this.batchCount > 1) {
      const total = this.batchSize * this.batchCount;
      return `Run ${this.batchSize} × ${this.batchCount} = ${total} strategies`;
    }
    return `Run ${this.batchSize} strateg${this.batchSize === 1 ? 'y' : 'ies'}`;
  }

  submit(): void {
    if (!this.categoriesValid) {
      return;
    }
    // The native input [min]/[max] only constrain the spinner arrows, not
    // typed/pasted values — clamp here so the value actually sent (and the
    // label the user just confirmed) can never diverge from what
    // runNewStrategy() would otherwise silently re-clamp after the dialog
    // has already closed.
    const batchSize = this.clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = this.clamp(this.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX);
    this.ref.close({
      batchSize,
      batchCount,
      selectedCategories: this.selectedCategories(),
      categoriesTouched: this.categoriesTouched,
    });
  }

  private clamp(value: number, min: number, max: number): number {
    const n = Number.isFinite(value) ? Math.floor(value) : min;
    return Math.max(min, Math.min(max, n));
  }

  cancel(): void {
    this.ref.close();
  }
}
