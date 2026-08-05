import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { BacklogTabComponent } from './backlog-tab.component';
import { ProductDeliveryService } from '../../../../services/product-delivery.service';
import type { BacklogTree, Product } from '../../../../models/product-delivery.model';

const products: Product[] = [
  {
    id: 'p1',
    name: 'P',
    description: '',
    vision: '',
    author: 'a',
    created_at: '',
    updated_at: '',
  },
];

const tree: BacklogTree = {
  product: products[0],
  initiatives: [
    {
      id: 'i1',
      product_id: 'p1',
      title: 'I',
      summary: '',
      status: 'proposed',
      wsjf_score: null,
      rice_score: null,
      author: 'a',
      created_at: '',
      updated_at: '',
      epics: [
        {
          id: 'e1',
          initiative_id: 'i1',
          title: 'E',
          summary: '',
          status: 'proposed',
          wsjf_score: null,
          rice_score: null,
          author: 'a',
          created_at: '',
          updated_at: '',
          stories: [
            {
              id: 's1',
              epic_id: 'e1',
              title: 'S1',
              summary: '',
              user_story: '',
              status: 'proposed',
              estimate_points: 3,
              wsjf_score: 5,
              rice_score: 2,
              author: 'a',
              created_at: '',
              updated_at: '',
              tasks: [],
              acceptance_criteria: [],
            },
          ],
        },
      ],
    },
  ],
};

describe('BacklogTabComponent', () => {
  let api: {
    listProducts: ReturnType<typeof vi.fn>;
    getBacklog: ReturnType<typeof vi.fn>;
    patchStoryStatus: ReturnType<typeof vi.fn>;
    patchStoryScores: ReturnType<typeof vi.fn>;
  };

  function build() {
    TestBed.configureTestingModule({
      imports: [BacklogTabComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        { provide: ProductDeliveryService, useValue: api },
      ],
    });
    return TestBed.createComponent(BacklogTabComponent);
  }

  beforeEach(() => {
    api = {
      listProducts: vi.fn().mockReturnValue(of(products)),
      getBacklog: vi.fn().mockReturnValue(of(tree)),
      patchStoryStatus: vi.fn().mockReturnValue(of({ ok: true })),
      patchStoryScores: vi.fn().mockReturnValue(of({ ok: true })),
    };
  });

  it('loads products and auto-selects the first one', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.products()).toEqual(products);
    expect(fixture.componentInstance.selectedProductId()).toBe('p1');
    expect(api.getBacklog).toHaveBeenCalledWith('p1');
    expect(fixture.componentInstance.backlog()).toEqual(tree);
  });

  it('surfaces product-list errors', () => {
    api.listProducts = vi.fn().mockReturnValue(throwError(() => ({ message: 'down' })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('down');
  });

  it('surfaces backlog errors', () => {
    api.getBacklog = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('boom');
    expect(fixture.componentInstance.backlog()).toBeNull();
  });

  it('opens the drawer with story values pre-filled', () => {
    const fixture = build();
    fixture.detectChanges();
    const story = tree.initiatives[0].epics[0].stories[0];
    fixture.componentInstance.openStoryDrawer(story);
    expect(fixture.componentInstance.drawerStory()?.id).toBe('s1');
    expect(fixture.componentInstance.drawerStatus()).toBe('proposed');
    expect(fixture.componentInstance.drawerWsjf()).toBe('5');
    expect(fixture.componentInstance.drawerRice()).toBe('2');
  });

  it('persists status + scores when saving the drawer', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.openStoryDrawer(tree.initiatives[0].epics[0].stories[0]);
    fixture.componentInstance.drawerStatus.set('in_progress');
    fixture.componentInstance.drawerWsjf.set('9');
    fixture.componentInstance.drawerRice.set('3');
    fixture.componentInstance.saveDrawer();
    expect(api.patchStoryStatus).toHaveBeenCalledWith('s1', { status: 'in_progress' });
    expect(api.patchStoryScores).toHaveBeenCalledWith('s1', {
      wsjf_score: 9,
      rice_score: 3,
    });
    // Drawer closes after successful save.
    expect(fixture.componentInstance.drawerStory()).toBeNull();
  });

  it('rejects non-numeric scores before sending any request', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.openStoryDrawer(tree.initiatives[0].epics[0].stories[0]);
    fixture.componentInstance.drawerWsjf.set('not-a-number');
    fixture.componentInstance.saveDrawer();
    expect(api.patchStoryStatus).not.toHaveBeenCalled();
    expect(api.patchStoryScores).not.toHaveBeenCalled();
    expect(fixture.componentInstance.drawerError()).toMatch(/numeric/);
  });

  it('rolls back the optimistic edit when status PATCH fails', () => {
    api.patchStoryStatus = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.openStoryDrawer(tree.initiatives[0].epics[0].stories[0]);
    fixture.componentInstance.drawerStatus.set('done');
    fixture.componentInstance.saveDrawer();
    // Drawer stays open with an error message; tree status reverts.
    expect(fixture.componentInstance.drawerError()).toBe('nope');
    const updated = fixture.componentInstance.backlog()!.initiatives[0].epics[0].stories[0];
    expect(updated.status).toBe('proposed');
  });

  it('reloads the backlog when groom modal closes with applied=true', () => {
    const fixture = build();
    fixture.detectChanges();
    api.getBacklog.mockClear();
    fixture.componentInstance.onGroomClosed({ applied: true });
    expect(api.getBacklog).toHaveBeenCalledWith('p1');
  });

  it('does not reload when groom modal is discarded', () => {
    const fixture = build();
    fixture.detectChanges();
    api.getBacklog.mockClear();
    fixture.componentInstance.onGroomClosed({ applied: false });
    expect(api.getBacklog).not.toHaveBeenCalled();
  });

  it('handles a closed-without-result groom modal', () => {
    const fixture = build();
    fixture.detectChanges();
    api.getBacklog.mockClear();
    fixture.componentInstance.onGroomClosed();
    expect(api.getBacklog).not.toHaveBeenCalled();
  });
});
