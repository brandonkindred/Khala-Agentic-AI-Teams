import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  AgenticTeamSummary,
  AgenticTeamDetailResponse,
  AgenticTeamAgent,
  CreateAgenticTeamRequest,
  CreateAgenticTeamResponse,
  AgenticConversationStateResponse,
  AgenticConversationSummary,
  AgentEnvProvisionSummary,
  ProcessDefinition,
  RosterValidationResult,
} from '../models';

@Injectable({ providedIn: 'root' })
export class AgenticTeamApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.agenticTeamProvisioningApiUrl;

  // Health
  health(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${this.base}/health`);
  }

  // Teams
  createTeam(req: CreateAgenticTeamRequest): Observable<CreateAgenticTeamResponse> {
    return this.http.post<CreateAgenticTeamResponse>(`${this.base}/teams`, req);
  }

  listTeams(): Observable<AgenticTeamSummary[]> {
    return this.http.get<AgenticTeamSummary[]>(`${this.base}/teams`);
  }

  getTeam(teamId: string): Observable<AgenticTeamDetailResponse> {
    return this.http.get<AgenticTeamDetailResponse>(`${this.base}/teams/${teamId}`);
  }

  // Roster (agents pool)
  listTeamAgents(teamId: string): Observable<AgenticTeamAgent[]> {
    return this.http.get<AgenticTeamAgent[]>(`${this.base}/teams/${teamId}/agents`);
  }

  /** Validate whether the roster fully covers the team's process needs. */
  validateRoster(teamId: string): Observable<RosterValidationResult> {
    return this.http.get<RosterValidationResult>(`${this.base}/teams/${teamId}/roster/validation`);
  }

  // Processes
  listProcesses(teamId: string): Observable<ProcessDefinition[]> {
    return this.http.get<ProcessDefinition[]>(`${this.base}/teams/${teamId}/processes`);
  }

  getProcess(processId: string): Observable<ProcessDefinition> {
    return this.http.get<ProcessDefinition>(`${this.base}/processes/${processId}`);
  }

  // Conversations
  createConversation(teamId: string, initialMessage?: string): Observable<AgenticConversationStateResponse> {
    return this.http.post<AgenticConversationStateResponse>(`${this.base}/conversations`, {
      team_id: teamId,
      initial_message: initialMessage ?? null,
    });
  }

  sendMessage(conversationId: string, message: string): Observable<AgenticConversationStateResponse> {
    return this.http.post<AgenticConversationStateResponse>(
      `${this.base}/conversations/${conversationId}/messages`,
      { message },
    );
  }

  getConversation(conversationId: string): Observable<AgenticConversationStateResponse> {
    return this.http.get<AgenticConversationStateResponse>(`${this.base}/conversations/${conversationId}`);
  }

  listConversations(teamId: string): Observable<AgenticConversationSummary[]> {
    return this.http.get<AgenticConversationSummary[]>(`${this.base}/teams/${teamId}/conversations`);
  }

  /** Sandbox provisioning status for each process step agent (Agent Provisioning team). */
  listAgentEnvironments(teamId: string): Observable<AgentEnvProvisionSummary[]> {
    return this.http.get<AgentEnvProvisionSummary[]>(`${this.base}/teams/${teamId}/agent-environments`);
  }
}
