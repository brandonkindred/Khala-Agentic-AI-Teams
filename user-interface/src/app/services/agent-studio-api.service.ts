import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { SKIP_NOTIFY_OPTIONS } from '../core/error-handler.interceptor';
import type {
  AgentDefinition,
  AgentStudioDraft,
  AgentStudioDraftSummary,
  ConversationStateResponse,
  RenameDraftRequest,
  SaveAgentRequest,
  SaveAgentResponse,
  SaveDraftRequest,
  SendMessageRequest,
  StartConversationRequest,
} from '../models/agent-studio.model';

/**
 * API client for the Agent Studio Stage-1 build flow.
 *
 * Talks to the backend `agent_studio` team mounted at `/api/agent-studio`
 * (conversations, clone-from-registry, save agent, and user-scoped drafts).
 * Errors are not handled here; subscribers should surface `err?.error?.detail`
 * at the component layer (existing project convention — see `ProductDeliveryService`).
 */
@Injectable({ providedIn: 'root' })
export class AgentStudioApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.agentStudioApiUrl;

  // -------------------------------------------------------------------------
  // Conversations
  // -------------------------------------------------------------------------

  startConversation(req: StartConversationRequest): Observable<ConversationStateResponse> {
    return this.http.post<ConversationStateResponse>(`${this.baseUrl}/conversations`, req);
  }

  sendMessage(
    conversationId: string,
    req: SendMessageRequest,
  ): Observable<ConversationStateResponse> {
    return this.http.post<ConversationStateResponse>(
      `${this.baseUrl}/conversations/${encodeURIComponent(conversationId)}/messages`,
      req,
    );
  }

  // -------------------------------------------------------------------------
  // Clone / Save
  // -------------------------------------------------------------------------

  cloneFromRegistry(agentId: string): Observable<AgentDefinition> {
    return this.http.post<AgentDefinition>(
      `${this.baseUrl}/agents/from-registry/${encodeURIComponent(agentId)}`,
      null,
    );
  }

  saveAgent(req: SaveAgentRequest): Observable<SaveAgentResponse> {
    return this.http.post<SaveAgentResponse>(`${this.baseUrl}/agents`, req);
  }

  // -------------------------------------------------------------------------
  // Drafts
  // -------------------------------------------------------------------------

  createDraft(req: SaveDraftRequest): Observable<AgentStudioDraftSummary> {
    return this.http.post<AgentStudioDraftSummary>(`${this.baseUrl}/drafts`, req);
  }

  updateDraft(draftId: string, req: SaveDraftRequest): Observable<AgentStudioDraftSummary> {
    return this.http.put<AgentStudioDraftSummary>(
      `${this.baseUrl}/drafts/${encodeURIComponent(draftId)}`,
      req,
    );
  }

  listDrafts(limit?: number, offset?: number): Observable<AgentStudioDraftSummary[]> {
    let params = new HttpParams();
    if (limit !== undefined) params = params.set('limit', limit);
    if (offset !== undefined) params = params.set('offset', offset);
    return this.http.get<AgentStudioDraftSummary[]>(`${this.baseUrl}/drafts`, { params });
  }

  getDraft(draftId: string): Observable<AgentStudioDraft> {
    return this.http.get<AgentStudioDraft>(`${this.baseUrl}/drafts/${encodeURIComponent(draftId)}`);
  }

  renameDraft(draftId: string, name: string): Observable<AgentStudioDraftSummary> {
    const req: RenameDraftRequest = { name };
    return this.http.patch<AgentStudioDraftSummary>(
      `${this.baseUrl}/drafts/${encodeURIComponent(draftId)}`,
      req,
    );
  }

  deleteDraft(draftId: string): Observable<{ draft_id: string; status: string }> {
    return this.http.delete<{ draft_id: string; status: string }>(
      `${this.baseUrl}/drafts/${encodeURIComponent(draftId)}`,
      SKIP_NOTIFY_OPTIONS,
    );
  }
}
