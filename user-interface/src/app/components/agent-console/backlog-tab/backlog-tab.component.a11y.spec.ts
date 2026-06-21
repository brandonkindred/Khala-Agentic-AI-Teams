import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { MatDialog } from '@angular/material/dialog';
import { ProductDeliveryService } from '../../../services/product-delivery.service';
import { BacklogTabComponent } from './backlog-tab.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('BacklogTabComponent a11y', () => {
  it('has no axe violations in the empty (no products) state', async () => {
    const api = {
      listProducts: vi.fn().mockReturnValue(of([])),
      getBacklog: vi.fn(),
      patchStoryStatus: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [BacklogTabComponent, NoopAnimationsModule],
      providers: [
        { provide: ProductDeliveryService, useValue: api },
        { provide: MatDialog, useValue: { open: vi.fn() } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(BacklogTabComponent);
    fixture.detectChanges();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    // Guard: the tab shell rendered, so axe audits the real toolbar.
    expect(fixture.nativeElement.querySelector('.backlog-tab')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
