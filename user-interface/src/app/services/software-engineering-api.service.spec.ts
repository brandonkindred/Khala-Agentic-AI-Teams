import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from './software-engineering-api.service';
import { environment } from '../../environments/environment';

describe('SoftwareEngineeringApiService', () => {
  let service: SoftwareEngineeringApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.softwareEngineeringApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [SoftwareEngineeringApiService],
    });
    service = TestBed.inject(SoftwareEngineeringApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('POST /run-team', () => {
    service.runTeam({ repo_path: '/tmp' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('GET /run-team/{id}', () => {
    service.getJobStatus('1').subscribe();
    httpMock.expectOne(`${baseUrl}/run-team/1`).flush({});
  });

  it('GET /run-team/jobs running_only=true (default)', () => {
    service.getRunningJobs().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/run-team/jobs`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('GET /run-team/jobs running_only=false', () => {
    service.getRunningJobs(false).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/run-team/jobs`);
    expect(req.request.params.get('running_only')).toBe('false');
    req.flush({});
  });

  it('POST /run-team/upload', () => {
    const file = new File(['x'], 'spec.md', { type: 'text/markdown' });
    service.runTeamFromUpload('proj', file).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/upload`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body instanceof FormData).toBe(true);
    req.flush({});
  });

  it('POST /run-team/{id}/retry-failed', () => {
    service.retryFailed('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/retry-failed`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /run-team/{id}/resume', () => {
    service.resumeRunTeamJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/resume`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /run-team/{id}/restart', () => {
    service.restartRunTeamJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/restart`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /run-team/{id}/cancel', () => {
    service.cancelJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('DELETE /run-team/{id}', () => {
    service.deleteJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('POST /run-team/{id}/answers', () => {
    service.submitAnswers('j1', { answers: [] } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/answers`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /run-team/{id}/auto-answer/{qid} default body', () => {
    service.autoAnswerRunTeam('j1', 'q1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('POST /run-team/{id}/auto-answer/{qid} with body', () => {
    service.autoAnswerRunTeam('j1', 'q1', { context: 'x' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run-team/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({ context: 'x' });
    req.flush({});
  });

  it('GET /execution/tasks', () => {
    service.getExecutionTasks().subscribe();
    httpMock.expectOne(`${baseUrl}/execution/tasks`).flush({});
  });

  it('POST /architect/design', () => {
    service.architectDesign({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/architect/design`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /backend-code-v2/run', () => {
    service.runBackendCodeV2({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/backend-code-v2/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('GET /backend-code-v2/status/{id}', () => {
    service.getBackendCodeV2Status('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/backend-code-v2/status/j1`).flush({});
  });

  it('POST /frontend-code-v2/run', () => {
    service.runFrontendCodeV2({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/frontend-code-v2/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('GET /frontend-code-v2/status/{id}', () => {
    service.getFrontendCodeV2Status('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/frontend-code-v2/status/j1`).flush({});
  });

  it('POST /planning-v2/run', () => {
    service.runPlanningV2({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/planning-v2/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('GET /planning-v2/status/{id}', () => {
    service.getPlanningV2Status('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/planning-v2/status/j1`).flush({});
  });

  it('GET /planning-v2/jobs', () => {
    service.getPlanningV2Jobs().subscribe();
    httpMock.expectOne(`${baseUrl}/planning-v2/jobs`).flush({});
  });

  it('GET /planning-v2/result/{id}', () => {
    service.getPlanningV2Result('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/planning-v2/result/j1`).flush({});
  });

  it('POST /planning-v2/{id}/answers', () => {
    service.submitPlanningV2Answers('j1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/planning-v2/j1/answers`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /planning-v2/{id}/auto-answer/{qid} default body', () => {
    service.autoAnswerPlanningV2('j1', 'q1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/planning-v2/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('POST /planning-v2/{id}/auto-answer/{qid} with body', () => {
    service.autoAnswerPlanningV2('j1', 'q1', { context: 'x' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/planning-v2/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({ context: 'x' });
    req.flush({});
  });

  it('GET /planning-v2/{id}/artifacts', () => {
    service.getPlanningV2Artifacts('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/planning-v2/j1/artifacts`).flush({});
  });

  it('GET /planning-v2/{id}/artifacts/{name} encodes name', () => {
    service.getPlanningV2ArtifactContent('j1', 'a/b.md').subscribe();
    httpMock.expectOne(`${baseUrl}/planning-v2/j1/artifacts/${encodeURIComponent('a/b.md')}`).flush({});
  });

  it('POST /product-analysis/run', () => {
    service.runProductAnalysis({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/product-analysis/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /product-analysis/start-from-spec', () => {
    service.startProductAnalysisFromSpec('proj', 'spec text').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/product-analysis/start-from-spec`);
    expect(req.request.body.project_name).toBe('proj');
    expect(req.request.body.spec_content).toBe('spec text');
    req.flush({});
  });

  it('GET /product-analysis/status/{id}', () => {
    service.getProductAnalysisStatus('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/product-analysis/status/j1`).flush({});
  });

  it('GET /product-analysis/jobs', () => {
    service.getProductAnalysisJobs().subscribe();
    httpMock.expectOne(`${baseUrl}/product-analysis/jobs`).flush({});
  });

  it('POST /product-analysis/{id}/answers', () => {
    service.submitProductAnalysisAnswers('j1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/product-analysis/j1/answers`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /product-analysis/{id}/auto-answer/{qid} default body', () => {
    service.autoAnswerProductAnalysis('j1', 'q1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/product-analysis/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({});
    req.flush({});
  });

  it('POST /product-analysis/{id}/auto-answer/{qid} with body', () => {
    service.autoAnswerProductAnalysis('j1', 'q1', { context: 'x' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/product-analysis/j1/auto-answer/q1`);
    expect(req.request.body).toEqual({ context: 'x' });
    req.flush({});
  });

  it('GET /health', () => {
    service.health().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({});
  });

  it('getExecutionStream SSE: onmessage parses JSON', () => {
    type ESHandler = (e: { data: string }) => void;
    interface FakeES {
      url: string;
      onmessage: ESHandler | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { url: '', onmessage: null, onerror: null, close: vi.fn() };
    const ESCtor = vi.fn((url: string) => {
      fakeES.url = url;
      return fakeES;
    });
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = ESCtor as unknown as typeof EventSource;

    const received: unknown[] = [];
    const sub = service.getExecutionStream().subscribe((d) => received.push(d));
    expect(fakeES.url).toContain('/execution/stream');
    fakeES.onmessage?.({ data: '{"task":"foo"}' });
    expect(received).toEqual([{ task: 'foo' }]);

    // Bad JSON: emits raw fallback
    fakeES.onmessage?.({ data: 'not-json' });
    expect(received[1]).toEqual({ raw: 'not-json' });

    sub.unsubscribe();
    expect(fakeES.close).toHaveBeenCalled();
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  it('getExecutionStream SSE: onerror closes', () => {
    type ESHandler = (e: { data: string }) => void;
    interface FakeES {
      url: string;
      onmessage: ESHandler | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { url: '', onmessage: null, onerror: null, close: vi.fn() };
    const ESCtor = vi.fn(() => fakeES);
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = ESCtor as unknown as typeof EventSource;

    let errored = false;
    service.getExecutionStream().subscribe({ error: () => (errored = true) });
    fakeES.onerror?.({ error: 'boom' });
    expect(errored).toBe(true);
    expect(fakeES.close).toHaveBeenCalled();

    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });
});
