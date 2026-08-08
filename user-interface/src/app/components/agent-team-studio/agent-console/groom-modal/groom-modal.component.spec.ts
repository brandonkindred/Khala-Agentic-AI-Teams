import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { GroomModalComponent } from './groom-modal.component';
import { ProductDeliveryService } from '../../../../services/product-delivery.service';
import type { GroomResult } from '../../../../models/product-delivery.model';

describe('GroomModalComponent', () => {
  let api: { groom: ReturnType<typeof vi.fn> };
  let dialogRef: { close: ReturnType<typeof vi.fn> };

  const result: GroomResult = {
    product_id: 'p1',
    method: 'wsjf',
    ranked: [
      {
        kind: 'story',
        id: 's1',
        title: 'Pay rent',
        score: 12.5,
        wsjf_score: 12.5,
        rice_score: 8,
        rationale: 'High customer value.',
      },
    ],
    rationale: 'Overall: stable pipeline.',
  };

  function build() {
    TestBed.configureTestingModule({
      imports: [GroomModalComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        { provide: ProductDeliveryService, useValue: api },
        { provide: MatDialogRef, useValue: dialogRef },
        { provide: MAT_DIALOG_DATA, useValue: { productId: 'p1' } },
      ],
    });
    return TestBed.createComponent(GroomModalComponent);
  }

  beforeEach(() => {
    api = { groom: vi.fn().mockReturnValue(of(result)) };
    dialogRef = { close: vi.fn() };
  });

  it('previews with persist=false on open', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(api.groom).toHaveBeenCalledWith('p1', 'wsjf', false);
    expect(fixture.componentInstance.preview()).toEqual(result);
  });

  it('re-runs the preview when the method changes', () => {
    const fixture = build();
    fixture.detectChanges();
    api.groom.mockClear();
    fixture.componentInstance.setMethod('rice');
    expect(api.groom).toHaveBeenCalledWith('p1', 'rice', false);
  });

  it('does not refetch when the same method is chosen', () => {
    const fixture = build();
    fixture.detectChanges();
    api.groom.mockClear();
    fixture.componentInstance.setMethod('wsjf');
    expect(api.groom).not.toHaveBeenCalled();
  });

  it('surfaces a preview error', () => {
    api.groom = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('boom');
    expect(fixture.componentInstance.preview()).toBeNull();
  });

  it('applies grooming with persist=true and closes the dialog', () => {
    const fixture = build();
    fixture.detectChanges();
    api.groom.mockClear();
    api.groom.mockReturnValue(of(result));
    fixture.componentInstance.apply();
    expect(api.groom).toHaveBeenCalledWith('p1', 'wsjf', true);
    expect(dialogRef.close).toHaveBeenCalledWith({ applied: true });
  });

  it('surfaces an apply error without closing the dialog', () => {
    const fixture = build();
    fixture.detectChanges();
    api.groom = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'busy' } })));
    fixture.componentInstance.apply();
    expect(dialogRef.close).not.toHaveBeenCalled();
    expect(fixture.componentInstance.error()).toBe('busy');
  });

  it('discards without applying', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.discard();
    expect(dialogRef.close).toHaveBeenCalledWith({ applied: false });
  });

  it('falls back to the method-specific score when score is null', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(
      fixture.componentInstance.scoreFor({ score: null, wsjf_score: 7, rice_score: 3 }),
    ).toBe(7);
    fixture.componentInstance.setMethod('rice');
    expect(
      fixture.componentInstance.scoreFor({ score: null, wsjf_score: 7, rice_score: 3 }),
    ).toBe(3);
  });
});
