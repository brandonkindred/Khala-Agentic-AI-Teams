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
  AgentQualityScore,
  ProcessDefinition,
  RecommendAgentsResponse,
  RosterValidationResult,
  TeamMode,
  TestChatSession,
  TestChatSessionDetail,
  TestChatMessage,
  TestPipelineRun,
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

  /** Link a process to a conversation so chat stays in sync with the visual editor. */
  setConversationProcess(conversationId: string, processId: string): Observable<{ conversation_id: string; process_id: string }> {
    return this.http.put<{ conversation_id: string; process_id: string }>(
      `${this.base}/conversations/${conversationId}/process`,
      { process_id: processId },
    );
  }

  /** Create a new blank process for a team. */
  createProcess(teamId: string): Observable<ProcessDefinition> {
    return this.http.post<ProcessDefinition>(`${this.base}/teams/${teamId}/processes`, {});
  }

  /** Update a process definition (visual editor saves). */
  updateProcess(processId: string, process: ProcessDefinition): Observable<ProcessDefinition> {
    return this.http.put<ProcessDefinition>(`${this.base}/processes/${processId}`, process);
  }

  /** Get agent recommendations for a specific process step. */
  recommendAgentsForStep(processId: string, stepId: string): Observable<RecommendAgentsResponse> {
    return this.http.post<RecommendAgentsResponse>(
      `${this.base}/processes/${processId}/steps/${stepId}/recommend-agents`,
      {},
    );
  }

  /** Sandbox provisioning status for each process step agent (Agent Provisioning team). */
  listAgentEnvironments(teamId: string): Observable<AgentEnvProvisionSummary[]> {
    return this.http.get<AgentEnvProvisionSummary[]>(`${this.base}/teams/${teamId}/agent-environments`);
  }

  // -------------------------------------------------------------------------
  // Interactive Testing Mode
  // -------------------------------------------------------------------------

  setTeamMode(teamId: string, mode: TeamMode): Observable<{ team_id: string; mode: string }> {
    return this.http.put<{ team_id: string; mode: string }>(`${this.base}/teams/${teamId}/mode`, { mode });
  }

  // Agent Chat Testing

  createTestChatSession(teamId: string, agentName: string): Observable<TestChatSession> {
    return this.http.post<TestChatSession>(`${this.base}/teams/${teamId}/test-chat/sessions`, {
      agent_name: agentName,
    });
  }

  listTestChatSessions(teamId: string, agentName?: string): Observable<TestChatSession[]> {
    const params = agentName ? `?agent_name=${encodeURIComponent(agentName)}` : '';
    return this.http.get<TestChatSession[]>(`${this.base}/teams/${teamId}/test-chat/sessions${params}`);
  }

  getTestChatSession(teamId: string, sessionId: string): Observable<TestChatSessionDetail> {
    return this.http.get<TestChatSessionDetail>(
      `${this.base}/teams/${teamId}/test-chat/sessions/${sessionId}`,
    );
  }

  renameTestChatSession(
    teamId: string, sessionId: string, sessionName: string,
  ): Observable<{ session_id: string; session_name: string }> {
    return this.http.put<{ session_id: string; session_name: string }>(
      `${this.base}/teams/${teamId}/test-chat/sessions/${sessionId}/name`,
      { session_name: sessionName },
    );
  }

  deleteTestChatSession(teamId: string, sessionId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/teams/${teamId}/test-chat/sessions/${sessionId}`);
  }

  sendTestChatMessage(
    teamId: string, sessionId: string, content: string,
  ): Observable<{ session: TestChatSession; messages: TestChatMessage[] }> {
    return this.http.post<{ session: TestChatSession; messages: TestChatMessage[] }>(
      `${this.base}/teams/${teamId}/test-chat/sessions/${sessionId}/messages`,
      { content },
    );
  }

  exportTestChatSession(teamId: string, sessionId: string): Observable<Blob> {
    return this.http.get(`${this.base}/teams/${teamId}/test-chat/sessions/${sessionId}/export`, {
      responseType: 'blob',
    });
  }

  rateTestChatMessage(
    teamId: string, messageId: string, rating: string,
  ): Observable<{ message_id: string; rating: string }> {
    return this.http.put<{ message_id: string; rating: string }>(
      `${this.base}/teams/${teamId}/test-chat/messages/${messageId}/rating`,
      { rating },
    );
  }

  getAgentQualityScores(teamId: string): Observable<AgentQualityScore[]> {
    return this.http.get<AgentQualityScore[]>(`${this.base}/teams/${teamId}/test-chat/quality-scores`);
  }

  // Pipeline Testing

  startPipelineRun(teamId: string, processId: string, initialInput?: string): Observable<TestPipelineRun> {
    return this.http.post<TestPipelineRun>(`${this.base}/teams/${teamId}/test-pipeline/runs`, {
      process_id: processId,
      initial_input: initialInput ?? null,
    });
  }

  listPipelineRuns(teamId: string): Observable<TestPipelineRun[]> {
    return this.http.get<TestPipelineRun[]>(`${this.base}/teams/${teamId}/test-pipeline/runs`);
  }

  getPipelineRun(teamId: string, runId: string): Observable<TestPipelineRun> {
    return this.http.get<TestPipelineRun>(`${this.base}/teams/${teamId}/test-pipeline/runs/${runId}`);
  }

  submitPipelineInput(teamId: string, runId: string, inputText: string): Observable<TestPipelineRun> {
    return this.http.post<TestPipelineRun>(
      `${this.base}/teams/${teamId}/test-pipeline/runs/${runId}/input`,
      { input: inputText },
    );
  }

  cancelPipelineRun(teamId: string, runId: string): Observable<TestPipelineRun> {
    return this.http.post<TestPipelineRun>(
      `${this.base}/teams/${teamId}/test-pipeline/runs/${runId}/cancel`,
      {},
    );
  }
}
