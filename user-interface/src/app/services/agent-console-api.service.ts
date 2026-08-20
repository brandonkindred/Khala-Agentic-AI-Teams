import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams, HttpResponse } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { SKIP_NOTIFY_OPTIONS } from '../core/error-handler.interceptor';
import type {
  AgentCatalogQuery,
  AgentDetail,
  AgentSummary,
  TeamGroup,
} from '../models/agent-catalog.model';
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
 * Unified API service for the Agent Console.
 *
 * Owns all HTTP calls to the `/api/agents` backend API, covering:
 *   - Phase 1: Catalog browsing, search, detail & schema.
 *   - Phase 2: Sandbox lifecycle, invoke, golden samples.
 *   - Phase 3: Saved inputs, run history, diff.
 *
 * Follows the project's "one service per API" boundary. Error handling is
 * delegated to the global `errorHandlerInterceptor`; callers that need
 * suppressed toasts should pass a custom `HttpContext` token at call-site.
 */
@Injectable({ providedIn: 'root' })
export class AgentConsoleApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.agentRegistryApiUrl;
  private readonly sandboxesUrl = `${this.baseUrl}/sandboxes`;

  // ============================================================
  // Phase 1 — Catalog
  // ============================================================

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

  // ============================================================
  // Phase 2 — Sandbox lifecycle
  // ============================================================

  listWarmSandboxes(): Observable<SandboxHandle[]> {
    return this.http.get<SandboxHandle[]>(this.sandboxesUrl);
  }

  ensureWarm(agentId: string): Observable<SandboxHandle> {
    return this.http.post<SandboxHandle>(
      `${this.sandboxesUrl}/${encodeURIComponent(agentId)}/warm`,
      {},
    );
  }

  getSandbox(agentId: string): Observable<SandboxHandle> {
    return this.http.get<SandboxHandle>(`${this.sandboxesUrl}/${encodeURIComponent(agentId)}`);
  }

  teardown(agentId: string): Observable<{ agent_id: string; status: string }> {
    return this.http.delete<{ agent_id: string; status: string }>(
      `${this.sandboxesUrl}/${encodeURIComponent(agentId)}`,
    );
  }

  // ============================================================
  // Phase 2 — Invoke + samples
  // ============================================================

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
      { observe: 'response', params, ...SKIP_NOTIFY_OPTIONS },
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

  // ============================================================
  // Phase 3 — Saved inputs
  // ============================================================

  listSavedInputs(agentId: string): Observable<SavedInput[]> {
    return this.http.get<SavedInput[]>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
    );
  }

  /**
   * Create a new saved input for the given agent.
   *
   * @param agentId - The agent identifier (will be URL-encoded).
   * @param body - The saved input payload.
   * @returns The created saved input.
   */
  createSavedInput(agentId: string, body: SavedInputCreate): Observable<SavedInput> {
    return this.http.post<SavedInput>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
      body,
      SKIP_NOTIFY_OPTIONS,
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
      SKIP_NOTIFY_OPTIONS,
    );
  }

  // ============================================================
  // Phase 3 — Runs
  // ============================================================

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

  // ============================================================
  // Phase 3 — Diff
  // ============================================================

  diff(body: DiffRequest): Observable<DiffResult> {
    return this.http.post<DiffResult>(`${this.baseUrl}/diff`, body);
  }
}
