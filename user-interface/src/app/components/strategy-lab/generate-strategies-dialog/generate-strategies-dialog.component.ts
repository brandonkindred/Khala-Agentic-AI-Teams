import { Component, Signal, effect, inject, signal } from '@angular/core';
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
import { clamp } from '../../../shared/clamp.util';
import type { AssetCategoryOption } from '../strategy-lab.component';

export interface GenerateStrategiesDialogData {
  batchSize: number;
  batchCount: number;
  batchSizeMin: number;
  batchSizeMax: number;
  batchCountMin: number;
  /**
   * The parent's own `BATCH_COUNT_MAX` signal — passed by reference, not
   * read at open time, so this dialog stays synchronized if a config fetch
   * resolves (changing the operator-configured limit) while it's still open.
   */
  batchCountMax: Signal<number>;
  categoryOptions: AssetCategoryOption[];
  selectedCategories: string[];
}

export interface GenerateStrategiesDialogResult {
  batchSize: number;
  /** Postcondition: always within `[batchCountMin, batchCountMax()]` as of the moment the dialog closed. */
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
  /** Live — reads the parent's own signal, so it tracks a config-driven change while this dialog is open. */
  readonly BATCH_COUNT_MAX = this.data.batchCountMax;
  readonly categoryOptions = this.data.categoryOptions;

  // Clamped immediately at construction (not just on the next edit) so a
  // caller-supplied out-of-bounds value is never displayed unclamped.
  batchSize = clamp(this.data.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
  readonly batchCount = signal(clamp(this.data.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX()));

  readonly selectedCategories = signal<string[]>(this.data.selectedCategories);
  /** Set once the user touches the category toggles — see `GenerateStrategiesDialogResult.categoriesTouched`. */
  private categoriesTouched = false;

  /**
   * Keeps `batchCount` inside `[BATCH_COUNT_MIN, BATCH_COUNT_MAX()]` even
   * when `BATCH_COUNT_MAX()` shrinks while this dialog is open (a config
   * fetch resolving with a lower operator-configured limit) — without this,
   * the confirmation label could keep showing a count the run would
   * silently reduce after the dialog closes.
   */
  private readonly syncBatchCountToMax = effect(() => {
    const max = this.BATCH_COUNT_MAX();
    this.batchCount.update((v) => clamp(v, this.BATCH_COUNT_MIN, max));
  });

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
    this.batchSize = clamp(value, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
  }

  onBatchCountChange(value: number): void {
    this.batchCount.set(clamp(value, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX()));
  }

  /** Label for the submit button — adapts to single- vs multi-batch mode. */
  runButtonLabel(): string {
    const batchCount = this.batchCount();
    if (batchCount > 1) {
      const total = this.batchSize * batchCount;
      return `Run ${this.batchSize} × ${batchCount} = ${total} strategies`;
    }
    return `Run ${this.batchSize} strateg${this.batchSize === 1 ? 'y' : 'ies'}`;
  }

  submit(): void {
    if (!this.categoriesValid) {
      return;
    }
    // batchSize/batchCount are already kept clamped continuously (construction,
    // every edit via onBatchSizeChange/onBatchCountChange, and — for batchCount —
    // every BATCH_COUNT_MAX() change via the effect above); this is a final
    // defensive clamp so the value closed with is correct even if something
    // mutated the fields directly, bypassing those paths.
    const batchSize = clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = clamp(this.batchCount(), this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX());
    this.ref.close({
      batchSize,
      batchCount,
      selectedCategories: this.selectedCategories(),
      categoriesTouched: this.categoriesTouched,
    });
  }

  cancel(): void {
    this.ref.close();
  }
}
