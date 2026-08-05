import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { FeedbackTabComponent } from './feedback-tab.component';
import { ProductDeliveryService } from '../../../../services/product-delivery.service';
import type {
  BacklogTree,
  FeedbackItem,
  Product,
} from '../../../../models/product-delivery.model';

const products: Product[] = [
  { id: 'p1', name: 'P', description: '', vision: '', author: 'a', created_at: '', updated_at: '' },
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
              title: 'Pay rent',
              summary: '',
              user_story: '',
              status: 'proposed',
              estimate_points: null,
              wsjf_score: null,
              rice_score: null,
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

const feedback: FeedbackItem = {
  id: 'f1',
  product_id: 'p1',
  source: 'qa',
  raw_payload: { note: 'broken' },
  severity: 'normal',
  status: 'open',
  linked_story_id: null,
  sprint_id: null,
  author: 'a',
  created_at: '',
  updated_at: '',
};

describe('FeedbackTabComponent', () => {
  let api: {
    listProducts: ReturnType<typeof vi.fn>;
    listFeedback: ReturnType<typeof vi.fn>;
    getBacklog: ReturnType<typeof vi.fn>;
    linkFeedback: ReturnType<typeof vi.fn>;
  };

  function build() {
    TestBed.configureTestingModule({
      imports: [FeedbackTabComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        { provide: ProductDeliveryService, useValue: api },
      ],
    });
    return TestBed.createComponent(FeedbackTabComponent);
  }

  beforeEach(() => {
    api = {
      listProducts: vi.fn().mockReturnValue(of(products)),
      listFeedback: vi.fn().mockReturnValue(of([feedback])),
      getBacklog: vi.fn().mockReturnValue(of(tree)),
      linkFeedback: vi.fn().mockReturnValue(of({ ...feedback, linked_story_id: 's1' })),
    };
  });

  it('loads products, stories, and open feedback by default', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(api.listFeedback).toHaveBeenCalledWith('p1', 'open');
    expect(api.getBacklog).toHaveBeenCalledWith('p1');
    expect(fixture.componentInstance.items()).toEqual([feedback]);
    expect(fixture.componentInstance.stories().map((s) => s.id)).toEqual(['s1']);
  });

  it('refetches when the status filter changes', () => {
    const fixture = build();
    fixture.detectChanges();
    api.listFeedback.mockClear();
    fixture.componentInstance.setStatusFilter(null);
    expect(api.listFeedback).toHaveBeenCalledWith('p1', null);
    fixture.componentInstance.setStatusFilter('closed');
    expect(api.listFeedback).toHaveBeenCalledWith('p1', 'closed');
  });

  it('surfaces a feedback-list error', () => {
    api.listFeedback = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'unavailable' } })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('unavailable');
    expect(fixture.componentInstance.items()).toEqual([]);
  });

  it('falls back to empty stories silently when backlog fails', () => {
    api.getBacklog = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'down' } })));
    const fixture = build();
    fixture.detectChanges();
    // Feedback list still loaded; stories array empty.
    expect(fixture.componentInstance.stories()).toEqual([]);
    expect(fixture.componentInstance.error()).toBeNull();
  });

  it('resolves the linked story title from the cached story list', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.storyTitle(null)).toBe('—');
    expect(fixture.componentInstance.storyTitle('s1')).toBe('Pay rent');
    expect(fixture.componentInstance.storyTitle('missing')).toBe('missing');
  });

  it('renders a payload snippet, truncated to 120 chars', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.payloadSnippet(feedback)).toBe('{"note":"broken"}');
    const long = 'x'.repeat(300);
    const snip = fixture.componentInstance.payloadSnippet({
      ...feedback,
      raw_payload: { note: long },
    });
    expect(snip.endsWith('…')).toBe(true);
    expect(snip.length).toBe(120);
  });

  it('PATCHes the link via applyLink and refreshes the row', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.applyLink(feedback, 's1');
    expect(api.linkFeedback).toHaveBeenCalledWith('f1', 's1');
    expect(fixture.componentInstance.items()[0].linked_story_id).toBe('s1');
  });

  it('rolls back optimistic link on PATCH failure with the server detail', () => {
    api.linkFeedback = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'cross product feedback link' } })));
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.applyLink(feedback, 's1');
    expect(fixture.componentInstance.items()[0].linked_story_id).toBeNull();
    expect(fixture.componentInstance.error()).toBe('cross product feedback link');
  });

  it('unlinks via applyLink(null)', () => {
    const linked: FeedbackItem = { ...feedback, linked_story_id: 's1' };
    api.linkFeedback = vi.fn().mockReturnValue(of({ ...linked, linked_story_id: null }));
    api.listFeedback = vi.fn().mockReturnValue(of([linked]));
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.applyLink(linked, null);
    expect(api.linkFeedback).toHaveBeenCalledWith('f1', null);
    expect(fixture.componentInstance.items()[0].linked_story_id).toBeNull();
  });
});
