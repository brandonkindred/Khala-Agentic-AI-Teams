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
 * layer (existing project convention — see `AgentConsoleApiService`).
 *
 * Preconditions (all methods): `agentId` — and, where applicable, `proposalId`
 * — are non-empty strings. Violations throw rather than issue a malformed URL.
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

  private requireProposalId(proposalId: string): string {
    if (!proposalId) {
      throw new Error('CognitionApiService: proposalId must be a non-empty string.');
    }
    return encodeURIComponent(proposalId);
  }

  // -------------------------------------------------------------------------
  // Proposals (the HITL gate)
  // -------------------------------------------------------------------------

  /**
   * List an agent's rule proposals, newest first.
   *
   * @param agentId Non-empty agent id.
   * @param query Optional `status` filter and `limit`/`offset` paging.
   * @returns Observable of the matching proposals.
   */
  listProposals(agentId: string, query: ProposalsQuery = {}): Observable<RuleProposal[]> {
    let params = new HttpParams();
    if (query.status != null) params = params.set('status', query.status);
    if (query.limit != null) params = params.set('limit', String(query.limit));
    if (query.offset != null) params = params.set('offset', String(query.offset));
    return this.http.get<RuleProposal[]>(`${this.agentBase(agentId)}/proposals`, { params });
  }

  /**
   * Approve a pending proposal.
   *
   * @param agentId Non-empty agent id.
   * @param proposalId Non-empty proposal id.
   * @returns Observable of the activated `Rule`.
   */
  approveProposal(agentId: string, proposalId: string): Observable<Rule> {
    const base = this.agentBase(agentId);
    return this.http.post<Rule>(`${base}/proposals/${this.requireProposalId(proposalId)}/approve`, {});
  }

  /**
   * Reject a pending proposal.
   *
   * @param agentId Non-empty agent id.
   * @param proposalId Non-empty proposal id.
   * @returns Observable of the updated `RuleProposal` (status `rejected`).
   */
  rejectProposal(agentId: string, proposalId: string): Observable<RuleProposal> {
    const base = this.agentBase(agentId);
    return this.http.post<RuleProposal>(`${base}/proposals/${this.requireProposalId(proposalId)}/reject`, {});
  }

  // -------------------------------------------------------------------------
  // Memory
  // -------------------------------------------------------------------------

  /**
   * List an agent's memory events.
   *
   * @param agentId Non-empty agent id.
   * @param query Optional `topN`, `bySalience` ordering, and `since` cutoff.
   * @returns Observable of the matching events.
   */
  listMemoryEvents(agentId: string, query: MemoryEventsQuery = {}): Observable<MemoryEvent[]> {
    let params = new HttpParams();
    if (query.topN != null) params = params.set('top_n', String(query.topN));
    if (query.bySalience != null) params = params.set('by_salience', String(query.bySalience));
    if (query.since != null) params = params.set('since', query.since);
    return this.http.get<MemoryEvent[]>(`${this.agentBase(agentId)}/memory/events`, { params });
  }

  /**
   * List an agent's period summaries at a scale.
   *
   * @param agentId Non-empty agent id.
   * @param scale One of `day|week|month|year`.
   * @param query Optional `limit`/`offset` paging and `excludeStale`.
   * @returns Observable of the matching summaries.
   */
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

  /**
   * List an agent's rules, highest priority first.
   *
   * @param agentId Non-empty agent id.
   * @param query Optional `status` filter and `limit`/`offset` paging.
   * @returns Observable of the matching rules.
   */
  listRules(agentId: string, query: RulesQuery = {}): Observable<Rule[]> {
    let params = new HttpParams();
    if (query.status != null) params = params.set('status', query.status);
    if (query.limit != null) params = params.set('limit', String(query.limit));
    if (query.offset != null) params = params.set('offset', String(query.offset));
    return this.http.get<Rule[]>(`${this.agentBase(agentId)}/rules`, { params });
  }
}
