import { Injectable, inject } from '@angular/core';
import { HttpResponse } from '@angular/common/http';
import { Observable } from 'rxjs';
import { AgentStudioApiService } from './agent-studio-api.service';
import { AgentRunnerApiService } from './agent-runner-api.service';
import { AgenticTeamApiService } from './agentic-team-api.service';
import { PersonaTestingApiService } from './persona-testing-api.service';
import type {
  AgentDefinition,
  AgentStudioDraft,
  AgentStudioDraftSummary,
  ConversationStateResponse,
  SaveAgentRequest,
  SaveAgentResponse,
  SaveDraftRequest,
  SendMessageRequest,
  StartConversationRequest,
} from '../models/agent-studio.model';
import type { InvokeEnvelope, SandboxHandle } from '../models/agent-runner.model';
import type {
  AgenticTeamAgent,
  AgenticTeamDetailResponse,
  AgenticTeamSummary,
  CreateAgenticTeamRequest,
  CreateAgenticTeamResponse,
  CreatePersonaRequest,
  PersonaInfo,
  PersonaTestRunDetail,
  StartTestRequest,
  TestPipelineRun,
} from '../models';

/**
 * Single integration surface for Agent Studio's Stage 1–4 happy-path data
 * operations. Delegates to the existing `*-api.service.ts` HTTP clients
 * (`AgentStudioApiService`, `AgentRunnerApiService`, `AgenticTeamApiService`,
 * `PersonaTestingApiService`), which remain available as implementation
 * details for call sites that still need them directly.
 *
 * Out of scope here: centralizing `AgentStudioStateService` (handoff) writes
 * — the facade returns raw responses and leaves state updates to callers.
 */
@Injectable({ providedIn: 'root' })
export class AgentStudioFacade {
  private readonly studioApi = inject(AgentStudioApiService);
  private readonly runnerApi = inject(AgentRunnerApiService);
  private readonly agenticTeamApi = inject(AgenticTeamApiService);
  private readonly personaApi = inject(PersonaTestingApiService);

  // -------------------------------------------------------------------------
  // Stage 1 — Build Agent
  // -------------------------------------------------------------------------

  startAgentConversation(req: StartConversationRequest): Observable<ConversationStateResponse> {
    return this.studioApi.startConversation(req);
  }

  sendAgentMessage(
    conversationId: string,
    req: SendMessageRequest,
  ): Observable<ConversationStateResponse> {
    return this.studioApi.sendMessage(conversationId, req);
  }

  selectAgent(agentId: string): Observable<AgentDefinition> {
    return this.studioApi.cloneFromRegistry(agentId);
  }

  saveAgent(req: SaveAgentRequest): Observable<SaveAgentResponse> {
    return this.studioApi.saveAgent(req);
  }

  /**
   * Save the in-progress build draft — updates an existing draft when
   * `draftId` is provided, otherwise creates a new one.
   */
  saveDraft(
    req: SaveDraftRequest,
    draftId?: string | null,
  ): Observable<AgentStudioDraftSummary> {
    return draftId ? this.studioApi.updateDraft(draftId, req) : this.studioApi.createDraft(req);
  }

  loadDraft(draftId: string): Observable<AgentStudioDraft> {
    return this.studioApi.getDraft(draftId);
  }

  listDrafts(limit?: number, offset?: number): Observable<AgentStudioDraftSummary[]> {
    return this.studioApi.listDrafts(limit, offset);
  }

  renameDraft(draftId: string, name: string): Observable<AgentStudioDraftSummary> {
    return this.studioApi.renameDraft(draftId, name);
  }

  deleteDraft(draftId: string): Observable<{ draft_id: string; status: string }> {
    return this.studioApi.deleteDraft(draftId);
  }

  // -------------------------------------------------------------------------
  // Stage 2 — Test Agent
  // -------------------------------------------------------------------------

  ensureAgentSandbox(agentId: string): Observable<SandboxHandle> {
    return this.runnerApi.ensureWarm(agentId);
  }

  invokeAgent(
    agentId: string,
    body: unknown,
    savedInputId?: string | null,
  ): Observable<HttpResponse<InvokeEnvelope | Record<string, unknown>>> {
    return this.runnerApi.invoke(agentId, body, savedInputId);
  }

  // -------------------------------------------------------------------------
  // Stage 3 — Compose Team
  // -------------------------------------------------------------------------

  listTeams(): Observable<AgenticTeamSummary[]> {
    return this.agenticTeamApi.listTeams();
  }

  /** Shared with Stage 4, which also loads the team it's testing. */
  getTeam(teamId: string): Observable<AgenticTeamDetailResponse> {
    return this.agenticTeamApi.getTeam(teamId);
  }

  composeTeam(req: CreateAgenticTeamRequest): Observable<CreateAgenticTeamResponse> {
    return this.agenticTeamApi.createTeam(req);
  }

  addAgentToTeam(teamId: string, manifestId: string): Observable<AgenticTeamAgent> {
    return this.agenticTeamApi.addAgentFromRegistry(teamId, manifestId);
  }

  // -------------------------------------------------------------------------
  // Stage 4 — Test Team w/ Personas
  // -------------------------------------------------------------------------

  getTeamPipelineRun(teamId: string, runId: string): Observable<TestPipelineRun> {
    return this.agenticTeamApi.getPipelineRun(teamId, runId);
  }

  listPersonas(): Observable<{ personas: PersonaInfo[] }> {
    return this.personaApi.getPersonas();
  }

  createPersona(payload: CreatePersonaRequest): Observable<PersonaInfo> {
    return this.personaApi.createPersona(payload);
  }

  startPersonaRun(
    payload: StartTestRequest,
  ): Observable<{ job_id: string; status: string; message: string }> {
    return this.personaApi.startTest(payload);
  }

  getPersonaRunStatus(runId: string): Observable<PersonaTestRunDetail> {
    return this.personaApi.getRunStatus(runId);
  }

  cancelPersonaRun(runId: string): Observable<unknown> {
    return this.personaApi.cancelJob(runId);
  }
}
