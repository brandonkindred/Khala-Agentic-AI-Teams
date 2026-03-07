import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AgentProvisioningApiService } from './agent-provisioning-api.service';
import { environment } from '../../environments/environment';

describe('AgentProvisioningApiService', () => {
  let service: AgentProvisioningApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.agentProvisioningApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AgentProvisioningApiService],
    });
    service = TestBed.inject(AgentProvisioningApiService);
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

  it('should call POST /provision for startProvisioning', () => {
    const body = { name: 'env1', config: {} };
    service.startProvisioning(body).subscribe((res) => expect(res.job_id).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/provision`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ job_id: 'j1', status: 'running' });
  });

  it('should call GET /provision/status/{jobId} for getJobStatus', () => {
    const jobId = 'j1';
    service.getJobStatus(jobId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/provision/status/${jobId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ job_id: jobId, status: 'completed' });
  });

  it('should call GET /provision/jobs for listJobs', () => {
    service.listJobs().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/provision/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush({ jobs: [] });
  });

  it('should call GET /environments for listAgents', () => {
    service.listAgents().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/environments`);
    expect(req.request.method).toBe('GET');
    req.flush({ agents: [] });
  });
});
