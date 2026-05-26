import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { Subscription } from 'rxjs';
import { vi } from 'vitest';
import { RoadTripPlanningApiService } from './road-trip-planning-api.service';
import { environment } from '../../environments/environment';

describe('RoadTripPlanningApiService', () => {
  let service: RoadTripPlanningApiService;
  let httpMock: HttpTestingController;
  let subs: Subscription[] = [];
  const baseUrl = environment.roadTripPlanningApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [RoadTripPlanningApiService],
    });
    service = TestBed.inject(RoadTripPlanningApiService);
    httpMock = TestBed.inject(HttpTestingController);
    subs = [];
  });

  afterEach(() => {
    subs.forEach((s) => s.unsubscribe());
    vi.useRealTimers();
  });

  it('getHealth', () => {
    service.getHealth().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({ status: 'ok' });
    httpMock.verify();
  });

  it('submitPlanJob', () => {
    service.submitPlanJob({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/plan`);
    expect(req.request.method).toBe('POST');
    req.flush({ job_id: 'j1' });
    httpMock.verify();
  });

  it('getJob', () => {
    service.getJob('j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/jobs/j1`);
    req.flush({ status: 'completed' });
    httpMock.verify();
  });

  it('planAndPoll fires onSubmit and completes on terminal status', () => {
    vi.useFakeTimers();
    const emissions: unknown[] = [];
    const submissions: unknown[] = [];
    let completed = false;
    subs.push(
      service.planAndPoll({} as never, 100000, (s) => submissions.push(s)).subscribe({
        next: (v) => emissions.push(v),
        complete: () => (completed = true),
      })
    );

    httpMock.expectOne(`${baseUrl}/plan`).flush({ job_id: 'j1' });
    vi.advanceTimersByTime(0);
    httpMock.expectOne(`${baseUrl}/jobs/j1`).flush({ status: 'completed' });

    expect(submissions).toEqual([{ job_id: 'j1' }]);
    expect(emissions).toEqual([{ status: 'completed' }]);
    expect(completed).toBe(true);
  });

  it('planAndPoll without onSubmit hook also terminates', () => {
    vi.useFakeTimers();
    const emissions: unknown[] = [];
    let completed = false;
    subs.push(
      service.planAndPoll({} as never, 100000).subscribe({
        next: (v) => emissions.push(v),
        complete: () => (completed = true),
      })
    );
    httpMock.expectOne(`${baseUrl}/plan`).flush({ job_id: 'j2' });
    vi.advanceTimersByTime(0);
    httpMock.expectOne(`${baseUrl}/jobs/j2`).flush({ status: 'failed' });
    expect(emissions).toEqual([{ status: 'failed' }]);
    expect(completed).toBe(true);
  });
});
