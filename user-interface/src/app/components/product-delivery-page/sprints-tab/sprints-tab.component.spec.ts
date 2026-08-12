import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { SprintsTabComponent } from './sprints-tab.component';
import { ProductDeliveryService } from '../../../services/product-delivery.service';
import type {
  Product,
  Sprint,
  SprintPlanResult,
} from '../../../models/product-delivery.model';

const products: Product[] = [
  { id: 'p1', name: 'P', description: '', vision: '', author: 'a', created_at: '', updated_at: '' },
];

const sprint: Sprint = {
  id: 'sp1',
  product_id: 'p1',
  name: 'Sprint 1',
  capacity_points: 21,
  starts_at: null,
  ends_at: null,
  status: 'draft',
  author: 'a',
  created_at: '',
  updated_at: '',
};

const planResult: SprintPlanResult = {
  sprint_id: 'sp1',
  selected_story_ids: ['s1', 's2'],
  skipped_story_ids: ['s3'],
  used_capacity: 8,
  remaining_capacity: 13,
  rationale: 'Picked stories that fit.',
};

describe('SprintsTabComponent', () => {
  let api: {
    listProducts: ReturnType<typeof vi.fn>;
    listSprints: ReturnType<typeof vi.fn>;
    planSprint: ReturnType<typeof vi.fn>;
  };

  function build() {
    TestBed.configureTestingModule({
      imports: [SprintsTabComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        { provide: ProductDeliveryService, useValue: api },
      ],
    });
    return TestBed.createComponent(SprintsTabComponent);
  }

  beforeEach(() => {
    api = {
      listProducts: vi.fn().mockReturnValue(of(products)),
      listSprints: vi.fn().mockReturnValue(of([sprint])),
      planSprint: vi.fn().mockReturnValue(of(planResult)),
    };
  });

  it('auto-selects the first product and loads sprints', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.selectedProductId()).toBe('p1');
    expect(api.listSprints).toHaveBeenCalledWith('p1');
    expect(fixture.componentInstance.sprints()).toEqual([sprint]);
  });

  it('renders the empty state when the product has no sprints', () => {
    api.listSprints = vi.fn().mockReturnValue(of([]));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.sprints()).toEqual([]);
    expect(fixture.componentInstance.error()).toBeNull();
  });

  it('surfaces sprint listing errors', () => {
    api.listSprints = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('boom');
    expect(fixture.componentInstance.sprints()).toEqual([]);
  });

  it('enables Plan only for draft/proposed/planning sprints', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.canPlan({ ...sprint, status: 'draft' })).toBe(true);
    expect(fixture.componentInstance.canPlan({ ...sprint, status: 'proposed' })).toBe(true);
    expect(fixture.componentInstance.canPlan({ ...sprint, status: 'in_progress' })).toBe(false);
    expect(fixture.componentInstance.canPlan({ ...sprint, status: 'done' })).toBe(false);
  });

  it('plans a sprint and surfaces the result + reload', () => {
    const fixture = build();
    fixture.detectChanges();
    api.listSprints.mockClear();
    fixture.componentInstance.planSprint(sprint);
    expect(api.planSprint).toHaveBeenCalledWith('sp1');
    expect(fixture.componentInstance.planResult()).toEqual(planResult);
    expect(api.listSprints).toHaveBeenCalledWith('p1');
  });

  it('surfaces plan errors and stops the spinner', () => {
    api.planSprint = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'no' } })));
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.planSprint(sprint);
    expect(fixture.componentInstance.planningId()).toBeNull();
    expect(fixture.componentInstance.error()).toBe('no');
  });

  it('dismisses the plan result panel', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.planSprint(sprint);
    expect(fixture.componentInstance.planResult()).not.toBeNull();
    fixture.componentInstance.dismissPlanResult();
    expect(fixture.componentInstance.planResult()).toBeNull();
  });
});
