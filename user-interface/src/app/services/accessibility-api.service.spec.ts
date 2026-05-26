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

  it('GET /health', () => {
    service.healthCheck().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({ status: 'ok' });
  });

  it('POST /audit/create', () => {
    service.createAudit({ url: 'https://x' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/audit/create`);
    expect(req.request.method).toBe('POST');
    req.flush({ job_id: 'j1' });
  });

  it('GET /audit/status/{id}', () => {
    service.getJobStatus('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/audit/status/j1`).flush({ job_id: 'j1' });
  });

  it('GET /audit/{id}/report', () => {
    service.getReport('a1').subscribe();
    httpMock.expectOne(`${baseUrl}/audit/a1/report`).flush({});
  });

  it('GET /audit/{id}/findings no filters', () => {
    service.getFindings('a1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/audit/a1/findings`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({ findings: [] });
  });

  it('GET /audit/{id}/findings appends all filter arrays', () => {
    service
      .getFindings('a1', {
        severity: ['high', 'critical'],
        issue_type: ['contrast', 'aria'],
        wcag_level: ['AA'],
        state: ['open', 'closed'],
      })
      .subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/audit/a1/findings`);
    expect(req.request.params.getAll('severity')).toEqual(['high', 'critical']);
    expect(req.request.params.getAll('issue_type')).toEqual(['contrast', 'aria']);
    expect(req.request.params.getAll('wcag_level')).toEqual(['AA']);
    expect(req.request.params.getAll('state')).toEqual(['open', 'closed']);
    req.flush({});
  });

  it('GET /audit/{id}/findings skips empty filter arrays', () => {
    service
      .getFindings('a1', { severity: [], issue_type: [], wcag_level: [], state: [] })
      .subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/audit/a1/findings`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({});
  });

  it('POST /audit/{id}/retest', () => {
    service.retestFindings('a1', { finding_ids: ['x'] } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/audit/a1/retest`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /audit/{id}/export json', () => {
    service.exportBacklog('a1', 'json').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/audit/a1/export` && r.responseType === 'json');
    expect(req.request.body.format).toBe('json');
    req.flush({});
  });

  it('POST /audit/{id}/export blob csv', () => {
    service.downloadExport('a1', 'csv').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/audit/a1/export` && r.responseType === 'blob');
    expect(req.request.body.format).toBe('csv');
    req.flush(new Blob([]));
  });

  it('POST /designsystem/inventory', () => {
    service.buildDesignSystemInventory({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/designsystem/inventory`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('POST /designsystem/contract', () => {
    service.generateDesignSystemContract({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/designsystem/contract`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });
});
