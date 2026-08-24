import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { vi } from 'vitest';
import {
  GenerateStrategiesDialogComponent,
  type GenerateStrategiesDialogData,
} from './generate-strategies-dialog.component';

describe('GenerateStrategiesDialogComponent', () => {
  let component: GenerateStrategiesDialogComponent;
  let fixture: ComponentFixture<GenerateStrategiesDialogComponent>;
  let ref: { close: ReturnType<typeof vi.fn> };
  /** The `batchCountMax` signal of the most recently created fixture's data — a fresh one per `createFixture()` call. */
  let batchCountMax: ReturnType<typeof signal<number>>;

  // Read-only reference values shared by assertions across tests — never
  // passed as `data` directly, since `batchCountMax` must be a fresh signal
  // per fixture (see `createFixture`) rather than shared/mutated across tests.
  const DEFAULTS = {
    batchSize: 10,
    batchCount: 1,
    batchSizeMin: 1,
    batchSizeMax: 25,
    batchCountMin: 1,
    batchCountMaxInitial: 100,
    categoryOptions: [
      { value: 'stocks', label: 'Stocks', icon: 'show_chart' },
      { value: 'crypto', label: 'Crypto', icon: 'currency_bitcoin' },
    ],
    selectedCategories: ['stocks', 'crypto'],
  } as const;

  async function createFixture(overrides: Partial<Omit<GenerateStrategiesDialogData, 'batchCountMax'>> = {}) {
    batchCountMax = signal(DEFAULTS.batchCountMaxInitial);
    const data: GenerateStrategiesDialogData = {
      batchSize: DEFAULTS.batchSize,
      batchCount: DEFAULTS.batchCount,
      batchSizeMin: DEFAULTS.batchSizeMin,
      batchSizeMax: DEFAULTS.batchSizeMax,
      batchCountMin: DEFAULTS.batchCountMin,
      batchCountMax,
      categoryOptions: [...DEFAULTS.categoryOptions],
      selectedCategories: [...DEFAULTS.selectedCategories],
      ...overrides,
    };
    ref = { close: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [GenerateStrategiesDialogComponent, NoopAnimationsModule],
      providers: [
        { provide: MAT_DIALOG_DATA, useValue: data },
        { provide: MatDialogRef, useValue: ref },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(GenerateStrategiesDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    return fixture;
  }

  it('seeds batch/category state from the injected data', async () => {
    await createFixture();
    expect(component.batchSize).toBe(10);
    expect(component.batchCount()).toBe(1);
    expect(component.selectedCategories()).toEqual(['stocks', 'crypto']);
  });

  it('renders the batch inputs, category toggles, and submit button', async () => {
    const f = await createFixture();
    const el: HTMLElement = f.nativeElement;
    expect(el.querySelectorAll('mat-form-field').length).toBe(2);
    expect(el.querySelectorAll('mat-button-toggle').length).toBe(2);
    const submitBtn = Array.from(el.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Run 10 strateg'),
    );
    expect(submitBtn).toBeTruthy();
  });

  it('categoriesValid is false when no category is selected', async () => {
    await createFixture();
    component.selectedCategories.set([]);
    expect(component.categoriesValid).toBe(false);
  });

  it('disables the submit button when no category is selected', async () => {
    const f = await createFixture();
    component.onCategoriesChanged([]);
    f.detectChanges();

    const buttons = Array.from(f.nativeElement.querySelectorAll('button'));
    const submitBtn = buttons.find((b) => b.textContent?.includes('Run'));
    expect(submitBtn?.disabled).toBe(true);
  });

  it('onBatchSizeChange clamps out-of-range input immediately, so the label never shows a value the run would reduce', async () => {
    await createFixture();
    component.onBatchSizeChange(999);
    expect(component.batchSize).toBe(DEFAULTS.batchSizeMax);
    expect(component.runButtonLabel()).toContain(`${DEFAULTS.batchSizeMax}`);
  });

  it('onBatchCountChange clamps out-of-range input immediately', async () => {
    await createFixture();
    component.onBatchCountChange(-5);
    expect(component.batchCount()).toBe(DEFAULTS.batchCountMin);
  });

  it('runButtonLabel adapts to single- vs multi-batch mode', async () => {
    await createFixture();
    expect(component.runButtonLabel()).toBe('Run 10 strategies');

    component.batchSize = 5;
    component.batchCount.set(3);
    expect(component.runButtonLabel()).toBe('Run 5 × 3 = 15 strategies');
  });

  it('re-clamps batchCount when BATCH_COUNT_MAX() shrinks while the dialog is open', async () => {
    await createFixture();
    component.onBatchCountChange(80);
    expect(component.batchCount()).toBe(80);

    // Simulate a config fetch resolving with a lower operator-configured
    // limit while the dialog is still open.
    batchCountMax.set(3);
    fixture.detectChanges();

    expect(component.batchCount()).toBe(3);
    expect(component.runButtonLabel()).not.toContain('80');
  });

  it('re-clamps the initial batchCount if the caller-supplied value is already out of bounds', async () => {
    await createFixture({ batchCount: 999 });
    expect(component.batchCount()).toBe(DEFAULTS.batchCountMaxInitial);
  });

  it('submit closes the dialog with the current configuration and categoriesTouched=true when categories were edited', async () => {
    await createFixture();
    component.batchSize = 8;
    component.batchCount.set(2);
    component.onCategoriesChanged(['crypto']);

    component.submit();

    expect(ref.close).toHaveBeenCalledWith({
      batchSize: 8,
      batchCount: 2,
      selectedCategories: ['crypto'],
      categoriesTouched: true,
    });
  });

  it('submit reports categoriesTouched=false when the category toggles were never touched', async () => {
    await createFixture();
    component.batchSize = 8;

    component.submit();

    expect(ref.close).toHaveBeenCalledWith({
      batchSize: 8,
      batchCount: 1,
      selectedCategories: ['stocks', 'crypto'],
      categoriesTouched: false,
    });
  });

  it('submit clamps a typed/pasted value outside the min/max bounds before closing', async () => {
    // Native [min]/[max] on <input type="number"> only constrain the spinner
    // arrows, not a typed or pasted value — submit() must clamp explicitly so
    // the confirmed label and the value sent can never diverge.
    await createFixture();
    // Bypass the normal onBatchSizeChange/onBatchCountChange clamp paths
    // (as a direct/programmatic field write would) to exercise submit()'s
    // own defensive clamp.
    component.batchSize = 999;
    component.batchCount.set(-5);

    component.submit();

    expect(ref.close).toHaveBeenCalledWith({
      batchSize: DEFAULTS.batchSizeMax,
      batchCount: DEFAULTS.batchCountMin,
      selectedCategories: ['stocks', 'crypto'],
      categoriesTouched: false,
    });
  });

  it('submit is a no-op when no category is selected', async () => {
    await createFixture();
    component.onCategoriesChanged([]);

    component.submit();

    expect(ref.close).not.toHaveBeenCalled();
  });

  it('cancel closes the dialog with no result', async () => {
    await createFixture();
    component.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });
});
