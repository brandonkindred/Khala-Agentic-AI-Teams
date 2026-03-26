import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import type {
  TeamAssistantConversationState,
  TeamAssistantReadiness,
} from '../models/team-assistant.model';

/**
 * Generic API service for team assistant endpoints.
 *
 * Methods accept a `baseUrl` parameter so one service instance works for all teams.
 * Example: `baseUrl = '/api/soc2-compliance/assistant'`
 */
@Injectable({ providedIn: 'root' })
export class TeamAssistantApiService {
  private readonly http = inject(HttpClient);

  getConversation(baseUrl: string): Observable<TeamAssistantConversationState> {
    return this.http.get<TeamAssistantConversationState>(`${baseUrl}/conversation`);
  }

  sendMessage(baseUrl: string, message: string): Observable<TeamAssistantConversationState> {
    return this.http.post<TeamAssistantConversationState>(`${baseUrl}/conversation/messages`, {
      message,
    });
  }

  updateContext(
    baseUrl: string,
    context: Record<string, unknown>,
  ): Observable<TeamAssistantConversationState> {
    return this.http.put<TeamAssistantConversationState>(`${baseUrl}/conversation/context`, {
      context,
    });
  }

  getReadiness(baseUrl: string): Observable<TeamAssistantReadiness> {
    return this.http.get<TeamAssistantReadiness>(`${baseUrl}/readiness`);
  }
}
