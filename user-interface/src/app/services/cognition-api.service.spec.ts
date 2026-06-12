import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { CognitionApiService } from './cognition-api.service';
import { environment } from '../../environments/environment';
import type { MemoryEvent, Rule, RuleProposal } from '../models/cognition.model';

describe('CognitionApiService', () => {
  let service: CognitionApiService;
  let httpMock: HttpTestingController;
  const base = `${environment.agentCognitionApiUrl}/agents/a1`;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [CognitionApiService],
    });
    service = TestBed.inject(CognitionApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('is created', () => {
    expect(service).toBeTruthy();
  });

  // Proposals -----------------------------------------------------------------

  it('lists proposals with a status filter', () => {
    const stub: RuleProposal[] = [];
    service.listProposals('a1', { status: 'pending', limit: 20, offset: 5 }).subscribe((r) => {
      expect(r).toEqual(stub);
    });
    const req = httpMock.expectOne(
      `${base}/proposals?status=pending&limit=20&offset=5`,
    );
    expect(req.request.method).toBe('GET');
    req.flush(stub);
  });

  it('lists proposals with no params when query is empty', () => {
    service.listProposals('a1').subscribe((r) => expect(r).toEqual([]));
    const req = httpMock.expectOne(`${base}/proposals`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('approves a proposal via POST and returns the activated rule', () => {
    const rule = { id: 'r1' } as Rule;
    service.approveProposal('a1', 'p1').subscribe((r) => expect(r).toEqual(rule));
    const req = httpMock.expectOne(`${base}/proposals/p1/approve`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush(rule);
  });

  it('surfaces a 409 with detail when approving stale-evidence proposals', () => {
    let captured: unknown;
    service.approveProposal('a1', 'p1').subscribe({ error: (e) => (captured = e) });
    const req = httpMock.expectOne(`${base}/proposals/p1/approve`);
    req.flush({ detail: 'stale evidence' }, { status: 409, statusText: 'Conflict' });
    const err = captured as HttpErrorResponse;
    expect(err.status).toBe(409);
    expect(err.error?.detail).toBe('stale evidence');
  });

  it('rejects a proposal via POST', () => {
    const updated = { id: 'p1', status: 'rejected' } as RuleProposal;
    service.rejectProposal('a1', 'p1').subscribe((r) => expect(r).toEqual(updated));
    const req = httpMock.expectOne(`${base}/proposals/p1/reject`);
    expect(req.request.method).toBe('POST');
    req.flush(updated);
  });

  it('encodes agent and proposal ids in the path', () => {
    const rule = { id: 'r1' } as Rule;
    service.approveProposal('team/agent', 'p 1').subscribe((r) => expect(r).toEqual(rule));
    const req = httpMock.expectOne(
      `${environment.agentCognitionApiUrl}/agents/team%2Fagent/proposals/p%201/approve`,
    );
    req.flush(rule);
  });

  it('throws when called with an empty agent id', () => {
    expect(() => service.listProposals('')).toThrow(/agentId/);
  });

  // Memory --------------------------------------------------------------------

  it('lists memory events with order + count params', () => {
    const stub: MemoryEvent[] = [];
    service.listMemoryEvents('a1', { bySalience: false, topN: 25 }).subscribe();
    const req = httpMock.expectOne(`${base}/memory/events?top_n=25&by_salience=false`);
    expect(req.request.method).toBe('GET');
    req.flush(stub);
  });

  it('lists memory events with a since filter', () => {
    service.listMemoryEvents('a1', { since: '2026-06-01T00:00:00Z' }).subscribe();
    const req = httpMock.expectOne(`${base}/memory/events?since=2026-06-01T00:00:00Z`);
    req.flush([]);
  });

  it('lists summaries for a scale', () => {
    service.listSummaries('a1', 'week', { limit: 10, offset: 0, excludeStale: true }).subscribe();
    const req = httpMock.expectOne(
      `${base}/memory/summaries?scale=week&limit=10&offset=0&exclude_stale=true`,
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  // Rules ---------------------------------------------------------------------

  it('lists rules with a status filter', () => {
    service.listRules('a1', { status: 'active' }).subscribe();
    const req = httpMock.expectOne(`${base}/rules?status=active`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('surfaces a 503 with detail when storage is unavailable', () => {
    let captured: unknown;
    service.listRules('a1').subscribe({ error: (e) => (captured = e) });
    const req = httpMock.expectOne(`${base}/rules`);
    req.flush({ detail: 'unavailable' }, { status: 503, statusText: 'Service Unavailable' });
    const err = captured as HttpErrorResponse;
    expect(err.status).toBe(503);
    expect(err.error?.detail).toBe('unavailable');
  });
});
