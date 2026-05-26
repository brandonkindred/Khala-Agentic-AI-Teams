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
});
