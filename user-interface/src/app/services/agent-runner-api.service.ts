import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  InvokeEnvelope,
  SandboxHandle,
} from '../models/agent-runner.model';

/**
 * Phase 2 Agent Console Runner API.
 *
 * Handles both the sandbox lifecycle (`/api/agents/sandboxes/*`) and the
 * invoke endpoint (`POST /api/agents/{id}/invoke`).
 */
@Injectable({ providedIn: 'root' })
export class AgentRunnerApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.agentRegistryApiUrl;
  private readonly sandboxesUrl = `${this.baseUrl}/sandboxes`;

  // ------------------------------------------------------------
  // Sandbox lifecycle
  // ------------------------------------------------------------

  listWarmSandboxes(): Observable<SandboxHandle[]> {
    return this.http.get<SandboxHandle[]>(this.sandboxesUrl);
  }

  ensureWarm(team: string): Observable<SandboxHandle> {
    return this.http.post<SandboxHandle>(`${this.sandboxesUrl}/${encodeURIComponent(team)}`, {});
  }

  getSandbox(team: string): Observable<SandboxHandle> {
    return this.http.get<SandboxHandle>(`${this.sandboxesUrl}/${encodeURIComponent(team)}`);
  }

  teardown(team: string): Observable<{ team: string; status: string }> {
    return this.http.delete<{ team: string; status: string }>(
      `${this.sandboxesUrl}/${encodeURIComponent(team)}`,
    );
  }

  // ------------------------------------------------------------
  // Invoke + samples
  // ------------------------------------------------------------

  invoke(agentId: string, body: unknown): Observable<InvokeEnvelope> {
    return this.http.post<InvokeEnvelope>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/invoke`,
      body,
      { observe: 'body' },
    );
  }

  listSamples(agentId: string): Observable<string[]> {
    return this.http.get<string[]>(`${this.baseUrl}/${encodeURIComponent(agentId)}/samples`);
  }

  getSample(agentId: string, name: string): Observable<unknown> {
    return this.http.get<unknown>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/samples/${encodeURIComponent(name)}`,
    );
  }
}
