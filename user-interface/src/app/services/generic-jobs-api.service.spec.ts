import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { GenericJobsApiService } from './generic-jobs-api.service';

describe('GenericJobsApiService', () => {
  let service: GenericJobsApiService;
  let httpMock: HttpTestingController;
  const baseUrl = 'http://localhost:8888';

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [GenericJobsApiService],
    });
    service = TestBed.inject(GenericJobsApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('listJobs without running_only', () => {
    service.listJobs('team-x').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/api/jobs/team-x`);
    expect(req.request.params.keys().length).toBe(0);
    req.flush({ jobs: [] });
  });

  it('listJobs with running_only=true', () => {
    service.listJobs('team-x', true).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/api/jobs/team-x`);
    expect(req.request.params.get('running_only')).toBe('true');
    req.flush({ jobs: [] });
  });

  it('cancel', () => {
    service.cancel('team-x', 'job/1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/${encodeURIComponent('job/1')}/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('resume', () => {
    service.resume('team-x', 'j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/j1/resume`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('restart', () => {
    service.restart('team-x', 'j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/j1/restart`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('interrupt', () => {
    service.interrupt('team-x', 'j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/j1/interrupt`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('delete', () => {
    service.delete('team-x', 'j1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('markAllInterrupted', () => {
    service.markAllInterrupted('team-x').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/api/jobs/team-x/mark-all-interrupted`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });
});
