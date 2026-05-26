import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { Soc2ComplianceApiService } from './soc2-compliance-api.service';
import { environment } from '../../environments/environment';

describe('Soc2ComplianceApiService', () => {
  let service: Soc2ComplianceApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.soc2ComplianceApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [Soc2ComplianceApiService],
    });
    service = TestBed.inject(Soc2ComplianceApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call POST /soc2-audit/run', () => {
    service.runAudit({ repo_path: '/tmp/repo' }).subscribe((res) => {
      expect(res.job_id).toBeDefined();
    });
    const req = httpMock.expectOne(`${baseUrl}/soc2-audit/run`);
    expect(req.request.method).toBe('POST');
    req.flush({ job_id: '123', status: 'running', message: 'Started' });
  });

  it('should call GET /soc2-audit/status/{id}', () => {
    service.getStatus('123').subscribe((res) => {
      expect(res.job_id).toBe('123');
    });
    const req = httpMock.expectOne(`${baseUrl}/soc2-audit/status/123`);
    expect(req.request.method).toBe('GET');
    req.flush({ job_id: '123', status: 'completed' });
  });

  it('health', () => {
    service.health().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({});
  });
});
