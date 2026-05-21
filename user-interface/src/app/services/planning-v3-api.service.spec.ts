import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { PlanningV3ApiService } from './planning-v3-api.service';
import { environment } from '../../environments/environment';

describe('PlanningV3ApiService', () => {
  let service: PlanningV3ApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.planningV3ApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [PlanningV3ApiService],
    });
    service = TestBed.inject(PlanningV3ApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('POST /run', () => {
    service.run({ spec_text: 'hello' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('GET /status/{id}', () => {
    service.getStatus('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/status/j1`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('GET /result/{id}', () => {
    service.getResult('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/result/j1`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('GET /jobs', () => {
    service.getJobs().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('POST /{id}/answers', () => {
    service.submitAnswers('j1', [{ question_id: 'q1', selected_option_id: 'opt' }]).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/j1/answers`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.answers[0].question_id).toBe('q1');
    req.flush({});
  });

  it('GET /health', () => {
    service.health().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/health`);
    req.flush({ status: 'ok' });
  });
});
