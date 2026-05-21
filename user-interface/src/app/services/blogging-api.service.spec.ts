import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { vi } from 'vitest';
import { BloggingApiService } from './blogging-api.service';
import { environment } from '../../environments/environment';

describe('BloggingApiService', () => {
  let service: BloggingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.bloggingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [BloggingApiService],
    });
    service = TestBed.inject(BloggingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call POST /full-pipeline', () => {
    const request = { brief: 'Test brief', run_gates: true, max_rewrite_iterations: 3 };
    const mockResponse = {
      status: 'PASS',
      work_dir: '/tmp/foo',
      title_choices: [],
      outline: '',
    };

    service.fullPipeline(request).subscribe((res) => {
      expect(res.status).toBe('PASS');
      expect(res.work_dir).toBe('/tmp/foo');
    });

    const req = httpMock.expectOne(`${baseUrl}/full-pipeline`);
    expect(req.request.method).toBe('POST');
    req.flush(mockResponse);
  });

  it('should call GET /health', () => {
    service.health().subscribe((res) => {
      expect(res.status).toBe('ok');
      expect(res.brand_spec_configured).toBe(true);
    });

    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok', brand_spec_configured: true });
  });

  it('should call GET /job/{jobId}/artifacts', () => {
    const jobId = 'abc-123';
    const mockResponse = { artifacts: ['final.md', 'outline.md'] };

    service.getJobArtifacts(jobId).subscribe((res) => {
      expect(res.artifacts).toEqual(['final.md', 'outline.md']);
    });

    const req = httpMock.expectOne(`${baseUrl}/job/${jobId}/artifacts`);
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should call GET /job/{jobId}/artifacts/{artifactName} with encoded name', () => {
    const jobId = 'abc-123';
    const artifactName = 'final.md';
    const mockResponse = { name: 'final.md', content: '# Draft' };

    service.getJobArtifactContent(jobId, artifactName).subscribe((res) => {
      expect(res.name).toBe('final.md');
      expect(res.content).toBe('# Draft');
    });

    const req = httpMock.expectOne(
      `${baseUrl}/job/${jobId}/artifacts/${encodeURIComponent(artifactName)}`
    );
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('POST /full-pipeline-async', () => {
    service.startFullPipelineAsync({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/full-pipeline-async`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /medium-stats-async', () => {
    service.startMediumStatsAsync({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium-stats-async`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /medium-stats sync', () => {
    service.mediumStatsSync({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium-stats`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getJobs without running_only', () => {
    service.getJobs().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/jobs`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('getJobs with running_only=true', () => {
    service.getJobs(true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/jobs`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush([]);
  });

  it('getJobStatus GET', () => {
    service.getJobStatus('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/job/j1`).flush({});
  });

  it('cancelJob POST', () => {
    service.cancelJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('deleteJob DELETE', () => {
    service.deleteJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('resumeJob POST', () => {
    service.resumeJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/resume`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('restartJob POST', () => {
    service.restartJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/restart`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('approveJob POST', () => {
    service.approveJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/approve`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('unapproveJob POST', () => {
    service.unapproveJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/unapprove`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getJobArtifactDownloadUrl returns url string', () => {
    const url = service.getJobArtifactDownloadUrl('j1', 'a/b.md');
    expect(url).toContain(`${baseUrl}/job/j1/artifacts/${encodeURIComponent('a/b.md')}?download=true`);
  });

  it('selectTitle POST', () => {
    service.selectTitle('j1', 'My Title').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/select-title`);
    expect(req.request.body.title).toBe('My Title');
    req.flush({});
  });

  it('rateTitles POST', () => {
    service.rateTitles('j1', [{ title: 'a', rating: 'love' }]).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/rate-titles`);
    expect(req.request.body.ratings.length).toBe(1);
    req.flush({});
  });

  it('submitStoryResponse POST', () => {
    service.submitStoryResponse('j1', 'context').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/story-response`);
    expect(req.request.body.message).toBe('context');
    req.flush({});
  });

  it('skipStoryGap POST', () => {
    service.skipStoryGap('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/skip-story-gap`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('submitDraftFeedback POST', () => {
    service.submitDraftFeedback('j1', 'good', true).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/draft-feedback`);
    expect(req.request.body.feedback).toBe('good');
    expect(req.request.body.approved).toBe(true);
    req.flush({});
  });

  it('submitBlogAnswers POST', () => {
    service.submitBlogAnswers('j1', [{ q: 'a' }]).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/job/j1/answers`);
    expect(req.request.body.answers.length).toBe(1);
    req.flush({});
  });

  it('streamJobStatus SSE: parses, completes on done', () => {
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
    let completed = false;
    service.streamJobStatus('job/1').subscribe({
      next: (e) => received.push(e),
      complete: () => (completed = true),
    });
    expect(fakeES.url).toContain('/job/');
    // emit a status event
    fakeES.onmessage?.({ data: JSON.stringify({ type: 'status' }) });
    // emit done -> completes
    fakeES.onmessage?.({ data: JSON.stringify({ type: 'done' }) });
    expect(received.length).toBe(2);
    expect(completed).toBe(true);
    expect(fakeES.close).toHaveBeenCalled();

    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  it('streamJobStatus SSE: ignores unparseable frames', () => {
    interface FakeES {
      url: string;
      onmessage: ((e: { data: string }) => void) | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { url: '', onmessage: null, onerror: null, close: vi.fn() };
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      vi.fn(() => fakeES) as unknown as typeof EventSource;

    const received: unknown[] = [];
    service.streamJobStatus('j1').subscribe({ next: (e) => received.push(e) });
    fakeES.onmessage?.({ data: 'not-json' });
    expect(received).toEqual([]);

    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  it('streamJobStatus SSE: errors on connection lost', () => {
    interface FakeES {
      url: string;
      onmessage: ((e: { data: string }) => void) | null;
      onerror: ((e: unknown) => void) | null;
      close: () => void;
    }
    const fakeES: FakeES = { url: '', onmessage: null, onerror: null, close: vi.fn() };
    const originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      vi.fn(() => fakeES) as unknown as typeof EventSource;
    let err: Error | undefined;
    service.streamJobStatus('j1').subscribe({ error: (e) => (err = e as Error) });
    fakeES.onerror?.({});
    expect(err).toBeDefined();
    expect(fakeES.close).toHaveBeenCalled();

    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });
});
