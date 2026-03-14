import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  StartRunRequest,
  StartRunResponse,
  RunStatus,
  DecisionListResponse,
  Decision,
  StudioGridAgentListResponse,
  FindAgentsRequest,
  FindAgentsResponse,
  HealthResponse,
} from '../models';

@Injectable({ providedIn: 'root' })
export class StudioGridApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.studioGridApiUrl;

  /** POST /studio-grid/runs — create a new design project and run */
  startRun(request: StartRunRequest): Observable<StartRunResponse> {
    return this.http.post<StartRunResponse>(`${this.baseUrl}/studio-grid/runs`, request);
  }

  /** GET /studio-grid/runs/:run_id — get run status */
  getRunStatus(runId: string): Observable<RunStatus> {
    return this.http.get<RunStatus>(`${this.baseUrl}/studio-grid/runs/${runId}`);
  }

  /** GET /studio-grid/runs/:run_id/decisions — list decisions for a run */
  listDecisions(runId: string): Observable<DecisionListResponse> {
    return this.http.get<DecisionListResponse>(`${this.baseUrl}/studio-grid/runs/${runId}/decisions`);
  }

  /** POST /studio-grid/decisions/:decision_id/resolve — resolve a decision */
  resolveDecision(decisionId: string, option: string): Observable<Decision> {
    return this.http.post<Decision>(
      `${this.baseUrl}/studio-grid/decisions/${decisionId}/resolve`,
      { option }
    );
  }

  /** GET /studio-grid/registry/agents — list all agents */
  listAgents(): Observable<StudioGridAgentListResponse> {
    return this.http.get<StudioGridAgentListResponse>(`${this.baseUrl}/studio-grid/registry/agents`);
  }

  /** POST /studio-grid/registry/find — find agents by problem + skills */
  findAgents(request: FindAgentsRequest): Observable<FindAgentsResponse> {
    return this.http.post<FindAgentsResponse>(`${this.baseUrl}/studio-grid/registry/find`, request);
  }

  /** GET /health */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
