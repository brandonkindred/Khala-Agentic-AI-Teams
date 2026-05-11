import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { ProductDeliveryService } from './product-delivery.service';
import { environment } from '../../environments/environment';
import type {
  BacklogTree,
  FeedbackItem,
  GroomResult,
  Product,
  Sprint,
  SprintPlanResult,
} from '../models/product-delivery.model';

describe('ProductDeliveryService', () => {
  let service: ProductDeliveryService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.productDeliveryApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [ProductDeliveryService],
    });
    service = TestBed.inject(ProductDeliveryService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('is created', () => {
    expect(service).toBeTruthy();
  });

  // Products / backlog --------------------------------------------------------

  it('lists products via GET /products', () => {
    const stub: Product[] = [
      {
        id: 'p1',
        name: 'P',
        description: '',
        vision: '',
        author: 'a',
        created_at: '2026-05-01T00:00:00Z',
        updated_at: '2026-05-01T00:00:00Z',
      },
    ];
    service.listProducts().subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/products`);
    expect(req.request.method).toBe('GET');
    req.flush(stub);
  });

  it('fetches a backlog tree via GET /products/{id}/backlog', () => {
    const stub: BacklogTree = {
      product: {
        id: 'p1',
        name: 'P',
        description: '',
        vision: '',
        author: 'a',
        created_at: '2026-05-01T00:00:00Z',
        updated_at: '2026-05-01T00:00:00Z',
      },
      initiatives: [],
    };
    service.getBacklog('p1').subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/products/p1/backlog`);
    expect(req.request.method).toBe('GET');
    req.flush(stub);
  });

  it('encodes product ids in backlog URL', () => {
    service.getBacklog('p/with space').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/products/p%2Fwith%20space/backlog`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  // Story edits ---------------------------------------------------------------

  it('patches a story status via PATCH /story/{id}/status', () => {
    service.patchStoryStatus('s1', { status: 'in_progress' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/story/s1/status`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ status: 'in_progress' });
    req.flush({ ok: true, kind: 'story', id: 's1', status: 'in_progress' });
  });

  it('patches story scores via PATCH /story/{id}/scores', () => {
    service.patchStoryScores('s1', { wsjf_score: 12.5, rice_score: null }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/story/s1/scores`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ wsjf_score: 12.5, rice_score: null });
    req.flush({ ok: true, kind: 'story', id: 's1' });
  });

  // Grooming ------------------------------------------------------------------

  it('previews grooming with persist=false', () => {
    const stub: GroomResult = {
      product_id: 'p1',
      method: 'wsjf',
      ranked: [],
      rationale: '',
    };
    service.groom('p1', 'wsjf', false).subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/groom`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ product_id: 'p1', method: 'wsjf', persist: false });
    req.flush(stub);
  });

  it('commits grooming with persist=true', () => {
    service.groom('p1', 'rice', true).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/groom`);
    expect(req.request.body).toEqual({ product_id: 'p1', method: 'rice', persist: true });
    req.flush({ product_id: 'p1', method: 'rice', ranked: [], rationale: '' });
  });

  // Sprints -------------------------------------------------------------------

  it('lists sprints with product_id query param', () => {
    const stub: Sprint[] = [];
    service.listSprints('p1').subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/sprints?product_id=p1`);
    expect(req.request.method).toBe('GET');
    req.flush(stub);
  });

  it('gets a sprint with stories via GET /sprints/{id}', () => {
    service.getSprint('sp1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sprints/sp1`);
    expect(req.request.method).toBe('GET');
    req.flush({ sprint: {}, stories: [], acceptance_criteria_by_story_id: {} });
  });

  it('plans a sprint without overriding capacity', () => {
    const stub: SprintPlanResult = {
      sprint_id: 'sp1',
      selected_story_ids: [],
      skipped_story_ids: [],
      used_capacity: 0,
      remaining_capacity: 0,
      rationale: '',
    };
    service.planSprint('sp1').subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/sprints/sp1/plan`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toBeNull();
    req.flush(stub);
  });

  it('plans a sprint with a capacity override', () => {
    service.planSprint('sp1', 21).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sprints/sp1/plan`);
    expect(req.request.body).toEqual({ capacity_points: 21 });
    req.flush({});
  });

  // Releases ------------------------------------------------------------------

  it('lists releases with product_id query param', () => {
    service.listReleases('p1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/releases?product_id=p1`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  // Feedback ------------------------------------------------------------------

  it('lists feedback with product_id only', () => {
    service.listFeedback('p1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/feedback?product_id=p1`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('lists feedback filtered by status', () => {
    service.listFeedback('p1', 'open').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/feedback?product_id=p1&status=open`);
    req.flush([]);
  });

  it('omits the status param when null', () => {
    service.listFeedback('p1', null).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/feedback?product_id=p1`);
    req.flush([]);
  });

  it('links a feedback item to a story via PATCH /feedback/{id}/link', () => {
    const stub: FeedbackItem = {
      id: 'f1',
      product_id: 'p1',
      source: 'qa',
      raw_payload: {},
      severity: 'normal',
      status: 'open',
      linked_story_id: 's1',
      sprint_id: null,
      author: 'a',
      created_at: '2026-05-01T00:00:00Z',
      updated_at: '2026-05-01T00:00:00Z',
    };
    service.linkFeedback('f1', 's1').subscribe((res) => {
      expect(res).toEqual(stub);
    });
    const req = httpMock.expectOne(`${baseUrl}/feedback/f1/link`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ linked_story_id: 's1' });
    req.flush(stub);
  });

  it('unlinks a feedback item by sending linked_story_id=null', () => {
    service.linkFeedback('f1', null).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/feedback/f1/link`);
    expect(req.request.body).toEqual({ linked_story_id: null });
    req.flush({});
  });

  // Static helpers ------------------------------------------------------------

  it('flattens a backlog tree to its stories', () => {
    const tree: BacklogTree = {
      product: {
        id: 'p1',
        name: 'P',
        description: '',
        vision: '',
        author: 'a',
        created_at: '',
        updated_at: '',
      },
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
                  status: 'proposed',
                  user_story: '',
                  estimate_points: null,
                  wsjf_score: null,
                  rice_score: null,
                  author: 'a',
                  created_at: '',
                  updated_at: '',
                  tasks: [],
                  acceptance_criteria: [],
                },
                {
                  id: 's2',
                  epic_id: 'e1',
                  title: 'S2',
                  summary: '',
                  status: 'proposed',
                  user_story: '',
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
    expect(ProductDeliveryService.flattenStories(tree).map((s) => s.id)).toEqual(['s1', 's2']);
  });

  it('returns an empty list for a null tree', () => {
    expect(ProductDeliveryService.flattenStories(null)).toEqual([]);
  });
});
