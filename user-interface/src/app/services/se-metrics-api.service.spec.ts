import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { SeMetricsApiService } from './se-metrics-api.service';
import { environment } from '../../environments/environment';

describe('SeMetricsApiService', () => {
  let service: SeMetricsApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.softwareEngineeringApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [SeMetricsApiService],
    });
    service = TestBed.inject(SeMetricsApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('GETs /metrics/dora with the window_days param', () => {
    service.getMetrics(30).subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === `${baseUrl}/metrics/dora`,
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('window_days')).toBe('30');
    req.flush({});
  });

  it('passes a custom window', () => {
    service.getMetrics(7).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/metrics/dora`);
    expect(req.request.params.get('window_days')).toBe('7');
    req.flush({});
  });
});
