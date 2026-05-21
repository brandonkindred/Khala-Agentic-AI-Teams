import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { SalesApiService } from './sales-api.service';
import { environment } from '../../environments/environment';

describe('SalesApiService', () => {
  let service: SalesApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.salesApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [SalesApiService],
    });
    service = TestBed.inject(SalesApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('health', () => {
    service.health().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/health`);
    req.flush({});
  });

  it('runPipeline', () => {
    service.runPipeline({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/pipeline/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getPipelineStatus', () => {
    service.getPipelineStatus('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/pipeline/status/j1`);
    req.flush({});
  });

  it('listPipelineJobs default', () => {
    service.listPipelineJobs().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/pipeline/jobs`);
    expect(req.request.params.get('running_only')).toBe('false');
    req.flush([]);
  });

  it('listPipelineJobs runningOnly=true', () => {
    service.listPipelineJobs(true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/pipeline/jobs`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush([]);
  });

  it('cancelJob', () => {
    service.cancelJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/pipeline/job/j1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('deleteJob', () => {
    service.deleteJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/pipeline/job/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('recordStageOutcome', () => {
    service.recordStageOutcome({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/outcomes/stage`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('recordDealOutcome', () => {
    service.recordDealOutcome({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/outcomes/deal`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getOutcomeSummary', () => {
    service.getOutcomeSummary().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/outcomes/summary`);
    req.flush({});
  });

  it('listStageOutcomes default 100', () => {
    service.listStageOutcomes().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/outcomes/stage`);
    expect(req.request.params.get('limit')).toBe('100');
    req.flush([]);
  });

  it('listStageOutcomes custom limit', () => {
    service.listStageOutcomes(50).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/outcomes/stage`);
    expect(req.request.params.get('limit')).toBe('50');
    req.flush([]);
  });

  it('listDealOutcomes default', () => {
    service.listDealOutcomes().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/outcomes/deal`);
    expect(req.request.params.get('limit')).toBe('100');
    req.flush([]);
  });

  it('listDealOutcomes custom limit', () => {
    service.listDealOutcomes(25).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/sales/outcomes/deal`);
    expect(req.request.params.get('limit')).toBe('25');
    req.flush([]);
  });

  it('getInsights', () => {
    service.getInsights().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/insights`);
    req.flush({});
  });

  it('refreshInsights', () => {
    service.refreshInsights().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sales/insights/refresh`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });
});
