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

  it('should GET associations without a filter', () => {
    service.getAssociations().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/associations`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.has('artifact_type')).toBe(false);
    req.flush({ user_id: 'default', associations: [] });
  });

  it('should GET associations with an artifact_type filter', () => {
    service.getAssociations('brand').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/associations`);
    expect(req.request.params.get('artifact_type')).toBe('brand');
    req.flush({ user_id: 'default', associations: [] });
  });

  it('should GET integrations', () => {
    const mock = [{ id: 'slack', type: 'slack', enabled: true, channel: '#eng' }];
    service.getIntegrations().subscribe((res) => {
      expect(res).toEqual(mock);
    });
    const req = httpMock.expectOne(`${baseUrl}/integrations`);
    expect(req.request.method).toBe('GET');
    req.flush(mock);
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
