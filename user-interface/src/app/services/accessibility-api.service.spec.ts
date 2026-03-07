import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AccessibilityApiService } from './accessibility-api.service';
import { environment } from '../../environments/environment';

describe('AccessibilityApiService', () => {
  let service: AccessibilityApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.accessibilityApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AccessibilityApiService],
    });
    service = TestBed.inject(AccessibilityApiService);
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

  it('should call POST /audit/create for createAudit', () => {
    const body = { url: 'https://example.com', name: 'Audit 1' };
    service.createAudit(body).subscribe((res) => expect(res.job_id).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/audit/create`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ job_id: 'j1', status: 'running' });
  });

  it('should call GET /audit/status/{job_id} for getJobStatus', () => {
    const jobId = 'j1';
    service.getJobStatus(jobId).subscribe((res) => expect(res.job_id).toBe(jobId));
    const req = httpMock.expectOne(`${baseUrl}/audit/status/${jobId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ job_id: jobId, status: 'completed' });
  });

  it('should call GET /audit/{audit_id}/report for getReport', () => {
    const auditId = 'a1';
    service.getReport(auditId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/audit/${auditId}/report`);
    expect(req.request.method).toBe('GET');
    req.flush({ summary: {}, findings: [] });
  });

  it('should call GET /audit/{audit_id}/findings with optional filters for getFindings', () => {
    const auditId = 'a1';
    service.getFindings(auditId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/audit/${auditId}/findings`);
    expect(req.request.method).toBe('GET');
    req.flush({ findings: [] });
  });
});
