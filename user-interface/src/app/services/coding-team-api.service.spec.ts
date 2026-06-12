import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { CodingTeamApiService } from './coding-team-api.service';
import { environment } from '../../environments/environment';

describe('CodingTeamApiService', () => {
  let service: CodingTeamApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.codingTeamApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [CodingTeamApiService],
    });
    service = TestBed.inject(CodingTeamApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('GETs /health', () => {
    service.health().subscribe((r) => expect(r).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });

  it('GETs /status/{jobId}', () => {
    service.getJobStatus('job-1').subscribe((r) => {
      expect(r.job_id).toBe('job-1');
      expect(r.waiting_for_answers).toBe(true);
      expect(r.pending_questions?.[0]?.id).toBe('q1');
    });
    const req = httpMock.expectOne(`${baseUrl}/status/job-1`);
    expect(req.request.method).toBe('GET');
    req.flush({
      job_id: 'job-1',
      status: 'waiting_for_user',
      waiting_for_answers: true,
      pending_questions: [
        { id: 'q1', question_text: 'Pick one', options: [], required: true, source: 'tech_lead' },
      ],
    });
  });

  it('POSTs answers to /run/{jobId}/answers', () => {
    const body = {
      answers: [{ question_id: 'q1', selected_option_id: 'opt-a', other_text: null }],
    };
    service.submitAnswers('job-1', body).subscribe((r) => {
      expect(r.status).toBe('running');
      expect(r.waiting_for_answers).toBe(false);
    });
    const req = httpMock.expectOne(`${baseUrl}/run/job-1/answers`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ job_id: 'job-1', status: 'running', waiting_for_answers: false });
  });

  it('GETs /jobs?active=true by default (terminal jobs filtered server-side)', () => {
    service.listJobs().subscribe((jobs) => {
      expect(jobs.length).toBe(1);
      expect(jobs[0].github_context?.issue_number).toBe(42);
      expect(jobs[0].waiting_for_answers).toBe(true);
    });
    const req = httpMock.expectOne(`${baseUrl}/jobs?active=true`);
    expect(req.request.method).toBe('GET');
    req.flush([
      {
        job_id: 'job-1',
        status: 'waiting_for_user',
        waiting_for_answers: true,
        github_context: { owner: 'acme', repo: 'widgets', issue_number: 42 },
      },
    ]);
  });

  it('GETs /jobs unfiltered when activeOnly is false', () => {
    service.listJobs(false).subscribe((jobs) => {
      expect(jobs.length).toBe(1);
      expect(jobs[0].job_id).toBe('job-9');
      expect(jobs[0].status).toBe('completed');
    });
    const req = httpMock.expectOne(`${baseUrl}/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush([{ job_id: 'job-9', status: 'completed' }]);
  });
});
