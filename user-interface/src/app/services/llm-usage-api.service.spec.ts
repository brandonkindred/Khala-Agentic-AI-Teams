import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { LlmUsageApiService } from './llm-usage-api.service';
import { environment } from '../../environments/environment';
import type { LlmUsageSummary } from '../models/llm-usage.model';

function summary(over: Partial<LlmUsageSummary> = {}): LlmUsageSummary {
  return {
    team: 'all',
    window: '24h',
    window_hours: 24,
    total_calls: 0,
    total_prompt_tokens: 0,
    total_completion_tokens: 0,
    total_tokens: 0,
    total_cache_read_tokens: 0,
    total_cache_creation_tokens: 0,
    avg_latency_ms: 0,
    error_count: 0,
    by_agent: {},
    by_model: {},
    storage_available: true,
    storage_status: 'available',
    ...over,
  };
}

describe('LlmUsageApiService', () => {
  let service: LlmUsageApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.llmUsageApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [LlmUsageApiService],
    });
    service = TestBed.inject(LlmUsageApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('getSummary issues GET /api/llm-usage/?window=', () => {
    const mock = summary();
    service.getSummary('7d').subscribe((res) => expect(res).toEqual(mock));
    const req = httpMock.expectOne(
      (r) => r.method === 'GET' && r.url.startsWith(`${baseUrl}/`) && r.params.get('window') === '7d',
    );
    req.flush(mock);
  });

  it('getRecent issues GET /api/llm-usage/recent?window=&limit=', () => {
    service.getRecent('24h', 100).subscribe((res) => expect(res).toEqual([]));
    const req = httpMock.expectOne(
      (r) =>
        r.method === 'GET' &&
        r.url.startsWith(`${baseUrl}/recent`) &&
        r.params.get('window') === '24h' &&
        r.params.get('limit') === '100',
    );
    req.flush([]);
  });

  it('getSummary propagates HTTP errors', () => {
    let captured: HttpErrorResponse | undefined;
    service.getSummary('24h').subscribe({ error: (e) => (captured = e) });
    httpMock
      .expectOne((r) => r.url.startsWith(`${baseUrl}/`) && r.params.get('window') === '24h')
      .flush('error', { status: 500, statusText: 'Error' });
    expect(captured).toBeInstanceOf(HttpErrorResponse);
  });
});
