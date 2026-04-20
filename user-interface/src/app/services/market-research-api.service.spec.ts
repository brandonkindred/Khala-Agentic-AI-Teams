import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { MarketResearchApiService } from './market-research-api.service';
import { environment } from '../../environments/environment';

describe('MarketResearchApiService', () => {
  let service: MarketResearchApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.marketResearchApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [MarketResearchApiService],
    });
    service = TestBed.inject(MarketResearchApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should submit a run job and poll until completed', () => {
    const request = {
      product_concept: 'Concept',
      target_users: 'Users',
      business_goal: 'Goal',
    };
    const teamOutput = {
      status: 'draft',
      topology: 'unified',
      mission_summary: 'Summary',
      insights: [],
      market_signals: [],
      recommendation: { verdict: 'Go', confidence: 0.8 },
      proposed_research_scripts: [],
    };
    const jobId = 'mr-job-1';

    let received: any = null;
    service.run(request).subscribe((res) => (received = res));

    const submitReq = httpMock.expectOne(`${baseUrl}/market-research/run`);
    expect(submitReq.request.method).toBe('POST');
    submitReq.flush({ job_id: jobId, status: 'running' });

    const statusReq = httpMock.expectOne(`${baseUrl}/market-research/status/${jobId}`);
    expect(statusReq.request.method).toBe('GET');
    statusReq.flush({ job_id: jobId, status: 'completed', result: teamOutput });

    expect(received.status).toBe('draft');
    expect(received.recommendation.verdict).toBe('Go');
  });

  it('should call GET /health', () => {
    service.health().subscribe((res) => expect(res.status).toBe('ok'));
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });
});
