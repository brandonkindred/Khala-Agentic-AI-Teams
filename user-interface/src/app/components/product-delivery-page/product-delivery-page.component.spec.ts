import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { ProductDeliveryPageComponent } from './product-delivery-page.component';
import { ProductDeliveryService } from '../../services/product-delivery.service';

/**
 * All three tabs (Backlog/Sprints/Feedback) are eagerly rendered by the
 * `mat-tab-group` and each calls `listProducts()` on init, so the shared
 * `ProductDeliveryService` mock must cover all of them.
 */
function createServiceSpy() {
  return {
    listProducts: vi.fn().mockReturnValue(of([])),
    getBacklog: vi.fn().mockReturnValue(of(null)),
    listSprints: vi.fn().mockReturnValue(of([])),
    listFeedback: vi.fn().mockReturnValue(of([])),
  };
}

describe('ProductDeliveryPageComponent', () => {
  let fixture: ComponentFixture<ProductDeliveryPageComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ProductDeliveryPageComponent, NoopAnimationsModule],
      providers: [{ provide: ProductDeliveryService, useValue: createServiceSpy() }],
    }).compileComponents();

    fixture = TestBed.createComponent(ProductDeliveryPageComponent);
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('shows the "Product Delivery" title', () => {
    const h1: HTMLElement = fixture.nativeElement.querySelector('h1');
    expect(h1.textContent?.trim()).toBe('Product Delivery');
  });

  it('mounts the Backlog tab component (the initially active tab)', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-backlog-tab')).toBeTruthy();
  });

  it('renders Backlog, Sprints, and Feedback tab labels', () => {
    const labels = Array.from(fixture.nativeElement.querySelectorAll('.tab-label')).map(
      (el) => (el as HTMLElement).textContent?.trim(),
    );
    expect(labels).toEqual(['Backlog', 'Sprints', 'Feedback']);
  });
});
