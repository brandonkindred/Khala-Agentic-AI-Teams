import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { MatDialog } from '@angular/material/dialog';
import { ProductDeliveryService } from '../../../../services/product-delivery.service';
import { BacklogTabComponent } from './backlog-tab.component';
import { expectNoAxeViolations } from '../../../../testing/a11y';

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

    await expectNoAxeViolations(fixture.nativeElement);
  });

  it('has no axe violations with products and a backlog tree rendered', async () => {
    const ts = '2026-06-22T00:00:00Z';
    const audited = { author: 'u', created_at: ts, updated_at: ts };
    const product = { id: 'p1', ...audited, name: 'Acme', description: 'd', vision: 'v' };
    const story = {
      id: 's1', ...audited, title: 'Add login', summary: 's', status: 'todo',
      wsjf_score: 5, rice_score: null, epic_id: 'e1', user_story: 'As a user I can log in',
      estimate_points: 3, tasks: [], acceptance_criteria: [],
    };
    const epic = {
      id: 'e1', ...audited, title: 'Auth', summary: 's', status: 'todo',
      wsjf_score: null, rice_score: null, initiative_id: 'i1', stories: [story],
    };
    const initiative = {
      id: 'i1', ...audited, title: 'Onboarding', summary: 's', status: 'todo',
      wsjf_score: 8, rice_score: null, product_id: 'p1', epics: [epic],
    };
    const backlog = { product, initiatives: [initiative] };

    const api = {
      listProducts: vi.fn().mockReturnValue(of([product])),
      getBacklog: vi.fn().mockReturnValue(of(backlog)),
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

    // Guard: the product auto-selected and its initiative tree rendered.
    expect(fixture.nativeElement.querySelector('.initiative')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
