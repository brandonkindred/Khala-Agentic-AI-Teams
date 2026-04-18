import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams, HttpResponse } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  InvokeEnvelope,
  SandboxHandle,
} from '../models/agent-runner.model';
import type {
  DiffRequest,
  DiffResult,
  RunRecord,
  RunSummary,
  SavedInput,
  SavedInputCreate,
  SavedInputUpdate,
} from '../models/agent-history.model';

/**
 * Agent Console Runner API (Phases 2 + 3).
 *
 * Phase 2: sandbox lifecycle + invoke + golden samples.
 * Phase 3: saved inputs, run history, diff.
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

  /**
   * Return the full HttpResponse so the caller can branch on status. A
   * ``202 Accepted`` body is the sandbox "still warming" envelope, **not** a
   * real invoke envelope; Angular's HttpClient delivers it through ``next``
   * (not ``error``) because 202 is 2xx. The runner must inspect ``.status``
   * and treat 202 as a retry prompt rather than a successful invocation.
   */
  invoke(
    agentId: string,
    body: unknown,
    savedInputId?: string | null,
  ): Observable<HttpResponse<InvokeEnvelope | Record<string, unknown>>> {
    let params = new HttpParams();
    if (savedInputId) params = params.set('saved_input_id', savedInputId);
    return this.http.post<InvokeEnvelope | Record<string, unknown>>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/invoke`,
      body,
      { observe: 'response', params },
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

  // ------------------------------------------------------------
  // Saved inputs (Phase 3)
  // ------------------------------------------------------------

  listSavedInputs(agentId: string): Observable<SavedInput[]> {
    return this.http.get<SavedInput[]>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
    );
  }

  createSavedInput(agentId: string, body: SavedInputCreate): Observable<SavedInput> {
    return this.http.post<SavedInput>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
      body,
    );
  }

  updateSavedInput(savedId: string, body: SavedInputUpdate): Observable<SavedInput> {
    return this.http.put<SavedInput>(
      `${this.baseUrl}/saved-inputs/${encodeURIComponent(savedId)}`,
      body,
    );
  }

  deleteSavedInput(savedId: string): Observable<{ id: string; status: string }> {
    return this.http.delete<{ id: string; status: string }>(
      `${this.baseUrl}/saved-inputs/${encodeURIComponent(savedId)}`,
    );
  }

  // ------------------------------------------------------------
  // Runs (Phase 3)
  // ------------------------------------------------------------

  listRuns(agentId: string, cursor?: string | null, limit = 20): Observable<RunSummary[]> {
    let params = new HttpParams().set('limit', String(limit));
    if (cursor) params = params.set('cursor', cursor);
    return this.http.get<RunSummary[]>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/runs`,
      { params },
    );
  }

  getRun(runId: string): Observable<RunRecord> {
    return this.http.get<RunRecord>(`${this.baseUrl}/runs/${encodeURIComponent(runId)}`);
  }

  deleteRun(runId: string): Observable<{ id: string; status: string }> {
    return this.http.delete<{ id: string; status: string }>(
      `${this.baseUrl}/runs/${encodeURIComponent(runId)}`,
    );
  }

  // ------------------------------------------------------------
  // Diff (Phase 3)
  // ------------------------------------------------------------

  diff(body: DiffRequest): Observable<DiffResult> {
    return this.http.post<DiffResult>(`${this.baseUrl}/diff`, body);
  }
}
