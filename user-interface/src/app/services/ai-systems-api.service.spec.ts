import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AISystemsApiService } from './ai-systems-api.service';
import { environment } from '../../environments/environment';

describe('AISystemsApiService', () => {
  let service: AISystemsApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.aiSystemsApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AISystemsApiService],
    });
    service = TestBed.inject(AISystemsApiService);
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

  it('should call POST /build for startBuild', () => {
    const body = { project_name: 'proj1', config: {} };
    service.startBuild(body).subscribe((res) => expect(res.job_id).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/build`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ job_id: 'j1', status: 'running' });
  });

  it('should call GET /build/status/{jobId} for getJobStatus', () => {
    const jobId = 'j1';
    service.getJobStatus(jobId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/build/status/${jobId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ job_id: jobId, status: 'completed' });
  });

  it('should call GET /build/jobs for listJobs', () => {
    service.listJobs().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/build/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush({ jobs: [] });
  });

  it('should call GET /blueprints for listBlueprints', () => {
    service.listBlueprints().subscribe((res) => expect(res.blueprints).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/blueprints`);
    expect(req.request.method).toBe('GET');
    req.flush({ blueprints: [] });
  });

  it('should call GET /blueprints/{projectName} for getBlueprint', () => {
    const projectName = 'proj1';
    service.getBlueprint(projectName).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/blueprints/${encodeURIComponent(projectName)}`);
    expect(req.request.method).toBe('GET');
    req.flush({ project_name: projectName });
  });
});
