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

  /**
   * List registered agents, optionally filtered by team, tag, or free-text query.
   *
   * @param query - Optional filter criteria (team, tag, q). Omitted keys are not sent.
   * @returns Observable emitting an array of agent summaries.
   */
  listAgents(query: AgentCatalogQuery = {}): Observable<AgentSummary[]> {
    let params = new HttpParams();
    if (query.team) params = params.set('team', query.team);
    if (query.tag) params = params.set('tag', query.tag);
    if (query.q) params = params.set('q', query.q);
    return this.http.get<AgentSummary[]>(this.baseUrl, { params });
  }

  /**
   * List all team groups with their agent counts.
   *
   * @returns Observable emitting team group metadata.
   */
  listTeams(): Observable<TeamGroup[]> {
    return this.http.get<TeamGroup[]>(`${this.baseUrl}/teams`);
  }

  /**
   * Fetch detailed metadata for a single agent.
   *
   * @param id - Agent identifier; will be URL-encoded before being sent.
   * @returns Observable emitting the agent detail (manifest + anatomy markdown).
   */
  getAgent(id: string): Observable<AgentDetail> {
    return this.http.get<AgentDetail>(`${this.baseUrl}/${encodeURIComponent(id)}`);
  }

  /**
   * Fetch the JSON Schema describing an agent's expected input.
   *
   * @param id - Agent identifier; will be URL-encoded.
   * @returns Observable emitting the raw schema object.
   */
  getInputSchema(id: string): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/${encodeURIComponent(id)}/schema/input`);
  }

  /**
   * Fetch the JSON Schema describing an agent's output.
   *
   * @param id - Agent identifier; will be URL-encoded.
   * @returns Observable emitting the raw schema object.
   */
  getOutputSchema(id: string): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/${encodeURIComponent(id)}/schema/output`);
  }

  // ============================================================
  // Phase 2 — Sandbox lifecycle
  // ============================================================

  /**
   * List all currently warm sandboxes across agents.
   *
   * @returns Observable emitting an array of sandbox handles.
   */
  listWarmSandboxes(): Observable<SandboxHandle[]> {
    return this.http.get<SandboxHandle[]>(this.sandboxesUrl);
  }

  /**
   * Ensure a warm sandbox exists for the given agent. Creates one if needed.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @returns Observable emitting the sandbox handle once ready.
   */
  ensureWarm(agentId: string): Observable<SandboxHandle> {
    return this.http.post<SandboxHandle>(
      `${this.sandboxesUrl}/${encodeURIComponent(agentId)}/warm`,
      {},
    );
  }

  /**
   * Fetch the current sandbox status for an agent.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @returns Observable emitting the sandbox handle.
   */
  getSandbox(agentId: string): Observable<SandboxHandle> {
    return this.http.get<SandboxHandle>(`${this.sandboxesUrl}/${encodeURIComponent(agentId)}`);
  }

  /**
   * Tear down (destroy) the sandbox for an agent.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @returns Observable emitting the teardown confirmation.
   */
  teardown(agentId: string): Observable<{ agent_id: string; status: string }> {
    return this.http.delete<{ agent_id: string; status: string }>(
      `${this.sandboxesUrl}/${encodeURIComponent(agentId)}`,
      SKIP_NOTIFY_OPTIONS,
    );
  }

  // ============================================================
  // Phase 2 — Invoke + samples
  // ============================================================

  /**
   * Invoke an agent with the given input payload.
   *
   * Returns the full `HttpResponse` so the caller can branch on status. A
   * `202 Accepted` body is the sandbox "still warming" envelope, **not** a
   * real invoke envelope; Angular's HttpClient delivers it through `next`
   * (not `error`) because 202 is 2xx. The runner must inspect `.status`
   * and treat 202 as a retry prompt rather than a successful invocation.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @param body - The invoke payload (matches the agent's input schema).
   * @param savedInputId - Optional saved-input id to associate with this run.
   * @returns Observable emitting the full HTTP response.
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

  /**
   * List golden sample names for an agent.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @returns Observable emitting an array of sample file names.
   */
  listSamples(agentId: string): Observable<string[]> {
    return this.http.get<string[]>(`${this.baseUrl}/${encodeURIComponent(agentId)}/samples`);
  }

  /**
   * Fetch a specific golden sample's content.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @param name - Sample file name; will be URL-encoded.
   * @returns Observable emitting the parsed sample payload.
   */
  getSample(agentId: string, name: string): Observable<unknown> {
    return this.http.get<unknown>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/samples/${encodeURIComponent(name)}`,
    );
  }

  // ============================================================
  // Phase 3 — Saved inputs
  // ============================================================

  /**
   * List all saved inputs for an agent.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @returns Observable emitting an array of saved inputs.
   */
  listSavedInputs(agentId: string): Observable<SavedInput[]> {
    return this.http.get<SavedInput[]>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
    );
  }

  /**
   * Create a new saved input for the given agent.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @param body - The saved input payload (name, input_data, optional description).
   * @returns Observable emitting the created saved input.
   */
  createSavedInput(agentId: string, body: SavedInputCreate): Observable<SavedInput> {
    return this.http.post<SavedInput>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/saved-inputs`,
      body,
      SKIP_NOTIFY_OPTIONS,
    );
  }

  /**
   * Update an existing saved input.
   *
   * @param savedId - The saved input's unique id; will be URL-encoded.
   * @param body - Fields to update (name, input_data, description).
   * @returns Observable emitting the updated saved input.
   */
  updateSavedInput(savedId: string, body: SavedInputUpdate): Observable<SavedInput> {
    return this.http.put<SavedInput>(
      `${this.baseUrl}/saved-inputs/${encodeURIComponent(savedId)}`,
      body,
    );
  }

  /**
   * Delete a saved input by id.
   *
   * @param savedId - The saved input's unique id; will be URL-encoded.
   * @returns Observable emitting the deletion confirmation.
   */
  deleteSavedInput(savedId: string): Observable<{ id: string; status: string }> {
    return this.http.delete<{ id: string; status: string }>(
      `${this.baseUrl}/saved-inputs/${encodeURIComponent(savedId)}`,
      SKIP_NOTIFY_OPTIONS,
    );
  }

  // ============================================================
  // Phase 3 — Runs
  // ============================================================

  /**
   * List run history for an agent with cursor-based pagination.
   *
   * @param agentId - Agent identifier; will be URL-encoded.
   * @param cursor - Opaque pagination cursor from a previous response (optional).
   * @param limit - Maximum number of runs to return (default 20).
   * @returns Observable emitting an array of run summaries.
   */
  listRuns(agentId: string, cursor?: string | null, limit = 20): Observable<RunSummary[]> {
    let params = new HttpParams().set('limit', String(limit));
    if (cursor) params = params.set('cursor', cursor);
    return this.http.get<RunSummary[]>(
      `${this.baseUrl}/${encodeURIComponent(agentId)}/runs`,
      { params },
    );
  }

  /**
   * Fetch a complete run record including input, output, and logs.
   *
   * @param runId - The run's unique id; will be URL-encoded.
   * @returns Observable emitting the full run record.
   */
  getRun(runId: string): Observable<RunRecord> {
    return this.http.get<RunRecord>(`${this.baseUrl}/runs/${encodeURIComponent(runId)}`);
  }

  /**
   * Delete a run from history.
   *
   * @param runId - The run's unique id; will be URL-encoded.
   * @returns Observable emitting the deletion confirmation.
   */
  deleteRun(runId: string): Observable<{ id: string; status: string }> {
    return this.http.delete<{ id: string; status: string }>(
      `${this.baseUrl}/runs/${encodeURIComponent(runId)}`,
    );
  }

  // ============================================================
  // Phase 3 — Diff
  // ============================================================

  /**
   * Compute a unified diff between two data sources (runs, saved inputs, or inline data).
   *
   * @param body - The diff request specifying left and right sides.
   * @returns Observable emitting the diff result with unified patch text.
   */
  diff(body: DiffRequest): Observable<DiffResult> {
    return this.http.post<DiffResult>(`${this.baseUrl}/diff`, body);
  }
}
