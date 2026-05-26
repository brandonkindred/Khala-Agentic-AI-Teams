import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { vi } from 'vitest';
import { InvestmentApiService } from './investment-api.service';
import { environment } from '../../environments/environment';

describe('InvestmentApiService', () => {
  let service: InvestmentApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.investmentApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [InvestmentApiService],
    });
    service = TestBed.inject(InvestmentApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /health for healthCheck', () => {
    service.healthCheck().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });

  it('should call POST /profiles for createProfile', () => {
    const body = { user_id: 'u1', name: 'Profile 1' };
    service.createProfile(body).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/profiles`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ profile_id: 'p1' });
  });

  it('should call GET /profiles/{userId} for getProfile', () => {
    const userId = 'u1';
    service.getProfile(userId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/profiles/${userId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ profile: {} });
  });

  it('should call POST /proposals/create for createProposal', () => {
    const body = { profile_id: 'p1', title: 'Proposal 1' };
    service.createProposal(body).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/proposals/create`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ proposal_id: 'prop1' });
  });

  it('should call GET /proposals/{proposalId} for getProposal', () => {
    const proposalId = 'prop1';
    service.getProposal(proposalId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/proposals/${proposalId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ proposal: {} });
  });

  it('should call GET /workflow/status for getWorkflowStatus', () => {
    service.getWorkflowStatus().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/workflow/status`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'idle' });
  });

  it('validateProposal', () => {
    service.validateProposal('p1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/proposals/p1/validate`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('createStrategy', () => {
    service.createStrategy({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategies`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('validateStrategy without body', () => {
    service.validateStrategy('s1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategies/s1/validate`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('validateStrategy with body', () => {
    service.validateStrategy('s1', { force: true } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategies/s1/validate`);
    expect(req.request.body).toEqual({ force: true });
    req.flush({});
  });

  it('promotionDecision', () => {
    service.promotionDecision({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/promotions/decide`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getQueues', () => {
    service.getQueues().subscribe();
    httpMock.expectOne(`${baseUrl}/workflow/queues`).flush({});
  });

  it('createMemo', () => {
    service.createMemo({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/memos`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getStrategyLabConfig', () => {
    service.getStrategyLabConfig().subscribe();
    httpMock.expectOne(`${baseUrl}/strategy-lab/config`).flush({});
  });

  it('runStrategyLab default', () => {
    service.runStrategyLab().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/run`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('runStrategyLab with body', () => {
    service.runStrategyLab({ spec: 'a' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/run`);
    expect(req.request.body).toEqual({ spec: 'a' });
    req.flush({});
  });

  it('getActiveRuns', () => {
    service.getActiveRuns().subscribe();
    httpMock.expectOne(`${baseUrl}/strategy-lab/runs`).flush({});
  });

  it('getRunStatus encodes runId', () => {
    service.getRunStatus('run/1').subscribe();
    httpMock.expectOne(`${baseUrl}/strategy-lab/runs/${encodeURIComponent('run/1')}/status`).flush({});
  });

  it('resumeRun', () => {
    service.resumeRun('r1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/runs/r1/resume`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('restartRun', () => {
    service.restartRun('r1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/runs/r1/restart`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('deleteJob', () => {
    service.deleteJob('r1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/runs/r1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('listStrategyLabJobs default', () => {
    service.listStrategyLabJobs().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/jobs`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('listStrategyLabJobs runningOnly=true', () => {
    service.listStrategyLabJobs(true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/jobs`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush({});
  });

  it('getStrategyLabResults no winning', () => {
    service.getStrategyLabResults().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/results`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('getStrategyLabResults winning=true', () => {
    service.getStrategyLabResults(true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/results`);
    expect(req.request.params.get('winning')).toBe('true');
    req.flush({});
  });

  it('deleteStrategyLabRecord', () => {
    service.deleteStrategyLabRecord('rec1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/records/rec1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('clearStrategyLabStorage', () => {
    service.clearStrategyLabStorage().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/storage`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('runPaperTrading', () => {
    service.runPaperTrading({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/strategy-lab/paper-trade`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getPaperTradingResults default', () => {
    service.getPaperTradingResults().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/paper-trade/results`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('getPaperTradingResults with verdict', () => {
    service.getPaperTradingResults('passed').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/strategy-lab/paper-trade/results`);
    expect(req.request.params.get('verdict')).toBe('passed');
    req.flush({});
  });

  it('getPaperTradingSession encodes id', () => {
    service.getPaperTradingSession('s/1').subscribe();
    httpMock.expectOne(`${baseUrl}/strategy-lab/paper-trade/${encodeURIComponent('s/1')}`).flush({});
  });

  it('startAdvisorSession', () => {
    service.startAdvisorSession({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/advisor/sessions`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('sendAdvisorMessage', () => {
    service.sendAdvisorMessage('s1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/advisor/sessions/s1/messages`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getAdvisorSession', () => {
    service.getAdvisorSession('s1').subscribe();
    httpMock.expectOne(`${baseUrl}/advisor/sessions/s1`).flush({});
  });

  it('completeAdvisorSession', () => {
    service.completeAdvisorSession('s1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/advisor/sessions/s1/complete`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('streamRunStatus SSE parses + done completes', () => {
    interface FakeES {
      onmessage: ((e: { data: string }) => void) | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { onmessage: null, onerror: null, close: vi.fn() };
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      vi.fn(() => fakeES) as unknown as typeof EventSource;

    const received: unknown[] = [];
    let completed = false;
    service.streamRunStatus('run/1').subscribe({
      next: (e) => received.push(e),
      complete: () => (completed = true),
    });
    fakeES.onmessage?.({ data: JSON.stringify({ type: 'status' }) });
    fakeES.onmessage?.({ data: 'invalid' });
    fakeES.onmessage?.({ data: JSON.stringify({ type: 'done' }) });
    expect(received.length).toBe(2);
    expect(completed).toBe(true);
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  it('streamRunStatus SSE errors on connection lost', () => {
    interface FakeES {
      onmessage: ((e: { data: string }) => void) | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { onmessage: null, onerror: null, close: vi.fn() };
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      vi.fn(() => fakeES) as unknown as typeof EventSource;
    let err: unknown;
    service.streamRunStatus('r1').subscribe({ error: (e) => (err = e) });
    fakeES.onerror?.({});
    expect(err).toBeDefined();
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });
});
