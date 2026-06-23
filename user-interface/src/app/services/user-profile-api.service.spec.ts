import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { UserProfileApiService } from './user-profile-api.service';
import { environment } from '../../environments/environment';

describe('UserProfileApiService', () => {
  let service: UserProfileApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.userProfileApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [UserProfileApiService],
    });
    service = TestBed.inject(UserProfileApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should GET the profile', () => {
    const mock = { user_id: 'default', display_name: 'Brandon', email: '', bio: '' };
    service.getProfile().subscribe((res) => {
      expect(res).toEqual(mock);
    });
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });

  it('should PUT a profile update with body', () => {
    const body = { display_name: 'Brandon', email: 'b@example.com', bio: 'hi' };
    service.updateProfile(body).subscribe((res) => {
      expect(res.display_name).toBe('Brandon');
    });
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush({ user_id: 'default', ...body, preferences: {}, created_at: '', updated_at: '' });
  });

  it('should propagate a server error on getProfile', () => {
    let nextCalled = false;
    let status = 0;
    service.getProfile().subscribe({
      next: () => {
        nextCalled = true;
      },
      error: (err) => {
        status = err.status;
      },
    });
    const req = httpMock.expectOne(baseUrl);
    req.flush('boom', { status: 500, statusText: 'Server Error' });
    expect(nextCalled).toBe(false);
    expect(status).toBe(500);
  });

  it('should propagate a 503 on getOverview', () => {
    let nextCalled = false;
    let status = 0;
    service.getOverview().subscribe({
      next: () => {
        nextCalled = true;
      },
      error: (err) => {
        status = err.status;
      },
    });
    const req = httpMock.expectOne(`${baseUrl}/overview`);
    req.flush('unavailable', { status: 503, statusText: 'Service Unavailable' });
    expect(nextCalled).toBe(false);
    expect(status).toBe(503);
  });

  it('should propagate a server error on updateProfile', () => {
    let nextCalled = false;
    let status = 0;
    service.updateProfile({ display_name: 'x' }).subscribe({
      next: () => {
        nextCalled = true;
      },
      error: (err) => {
        status = err.status;
      },
    });
    const req = httpMock.expectOne(baseUrl);
    req.flush('boom', { status: 500, statusText: 'Server Error' });
    expect(nextCalled).toBe(false);
    expect(status).toBe(500);
  });

  it('should propagate a network error on updateProfile', () => {
    let nextCalled = false;
    let errored = false;
    service.updateProfile({ display_name: 'x' }).subscribe({
      next: () => {
        nextCalled = true;
      },
      error: () => {
        errored = true;
      },
    });
    const req = httpMock.expectOne(baseUrl);
    req.error(new ProgressEvent('network error'));
    expect(nextCalled).toBe(false);
    expect(errored).toBe(true);
  });

  it('should GET the aggregated overview', () => {
    const mock = {
      profile: { user_id: 'default', display_name: '', email: '', bio: '', preferences: {}, created_at: '', updated_at: '' },
      associations: [],
      integrations: [],
    };
    service.getOverview().subscribe((res) => {
      expect(res).toEqual(mock);
    });
    const req = httpMock.expectOne(`${baseUrl}/overview`);
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });
});
