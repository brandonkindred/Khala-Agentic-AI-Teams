import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  MemoryEvent,
  MemoryEventsQuery,
  PeriodSummary,
  ProposalsQuery,
  Rule,
  RuleProposal,
  RulesQuery,
  Scale,
  SummariesQuery,
} from '../models/cognition.model';

/**
 * API client for the agent cognition surface (memory, rules, proposals).
 *
 * Talks to the unified API at `${environment.agentCognitionApiUrl}`
 * (`/api/cognition`). Every endpoint is scoped to an `agent_id`. Errors are
 * not handled here; subscribers surface `err?.error?.detail` at the component
 * layer (existing project convention — see `AgentRunnerApiService`).
 */
@Injectable({ providedIn: 'root' })
export class CognitionApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.agentCognitionApiUrl;

  /**
   * Build the per-agent base URL.
   *
   * Precondition: `agentId` is a non-empty string. Callers must scope every
   * request to a selected agent; an empty id is a caller bug, so we fail loudly
   * rather than issue a request against `…/agents//…`.
   */
  private agentBase(agentId: string): string {
    if (!agentId) {
      throw new Error('CognitionApiService: agentId must be a non-empty string.');
    }
    return `${this.baseUrl}/agents/${encodeURIComponent(agentId)}`;
  }

  // -------------------------------------------------------------------------
  // Proposals (the HITL gate)
  // -------------------------------------------------------------------------

  listProposals(agentId: string, query: ProposalsQuery = {}): Observable<RuleProposal[]> {
    let params = new HttpParams();
    if (query.status) params = params.set('status', query.status);
    if (query.limit != null) params = params.set('limit', String(query.limit));
    if (query.offset != null) params = params.set('offset', String(query.offset));
    return this.http.get<RuleProposal[]>(`${this.agentBase(agentId)}/proposals`, { params });
  }

  /** Approve a pending proposal; returns the activated rule. */
  approveProposal(agentId: string, proposalId: string): Observable<Rule> {
    return this.http.post<Rule>(
      `${this.agentBase(agentId)}/proposals/${encodeURIComponent(proposalId)}/approve`,
      {},
    );
  }

  /** Reject a pending proposal; returns the updated proposal. */
  rejectProposal(agentId: string, proposalId: string): Observable<RuleProposal> {
    return this.http.post<RuleProposal>(
      `${this.agentBase(agentId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
      {},
    );
  }

  // -------------------------------------------------------------------------
  // Memory
  // -------------------------------------------------------------------------

  listMemoryEvents(agentId: string, query: MemoryEventsQuery = {}): Observable<MemoryEvent[]> {
    let params = new HttpParams();
    if (query.topN != null) params = params.set('top_n', String(query.topN));
    if (query.bySalience != null) params = params.set('by_salience', String(query.bySalience));
    if (query.since) params = params.set('since', query.since);
    return this.http.get<MemoryEvent[]>(`${this.agentBase(agentId)}/memory/events`, { params });
  }

  listSummaries(
    agentId: string,
    scale: Scale,
    query: SummariesQuery = {},
  ): Observable<PeriodSummary[]> {
    let params = new HttpParams().set('scale', scale);
    if (query.limit != null) params = params.set('limit', String(query.limit));
    if (query.offset != null) params = params.set('offset', String(query.offset));
    if (query.excludeStale != null) params = params.set('exclude_stale', String(query.excludeStale));
    return this.http.get<PeriodSummary[]>(`${this.agentBase(agentId)}/memory/summaries`, { params });
  }

  // -------------------------------------------------------------------------
  // Rules
  // -------------------------------------------------------------------------

  listRules(agentId: string, query: RulesQuery = {}): Observable<Rule[]> {
    let params = new HttpParams();
    if (query.status) params = params.set('status', query.status);
    if (query.limit != null) params = params.set('limit', String(query.limit));
    if (query.offset != null) params = params.set('offset', String(query.offset));
    return this.http.get<Rule[]>(`${this.agentBase(agentId)}/rules`, { params });
  }
}
