import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { SocialMarketingApiService } from './social-marketing-api.service';
import { environment } from '../../environments/environment';

describe('SocialMarketingApiService', () => {
  let service: SocialMarketingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.socialMarketingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [SocialMarketingApiService],
    });
    service = TestBed.inject(SocialMarketingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call POST /social-marketing/run', () => {
    const req = {
      brand_guidelines_path: '/a',
      brand_objectives_path: '/b',
      llm_model_name: 'model',
    };
    service.run(req).subscribe((res) => expect(res.job_id).toBeDefined());
    const httpReq = httpMock.expectOne(`${baseUrl}/social-marketing/run`);
    expect(httpReq.request.method).toBe('POST');
    httpReq.flush({ job_id: '1', status: 'running', message: 'OK' });
  });

  it('should call GET /social-marketing/status/{id}', () => {
    service.getStatus('1').subscribe((res) => expect(res.job_id).toBe('1'));
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/status/1`);
    expect(req.request.method).toBe('GET');
    req.flush({
      job_id: '1',
      status: 'completed',
      current_stage: 'done',
      progress: 100,
      llm_model_name: 'm',
      brand_guidelines_path: '/a',
      brand_objectives_path: '/b',
      last_updated_at: new Date().toISOString(),
    });
  });

  it('listJobs default', () => {
    service.listJobs().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/social-marketing/jobs`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('listJobs runningOnly', () => {
    service.listJobs(true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/social-marketing/jobs`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush([]);
  });

  it('cancelJob', () => {
    service.cancelJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/job/j1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('deleteJob', () => {
    service.deleteJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/job/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('resumeJob', () => {
    service.resumeJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/job/j1/resume`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('restartJob', () => {
    service.restartJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/job/j1/restart`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('ingestPerformance', () => {
    service.ingestPerformance('j1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/performance/j1`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('revise', () => {
    service.revise('j1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/social-marketing/revise/j1`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('health', () => {
    service.health().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({});
  });
});
