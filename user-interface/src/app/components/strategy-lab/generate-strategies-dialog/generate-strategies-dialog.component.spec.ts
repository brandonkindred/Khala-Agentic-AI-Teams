import { ComponentFixture, TestBed } from '@angular/core/testing';
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

  const DATA: GenerateStrategiesDialogData = {
    batchSize: 10,
    batchCount: 1,
    batchSizeMin: 1,
    batchSizeMax: 25,
    batchCountMin: 1,
    batchCountMax: 100,
    categoryOptions: [
      { value: 'stocks', label: 'Stocks', icon: 'show_chart' },
      { value: 'crypto', label: 'Crypto', icon: 'currency_bitcoin' },
    ],
    selectedCategories: ['stocks', 'crypto'],
  };

  async function createFixture(data: GenerateStrategiesDialogData = DATA) {
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
    expect(component.batchCount).toBe(1);
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

  it('runButtonLabel adapts to single- vs multi-batch mode', async () => {
    await createFixture();
    expect(component.runButtonLabel()).toBe('Run 10 strategies');

    component.batchSize = 5;
    component.batchCount = 3;
    expect(component.runButtonLabel()).toBe('Run 5 × 3 = 15 strategies');
  });

  it('submit closes the dialog with the current configuration', async () => {
    await createFixture();
    component.batchSize = 8;
    component.batchCount = 2;
    component.onCategoriesChanged(['crypto']);

    component.submit();

    expect(ref.close).toHaveBeenCalledWith({
      batchSize: 8,
      batchCount: 2,
      selectedCategories: ['crypto'],
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
