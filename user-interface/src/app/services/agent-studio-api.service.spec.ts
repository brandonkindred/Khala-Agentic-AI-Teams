import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AgentStudioApiService } from './agent-studio-api.service';
import { environment } from '../../environments/environment';

describe('AgentStudioApiService', () => {
  let service: AgentStudioApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.agentStudioApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AgentStudioApiService],
    });
    service = TestBed.inject(AgentStudioApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('starts a conversation', () => {
    const body = { mode: 'new' as const };
    service.startConversation(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/conversations`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({
      conversation_id: 'c1',
      mode: 'new',
      messages: [],
      definition: {},
      readiness: [],
      suggested_questions: [],
    });
  });

  it('sends a message on an existing conversation', () => {
    const body = { message: 'hello' };
    service.sendMessage('c1', body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/conversations/c1/messages`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({
      conversation_id: 'c1',
      mode: 'new',
      messages: [],
      definition: {},
      readiness: [],
      suggested_questions: [],
    });
  });

  it('clones an agent from the registry with an encoded id', () => {
    service.cloneFromRegistry('blog/writer').subscribe();
    const req = httpMock.expectOne(
      `${baseUrl}/agents/from-registry/${encodeURIComponent('blog/writer')}`,
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toBeNull();
    req.flush({});
  });

  it('saves an agent', () => {
    const body = { name: 'My Agent', role: 'writer' };
    service.saveAgent(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/agents`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ agent_id: 'a1', manifest: {}, created: true });
  });

  it('creates a draft', () => {
    const body = { name: 'Draft 1', payload: { foo: 'bar' } };
    service.createDraft(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ draft_id: 'd1', name: 'Draft 1', updated_at: '2026-01-01T00:00:00Z' });
  });

  it('updates a draft, sending only the provided fields', () => {
    const body = { name: 'Renamed' };
    service.updateDraft('d1', body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts/d1`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush({ draft_id: 'd1', name: 'Renamed', updated_at: '2026-01-01T00:00:01Z' });
  });

  it('updates a draft with an explicit empty payload to clear it', () => {
    const body = { payload: {} };
    service.updateDraft('d1', body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts/d1`);
    expect(req.request.body).toEqual(body);
    req.flush({ draft_id: 'd1', name: 'Renamed', updated_at: '2026-01-01T00:00:02Z' });
  });

  it('lists drafts without pagination params', () => {
    service.listDrafts().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/drafts`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('lists drafts with limit/offset params', () => {
    service.listDrafts(10, 20).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/drafts`);
    expect(req.request.params.get('limit')).toBe('10');
    expect(req.request.params.get('offset')).toBe('20');
    req.flush([]);
  });

  it('gets a full draft with an encoded id', () => {
    service.getDraft('d1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts/d1`);
    expect(req.request.method).toBe('GET');
    req.flush({
      draft_id: 'd1',
      name: 'Draft 1',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      payload: {},
    });
  });

  it('renames a draft', () => {
    service.renameDraft('d1', 'New Name').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts/d1`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ name: 'New Name' });
    req.flush({ draft_id: 'd1', name: 'New Name', updated_at: '2026-01-01T00:00:03Z' });
  });

  it('deletes a draft', () => {
    service.deleteDraft('d1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/drafts/d1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ draft_id: 'd1', status: 'deleted' });
  });
});
