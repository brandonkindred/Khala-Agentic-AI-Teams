import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  AgentCatalogQuery,
  AgentDetail,
  AgentSummary,
  TeamGroup,
} from '../models/agent-catalog.model';

/**
 * Read-only API service for the Agent Console catalog.
 *
 * Talks to the backend agent registry mounted at `/api/agents`. Used by the
 * `AgentCatalogComponent` to drive browsing, search, and drawer detail views.
 */
@Injectable({ providedIn: 'root' })
export class AgentCatalogApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.agentRegistryApiUrl;

  listAgents(query: AgentCatalogQuery = {}): Observable<AgentSummary[]> {
    let params = new HttpParams();
    if (query.team) params = params.set('team', query.team);
    if (query.tag) params = params.set('tag', query.tag);
    if (query.q) params = params.set('q', query.q);
    return this.http.get<AgentSummary[]>(this.baseUrl, { params });
  }

  listTeams(): Observable<TeamGroup[]> {
    return this.http.get<TeamGroup[]>(`${this.baseUrl}/teams`);
  }

  getAgent(id: string): Observable<AgentDetail> {
    return this.http.get<AgentDetail>(`${this.baseUrl}/${encodeURIComponent(id)}`);
  }

  getInputSchema(id: string): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/${encodeURIComponent(id)}/schema/input`);
  }

  getOutputSchema(id: string): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/${encodeURIComponent(id)}/schema/output`);
  }
}
