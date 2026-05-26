import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TeamAssistantApiService } from './team-assistant-api.service';

describe('TeamAssistantApiService', () => {
  let service: TeamAssistantApiService;
  let httpMock: HttpTestingController;
  const base = '/api/blogging/assistant';

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [TeamAssistantApiService],
    });
    service = TestBed.inject(TeamAssistantApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('getConversation without conversation id', () => {
    service.getConversation(base).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/conversation`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('getConversation with conversation id', () => {
    service.getConversation(base, 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/conversation`);
    expect(req.request.params.get('conversation_id')).toBe('c1');
    req.flush({});
  });

  it('sendMessage', () => {
    service.sendMessage(base, 'hi', 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/conversation/messages`);
    expect(req.request.method).toBe('POST');
    expect(req.request.params.get('conversation_id')).toBe('c1');
    expect(req.request.body.message).toBe('hi');
    req.flush({});
  });

  it('updateContext', () => {
    service.updateContext(base, { foo: 'bar' }, 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/conversation/context`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('getReadiness', () => {
    service.getReadiness(base, 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/readiness`);
    expect(req.request.params.get('conversation_id')).toBe('c1');
    req.flush({});
  });

  it('launch', () => {
    service.launch(base, 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/launch`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toBeNull();
    req.flush({});
  });

  it('resetConversation', () => {
    service.resetConversation(base, 'c1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/conversation`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('createConversation', () => {
    service.createConversation(base).subscribe();
    const req = httpMock.expectOne(`${base}/conversations`);
    expect(req.request.method).toBe('POST');
    req.flush({ conversation_id: 'c1' });
  });

  it('listConversations', () => {
    service.listConversations(base).subscribe();
    const req = httpMock.expectOne(`${base}/conversations`);
    expect(req.request.method).toBe('GET');
    req.flush({ conversations: [] });
  });

  it('listUnlinkedConversations', () => {
    service.listUnlinkedConversations(base).subscribe();
    const req = httpMock.expectOne(`${base}/conversations/unlinked`);
    req.flush({ conversations: [] });
  });

  it('getConversationByJob', () => {
    service.getConversationByJob(base, 'job/1').subscribe();
    const req = httpMock.expectOne(`${base}/conversations/by-job/${encodeURIComponent('job/1')}`);
    req.flush({});
  });

  it('linkConversationToJob', () => {
    service.linkConversationToJob(base, 'c1', 'j1').subscribe();
    const req = httpMock.expectOne(`${base}/conversations/c1/link-job`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.job_id).toBe('j1');
    req.flush({});
  });

  it('deleteConversation', () => {
    service.deleteConversation(base, 'c1').subscribe();
    const req = httpMock.expectOne(`${base}/conversations/c1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });
});
