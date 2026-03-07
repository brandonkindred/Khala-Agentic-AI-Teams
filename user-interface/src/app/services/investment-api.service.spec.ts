import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { InvestmentApiService } from './investment-api.service';
import { environment } from '../../environments/environment';

describe('InvestmentApiService', () => {
  let service: InvestmentApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.investmentApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [InvestmentApiService],
    });
    service = TestBed.inject(InvestmentApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /health for healthCheck', () => {
    service.healthCheck().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });

  it('should call POST /profiles for createProfile', () => {
    const body = { user_id: 'u1', name: 'Profile 1' };
    service.createProfile(body).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/profiles`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ profile_id: 'p1' });
  });

  it('should call GET /profiles/{userId} for getProfile', () => {
    const userId = 'u1';
    service.getProfile(userId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/profiles/${userId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ profile: {} });
  });

  it('should call POST /proposals/create for createProposal', () => {
    const body = { profile_id: 'p1', title: 'Proposal 1' };
    service.createProposal(body).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/proposals/create`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ proposal_id: 'prop1' });
  });

  it('should call GET /proposals/{proposalId} for getProposal', () => {
    const proposalId = 'prop1';
    service.getProposal(proposalId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/proposals/${proposalId}`);
    expect(req.request.method).toBe('GET');
    req.flush({ proposal: {} });
  });

  it('should call GET /workflow/status for getWorkflowStatus', () => {
    service.getWorkflowStatus().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/workflow/status`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'idle' });
  });
});
