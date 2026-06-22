import { TestBed } from '@angular/core/testing';
import { Injectable } from '@angular/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { BaseJobsApiService } from './base-jobs-api.service';

interface DemoJob {
  job_id: string;
  status: string;
}

@Injectable()
class DemoJobsApiService extends BaseJobsApiService<DemoJob> {
  protected readonly baseUrl = 'http://api.test/api/demo';
}

describe('BaseJobsApiService', () => {
  let svc: DemoJobsApiService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), DemoJobsApiService],
    });
    svc = TestBed.inject(DemoJobsApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('getJob hits the job URL', () => {
    svc.getJob('j1').subscribe();
    const req = http.expectOne('http://api.test/api/demo/jobs/j1');
    expect(req.request.method).toBe('GET');
    req.flush({ job_id: 'j1', status: 'running' });
  });

  it('cancelJob POSTs to /cancel and deleteJob DELETEs', () => {
    svc.cancelJob('j1').subscribe();
    const cancel = http.expectOne('http://api.test/api/demo/jobs/j1/cancel');
    expect(cancel.request.method).toBe('POST');
    cancel.flush({});

    svc.deleteJob('j1').subscribe();
    const del = http.expectOne('http://api.test/api/demo/jobs/j1');
    expect(del.request.method).toBe('DELETE');
    del.flush({});
  });

  it('encodes job ids in the URL', () => {
    svc.getJob('a/b c').subscribe();
    http.expectOne('http://api.test/api/demo/jobs/a%2Fb%20c').flush({ job_id: 'a/b c', status: 'x' });
  });

  it('pollJob polls getJob until the status is terminal', () => {
    vi.useFakeTimers();
    try {
      const seen: string[] = [];
      let done = false;
      svc
        .pollJob('j1', (j) => j.status === 'completed', { intervalMs: 100 })
        .subscribe({ next: (j) => seen.push(j.status), complete: () => (done = true) });

      vi.advanceTimersByTime(0);
      http.expectOne('http://api.test/api/demo/jobs/j1').flush({ job_id: 'j1', status: 'running' });
      expect(seen).toEqual(['running']);

      vi.advanceTimersByTime(100);
      http.expectOne('http://api.test/api/demo/jobs/j1').flush({ job_id: 'j1', status: 'completed' });
      expect(seen).toEqual(['running', 'completed']);
      expect(done).toBe(true);

      vi.advanceTimersByTime(200); // no further polls after completion
    } finally {
      vi.useRealTimers();
    }
  });
});
