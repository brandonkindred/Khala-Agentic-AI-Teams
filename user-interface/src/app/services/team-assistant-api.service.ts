import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import type {
  TeamAssistantConversationState,
  TeamAssistantLaunchResponse,
  TeamAssistantReadiness,
  TeamConversationSummary,
} from '../models/team-assistant.model';

/**
 * Generic API service for team assistant endpoints.
 *
 * Methods accept a `baseUrl` parameter so one service instance works for all teams.
 * Example: `baseUrl = '/api/blogging/assistant'`
 */
@Injectable({ providedIn: 'root' })
export class TeamAssistantApiService {
  private readonly http = inject(HttpClient);

  private _params(conversationId?: string): HttpParams {
    let params = new HttpParams();
    if (conversationId) {
      params = params.set('conversation_id', conversationId);
    }
    return params;
  }

  getConversation(baseUrl: string, conversationId?: string): Observable<TeamAssistantConversationState> {
    return this.http.get<TeamAssistantConversationState>(`${baseUrl}/conversation`, {
      params: this._params(conversationId),
    });
  }

  sendMessage(baseUrl: string, message: string, conversationId?: string): Observable<TeamAssistantConversationState> {
    return this.http.post<TeamAssistantConversationState>(`${baseUrl}/conversation/messages`, {
      message,
    }, { params: this._params(conversationId) });
  }

  updateContext(
    baseUrl: string,
    context: Record<string, unknown>,
    conversationId?: string,
  ): Observable<TeamAssistantConversationState> {
    return this.http.put<TeamAssistantConversationState>(`${baseUrl}/conversation/context`, {
      context,
    }, { params: this._params(conversationId) });
  }

  getReadiness(baseUrl: string, conversationId?: string): Observable<TeamAssistantReadiness> {
    return this.http.get<TeamAssistantReadiness>(`${baseUrl}/readiness`, {
      params: this._params(conversationId),
    });
  }

  /**
   * Trigger the team's real workflow from the conversation context.
   *
   * The backend (`POST {baseUrl}/launch`) validates readiness, builds the
   * team-specific request from the stored context, dispatches it in-process,
   * and links the returned `job_id` to the conversation.
   */
  launch(
    baseUrl: string,
    conversationId?: string,
  ): Observable<TeamAssistantLaunchResponse> {
    return this.http.post<TeamAssistantLaunchResponse>(
      `${baseUrl}/launch`,
      null,
      { params: this._params(conversationId) },
    );
  }

  resetConversation(baseUrl: string, conversationId?: string): Observable<TeamAssistantConversationState> {
    return this.http.delete<TeamAssistantConversationState>(`${baseUrl}/conversation`, {
      params: this._params(conversationId),
    });
  }

  // --- Per-conversation endpoints ---

  createConversation(baseUrl: string): Observable<{ conversation_id: string }> {
    return this.http.post<{ conversation_id: string }>(`${baseUrl}/conversations`, {});
  }

  listConversations(baseUrl: string): Observable<{ conversations: TeamConversationSummary[] }> {
    return this.http.get<{ conversations: TeamConversationSummary[] }>(`${baseUrl}/conversations`);
  }

  listUnlinkedConversations(baseUrl: string): Observable<{ conversations: TeamConversationSummary[] }> {
    return this.http.get<{ conversations: TeamConversationSummary[] }>(`${baseUrl}/conversations/unlinked`);
  }

  getConversationByJob(baseUrl: string, jobId: string): Observable<TeamAssistantConversationState> {
    return this.http.get<TeamAssistantConversationState>(
      `${baseUrl}/conversations/by-job/${encodeURIComponent(jobId)}`
    );
  }

  linkConversationToJob(baseUrl: string, conversationId: string, jobId: string): Observable<unknown> {
    return this.http.put(
      `${baseUrl}/conversations/${encodeURIComponent(conversationId)}/link-job`,
      { job_id: jobId }
    );
  }

  deleteConversation(baseUrl: string, conversationId: string): Observable<unknown> {
    return this.http.delete(`${baseUrl}/conversations/${encodeURIComponent(conversationId)}`);
  }
}
