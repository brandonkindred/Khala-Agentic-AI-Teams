import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { IntegrationsApiService } from './integrations-api.service';
import { environment } from '../../environments/environment';

describe('IntegrationsApiService', () => {
  let service: IntegrationsApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.integrationsApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [IntegrationsApiService],
    });
    service = TestBed.inject(IntegrationsApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /api/integrations for getIntegrations', () => {
    const mockList = [{ id: 'slack', type: 'slack', enabled: true, channel: '#eng' }];
    service.getIntegrations().subscribe((res) => {
      expect(res).toEqual(mockList);
    });
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('GET');
    req.flush(mockList);
  });

  it('should call GET /api/integrations/slack for getSlackConfig', () => {
    const mockConfig = { enabled: true, webhook_url: null, webhook_configured: true, channel_display_name: '#eng' };
    service.getSlackConfig().subscribe((res) => {
      expect(res).toEqual(mockConfig);
    });
    const req = httpMock.expectOne(`${baseUrl}/slack`);
    expect(req.request.method).toBe('GET');
    req.flush(mockConfig);
  });

  it('should call PUT /api/integrations/slack for updateSlackConfig with body', () => {
    const body = { enabled: true, webhook_url: 'https://hooks.slack.com/x', channel_display_name: '#eng' };
    const mockResponse = { enabled: true, webhook_url: null, webhook_configured: true, channel_display_name: '#eng' };
    service.updateSlackConfig(body).subscribe((res) => {
      expect(res).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${baseUrl}/slack`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush(mockResponse);
  });

  it('getSlackOAuthUrl GET', () => {
    service.getSlackOAuthUrl().subscribe();
    httpMock.expectOne(`${baseUrl}/slack/oauth/connect`).flush({});
  });

  it('disconnectSlack DELETE', () => {
    service.disconnectSlack().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/slack/oauth`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('getMediumConfig GET', () => {
    service.getMediumConfig().subscribe();
    httpMock.expectOne(`${baseUrl}/medium`).flush({});
  });

  it('updateMediumConfig PUT', () => {
    service.updateMediumConfig({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('importMediumSession POST', () => {
    service.importMediumSession({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getGoogleBrowserLoginStatus GET', () => {
    service.getGoogleBrowserLoginStatus().subscribe();
    httpMock.expectOne(`${baseUrl}/google-browser-login`).flush({});
  });

  it('putGoogleBrowserLoginCredentials PUT', () => {
    service.putGoogleBrowserLoginCredentials({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/google-browser-login`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('deleteGoogleBrowserLoginCredentials DELETE', () => {
    service.deleteGoogleBrowserLoginCredentials().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/google-browser-login`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('mediumBrowserLoginSession POST', () => {
    service.mediumBrowserLoginSession().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session/browser-login`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('clearMediumSession DELETE', () => {
    service.clearMediumSession().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });
});
