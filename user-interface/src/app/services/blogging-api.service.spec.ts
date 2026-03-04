import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
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

  it('should call POST /research-and-review', () => {
    const request = { brief: 'Test brief', max_results: 20 };
    const mockResponse = {
      title_choices: [{ title: 'Test', probability_of_success: 0.9 }],
      outline: 'Outline',
    };

    service.researchAndReview(request).subscribe((res) => {
      expect(res.title_choices.length).toBe(1);
      expect(res.outline).toBe('Outline');
    });

    const req = httpMock.expectOne(`${baseUrl}/research-and-review`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush(mockResponse);
  });

  it('should call POST /full-pipeline', () => {
    const request = { brief: 'Test', run_gates: true, max_rewrite_iterations: 3 };
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
    });

    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
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
});
