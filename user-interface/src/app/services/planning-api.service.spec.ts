import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { PlanningApiService } from './planning-api.service';
import { environment } from '../../environments/environment';
import type { PlanningRunRequest, PlanningStatusResponse, PlanningResultResponse } from '../models';

describe('PlanningApiService', () => {
  let service: PlanningApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.planningApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [PlanningApiService],
    });
    service = TestBed.inject(PlanningApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('POST /run', () => {
    const request: PlanningRunRequest = { initial_brief: 'hello' };
    service.run(request).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/run`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush({ job_id: 'j1', status: 'running', message: 'started' });
  });

  it('POST /run surfaces server errors', () => {
    let caughtStatus: number | undefined;
    service.run({ initial_brief: 'hello' }).subscribe({
      error: (err) => (caughtStatus = err.status),
    });
    const req = httpMock.expectOne(`${baseUrl}/run`);
    req.flush('Bad request', { status: 400, statusText: 'Bad Request' });
    expect(caughtStatus).toBe(400);
  });

  it('GET /status/{id}', () => {
    const response: PlanningStatusResponse = {
      job_id: 'j1',
      status: 'running',
      progress: 42,
      pending_questions: [],
      waiting_for_answers: false,
    };
    let received: PlanningStatusResponse | undefined;
    service.getStatus('j1').subscribe((res) => (received = res));
    const req = httpMock.expectOne(`${baseUrl}/status/j1`);
    expect(req.request.method).toBe('GET');
    req.flush(response);
    expect(received).toEqual(response);
  });

  it('GET /status/{id} surfaces a 404', () => {
    let caughtStatus: number | undefined;
    service.getStatus('missing').subscribe({
      error: (err) => (caughtStatus = err.status),
    });
    const req = httpMock.expectOne(`${baseUrl}/status/missing`);
    req.flush('Not found', { status: 404, statusText: 'Not Found' });
    expect(caughtStatus).toBe(404);
  });

  it('GET /result/{id}', () => {
    const response: PlanningResultResponse = { job_id: 'j1', success: true, summary: 'done' };
    let received: PlanningResultResponse | undefined;
    service.getResult('j1').subscribe((res) => (received = res));
    const req = httpMock.expectOne(`${baseUrl}/result/j1`);
    expect(req.request.method).toBe('GET');
    req.flush(response);
    expect(received).toEqual(response);
  });

  it('GET /jobs', () => {
    const response = { jobs: [{ job_id: 'j1', status: 'running' }] };
    let received: unknown;
    service.getJobs().subscribe((res) => (received = res));
    const req = httpMock.expectOne(`${baseUrl}/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush(response);
    expect(received).toEqual(response);
  });

  it('POST /{id}/answers', () => {
    service.submitAnswers('j1', [{ question_id: 'q1', selected_option_id: 'opt' }]).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/j1/answers`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.answers[0].question_id).toBe('q1');
    req.flush({});
  });

  it('POST /{id}/answers surfaces server errors', () => {
    let caughtStatus: number | undefined;
    service.submitAnswers('j1', [{ question_id: 'q1' }]).subscribe({
      error: (err) => (caughtStatus = err.status),
    });
    const req = httpMock.expectOne(`${baseUrl}/j1/answers`);
    req.flush('Server error', { status: 500, statusText: 'Server Error' });
    expect(caughtStatus).toBe(500);
  });

  it('GET /health', () => {
    let received: unknown;
    service.health().subscribe((res) => (received = res));
    const req = httpMock.expectOne(`${baseUrl}/health`);
    req.flush({ status: 'ok' });
    expect(received).toEqual({ status: 'ok' });
  });
});
