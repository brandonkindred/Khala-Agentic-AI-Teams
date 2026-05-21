import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { BrandingApiService, isTerminalJobStatus } from './branding-api.service';
import { environment } from '../../environments/environment';

describe('BrandingApiService', () => {
  let service: BrandingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.brandingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [BrandingApiService],
    });
    service = TestBed.inject(BrandingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should call POST /branding/sessions', () => {
    service
      .createSession({
        company_name: 'Acme',
        company_description: 'Brand strategy for SMB fintech',
        target_audience: 'Founders',
      })
      .subscribe((res) => expect(res.session_id).toBe('s1'));

    const req = httpMock.expectOne(`${baseUrl}/sessions`);
    expect(req.request.method).toBe('POST');
    req.flush({
      session_id: 's1',
      status: 'awaiting_user_answers',
      mission: {},
      latest_output: { status: 'needs_human_decision', mission_summary: '', brand_guidelines: [], writing_guidelines: { voice_principles: [] } },
      open_questions: [],
      answered_questions: [],
    });
  });

  it('should call POST question answer endpoint', () => {
    service.answerQuestion('s1', 'q1', 'clarity, trust').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sessions/s1/questions/q1/answer`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.answer).toBe('clarity, trust');
    req.flush({
      session_id: 's1',
      status: 'ready_for_rollout',
      mission: {},
      latest_output: { status: 'ready_for_rollout', mission_summary: '', brand_guidelines: [], writing_guidelines: { voice_principles: [] } },
      open_questions: [],
      answered_questions: [],
    });
  });

  it('listJobs(true) hits /branding/jobs?running_only=true', () => {
    const payload = { jobs: [{ job_id: 'j1', status: 'running', brand_id: 'b1' }] };
    service.listJobs(true).subscribe((jobs) => {
      expect(jobs).toHaveLength(1);
      expect(jobs[0].job_id).toBe('j1');
    });
    const req = httpMock.expectOne(`${baseUrl}/branding/jobs?running_only=true`);
    expect(req.request.method).toBe('GET');
    req.flush(payload);
  });

  it('observeJob emits intermediate running status before terminal', () => {
    const received: string[] = [];
    const sub = service.observeJob('j1').subscribe({
      next: (status) => received.push(status.status),
    });

    const first = httpMock.expectOne(`${baseUrl}/branding/status/j1`);
    first.flush({ job_id: 'j1', status: 'running', current_phase: 'Visual Identity' });

    // Intermediate status reached the subscriber; no second poll yet (timer).
    expect(received).toEqual(['running']);

    sub.unsubscribe();
  });

  it('isTerminalJobStatus helper', () => {
    expect(isTerminalJobStatus('completed')).toBe(true);
    expect(isTerminalJobStatus('failed')).toBe(true);
    expect(isTerminalJobStatus('cancelled')).toBe(true);
    expect(isTerminalJobStatus('running')).toBe(false);
  });

  it('listClients', () => {
    service.listClients().subscribe();
    httpMock.expectOne(`${baseUrl}/clients`).flush([]);
  });

  it('getClient', () => {
    service.getClient('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/clients/c1`).flush({});
  });

  it('createClient', () => {
    service.createClient({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('listBrands', () => {
    service.listBrands('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/clients/c1/brands`).flush([]);
  });

  it('getBrand', () => {
    service.getBrand('c1', 'b1').subscribe();
    httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1`).flush({});
  });

  it('createBrand', () => {
    service.createBrand('c1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('updateBrand', () => {
    service.updateBrand('c1', 'b1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('submitRun with default body', () => {
    service.submitRun('c1', 'b1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/run`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.human_approved).toBe(true);
    req.flush({ job_id: 'j1', status: 'running' });
  });

  it('submitRun with explicit body', () => {
    service.submitRun('c1', 'b1', { human_approved: false } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/run`);
    expect(req.request.body.human_approved).toBe(false);
    req.flush({});
  });

  it('runBrand completes when first poll returns completed result', () => {
    const out: unknown[] = [];
    service.runBrand('c1', 'b1').subscribe((r) => out.push(r));
    httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/run`).flush({ job_id: 'j1', status: 'running' });
    httpMock.expectOne(`${baseUrl}/branding/status/j1`).flush({
      job_id: 'j1',
      status: 'completed',
      result: { brand_guidelines: [] },
    });
    expect(out.length).toBe(1);
  });

  it('runBrand errors when failed', () => {
    let err: Error | undefined;
    service.runBrand('c1', 'b1').subscribe({ error: (e) => (err = e as Error) });
    httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/run`).flush({ job_id: 'j1', status: 'running' });
    httpMock.expectOne(`${baseUrl}/branding/status/j1`).flush({
      job_id: 'j1',
      status: 'failed',
      error: 'boom',
    });
    expect(err!.message).toContain('boom');
  });

  it('listJobs without filter', () => {
    service.listJobs().subscribe();
    httpMock.expectOne(`${baseUrl}/branding/jobs`).flush({ jobs: [] });
  });

  it('requestMarketResearch', () => {
    service.requestMarketResearch('c1', 'b1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/request-market-research`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('requestDesignAssets', () => {
    service.requestDesignAssets('c1', 'b1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/request-design-assets`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getSession', () => {
    service.getSession('s1').subscribe();
    httpMock.expectOne(`${baseUrl}/sessions/s1`).flush({});
  });

  it('getOpenQuestions', () => {
    service.getOpenQuestions('s1').subscribe();
    httpMock.expectOne(`${baseUrl}/sessions/s1/questions`).flush([]);
  });

  it('createConversation without initial message', () => {
    service.createConversation(null).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/conversations`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('createConversation with initial message', () => {
    service.createConversation('hi').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/conversations`);
    expect(req.request.body.initial_message).toBe('hi');
    req.flush({});
  });

  it('sendConversationMessage', () => {
    service.sendConversationMessage('c1', 'hi').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/conversations/c1/messages`);
    expect(req.request.body.message).toBe('hi');
    req.flush({});
  });

  it('getConversation', () => {
    service.getConversation('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/conversations/c1`).flush({});
  });

  it('getBrandConversation', () => {
    service.getBrandConversation('c1', 'b1').subscribe();
    httpMock.expectOne(`${baseUrl}/clients/c1/brands/b1/conversation`).flush({});
  });

  it('health', () => {
    service.health().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({});
  });
});
