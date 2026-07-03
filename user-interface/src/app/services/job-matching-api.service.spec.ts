import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { vi } from 'vitest';
import { JobMatchingApiService } from './job-matching-api.service';
import { environment } from '../../environments/environment';
import type { JobMatchResponse, JobSeekerProfile } from '../models';

describe('JobMatchingApiService', () => {
  let service: JobMatchingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.jobMatchingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [JobMatchingApiService],
    });
    service = TestBed.inject(JobMatchingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /health', () => {
    service.health().subscribe((res) => expect(res.status).toBe('ok'));
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });

  it('getProfile issues GET /profile', () => {
    let received: JobSeekerProfile | undefined;
    service.getProfile().subscribe((p) => (received = p));
    const req = httpMock.expectOne(`${baseUrl}/profile`);
    expect(req.request.method).toBe('GET');
    req.flush({ target_titles: ['Staff Eng'] });
    expect(received!.target_titles).toEqual(['Staff Eng']);
  });

  it('saveProfile issues PUT /profile with the payload', () => {
    const profile = { target_titles: ['SRE'], salary_min: 180000 } as JobSeekerProfile;
    service.saveProfile(profile).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/profile`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.salary_min).toBe(180000);
    req.flush(profile);
  });

  it('startScan issues POST /scan', () => {
    service.startScan({ top_n: 5 }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/scan`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.top_n).toBe(5);
    req.flush({ job_id: 'j1', status: 'pending' });
  });

  it('getScanStatus issues GET /scan/status/{id}', () => {
    service.getScanStatus('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/scan/status/j1`).flush({ job_id: 'j1', status: 'running' });
  });

  describe('pollScan', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    /** pollWhile schedules polls on a timer; fire the pending tick. */
    const tickPoll = () => vi.advanceTimersByTime(0);

    it('polls until completed and emits the result', () => {
      const result = { run_id: 'r1', ranked_jobs: [], total_found: 3, total_ranked: 2 };
      let received: JobMatchResponse | null = null;
      service.pollScan('j1').subscribe((res) => (received = res));

      tickPoll();
      httpMock.expectOne(`${baseUrl}/scan/status/j1`).flush({ job_id: 'j1', status: 'running' });
      vi.advanceTimersByTime(2000);
      httpMock
        .expectOne(`${baseUrl}/scan/status/j1`)
        .flush({ job_id: 'j1', status: 'completed', result });

      expect(received!.run_id).toBe('r1');
      expect(received!.total_ranked).toBe(2);
    });

    it('survives a transient poll failure and keeps polling', () => {
      const result = { run_id: 'r1', ranked_jobs: [], total_found: 1, total_ranked: 1 };
      let received: JobMatchResponse | null = null;
      let errored = false;
      service.pollScan('j1').subscribe({
        next: (res) => (received = res),
        error: () => (errored = true),
      });

      // First poll dies with a network error — the scan must NOT be reported
      // failed; the next interval retries.
      tickPoll();
      httpMock.expectOne(`${baseUrl}/scan/status/j1`).error(new ProgressEvent('error'));
      expect(errored).toBe(false);

      vi.advanceTimersByTime(2000);
      httpMock
        .expectOne(`${baseUrl}/scan/status/j1`)
        .flush({ job_id: 'j1', status: 'completed', result });

      expect(errored).toBe(false);
      expect(received!.run_id).toBe('r1');
    });

    it('gives up after repeated consecutive poll failures (permanent error)', () => {
      // A deleted job / dead API must eventually error the stream instead of
      // spinning the progress UI forever (MAX_POLL_ERRORS = 5).
      let errored = false;
      service.pollScan('gone').subscribe({ error: () => (errored = true) });

      tickPoll();
      httpMock.expectOne(`${baseUrl}/scan/status/gone`).error(new ProgressEvent('error'));
      for (let i = 0; i < 3; i++) {
        vi.advanceTimersByTime(2000);
        httpMock.expectOne(`${baseUrl}/scan/status/gone`).error(new ProgressEvent('error'));
        expect(errored).toBe(false);
      }
      vi.advanceTimersByTime(2000);
      httpMock.expectOne(`${baseUrl}/scan/status/gone`).error(new ProgressEvent('error'));
      expect(errored).toBe(true);

      // Terminated: no further polls are scheduled.
      vi.advanceTimersByTime(10000);
      httpMock.expectNone(`${baseUrl}/scan/status/gone`);
    });

    it('errors on failed status', () => {
      let err: Error | undefined;
      service.pollScan('j1').subscribe({ error: (e) => (err = e as Error) });
      tickPoll();
      httpMock
        .expectOne(`${baseUrl}/scan/status/j1`)
        .flush({ job_id: 'j1', status: 'failed', error: 'boom' });
      expect(err!.message).toContain('boom');
    });

    it('errors when cancelled without result', () => {
      let err: Error | undefined;
      service.pollScan('j1').subscribe({ error: (e) => (err = e as Error) });
      tickPoll();
      httpMock.expectOne(`${baseUrl}/scan/status/j1`).flush({ job_id: 'j1', status: 'cancelled' });
      expect(err).toBeDefined();
    });
  });

  it('listScanJobs passes running_only', () => {
    service.listScanJobs(true).subscribe((res) => expect(res.jobs.length).toBe(1));
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/scan/jobs`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush({ jobs: [{ job_id: 'j1', status: 'running' }] });
  });

  it('cancelScanJob posts to the cancel endpoint', () => {
    service.cancelScanJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/scan/jobs/j1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({ job_id: 'j1', success: true });
  });

  it('deleteScanJob issues DELETE', () => {
    service.deleteScanJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/scan/jobs/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ job_id: 'j1', deleted: true });
  });

  it('listRuns passes limit', () => {
    service.listRuns(5).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/runs`);
    expect(req.request.params.get('limit')).toBe('5');
    req.flush([]);
  });

  it('getRun issues GET /runs/{id}', () => {
    service.getRun('r1').subscribe((run) => expect(run.run_id).toBe('r1'));
    httpMock
      .expectOne(`${baseUrl}/runs/r1`)
      .flush({ run_id: 'r1', status: 'completed', total_found: 0, total_ranked: 0, ranked_jobs: [] });
  });

  it('listListings passes status and limit', () => {
    service.listListings('favorite', 10).subscribe((res) => expect(res.total).toBe(0));
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/listings`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('status')).toBe('favorite');
    expect(req.request.params.get('limit')).toBe('10');
    req.flush({ listings: [], total: 0, counts: {} });
  });

  it('listListings defaults to the active filter', () => {
    service.listListings().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/listings`);
    expect(req.request.params.get('status')).toBe('active');
    req.flush({ listings: [], total: 0, counts: {} });
  });

  it('updateListing issues PATCH with the status', () => {
    service
      .updateListing('fp1', { status: 'archived' })
      .subscribe((listing) => expect(listing.status).toBe('archived'));
    const req = httpMock.expectOne(`${baseUrl}/listings/fp1`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body.status).toBe('archived');
    req.flush({
      fingerprint: 'fp1',
      posting: { title: 'Eng', company: 'Acme' },
      score: 0.9,
      sub_scores: {},
      recommendation: 'apply',
      rationale: '',
      concerns: [],
      run_id: 'r1',
      times_seen: 1,
      status: 'archived',
    });
  });
});
