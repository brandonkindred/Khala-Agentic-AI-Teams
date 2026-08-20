import { Injectable, inject } from '@angular/core';
import { HttpResponse } from '@angular/common/http';
import { Observable, of, tap } from 'rxjs';
import { AgentStudioApiService } from './agent-studio-api.service';
import { AgentConsoleApiService } from './agent-console-api.service';
import { AgenticTeamApiService } from './agentic-team-api.service';
import { PersonaTestingApiService } from './persona-testing-api.service';
import { AgentStudioStateService } from './agent-studio-state.service';
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
 * (`AgentStudioApiService`, `AgentConsoleApiService`, `AgenticTeamApiService`,
 * `PersonaTestingApiService`), which remain available as implementation
 * details for call sites that still need them directly.
 *
 * Complementary to `AgentStudioStateService`, which owns in-session handoff
 * state (registry/team/persona/draft ids, the roster dedupe set) and stepper
 * position. Methods whose HTTP call produces a value the handoff state needs
 * write that state on success and leave it untouched on error — the error
 * itself is never intercepted, so it always reaches the caller unchanged.
 * Writes that are pure local UI-state resets with no backing HTTP call
 * (e.g. clearing the process/roster gates when a different team is selected)
 * stay the caller's responsibility; there is no response to hang them on.
 *
 * Provided at the Studio shell (not `root`), alongside
 * `AgentStudioStateService`, so both share one session-scoped injector.
 */
@Injectable()
export class AgentStudioFacade {
  private readonly studioApi = inject(AgentStudioApiService);
  private readonly runnerApi = inject(AgentConsoleApiService);
  private readonly agenticTeamApi = inject(AgenticTeamApiService);
  private readonly personaApi = inject(PersonaTestingApiService);
  private readonly state = inject(AgentStudioStateService);

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

  /**
   * Clone a registry agent into a Stage-1 draft. On success, stamps a fresh
   * client-generated `draftAgentId` — the draft has no server-issued id yet,
   * so this is not derived from the response.
   */
  selectAgent(agentId: string): Observable<AgentDefinition> {
    return this.studioApi.cloneFromRegistry(agentId).pipe(
      tap(() => this.state.setDraftAgentId(crypto.randomUUID())),
    );
  }

  /** Save + register the current draft. On success, its `agent_id` becomes `registryAgentId`. */
  saveAgent(req: SaveAgentRequest): Observable<SaveAgentResponse> {
    return this.studioApi.saveAgent(req).pipe(
      tap((response) => this.state.setRegistryAgentId(response.agent_id)),
    );
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

  /** Create a team. On success, its `team_id` becomes the journey's `teamId`. */
  composeTeam(req: CreateAgenticTeamRequest): Observable<CreateAgenticTeamResponse> {
    return this.agenticTeamApi.createTeam(req).pipe(
      tap((response) => this.state.setTeamId(response.team_id)),
    );
  }

  /**
   * Add a registry agent to a team's roster, enforcing the at-most-once
   * auto-add per `(teamId, manifestId)` this session. The dedupe key is
   * marked consumed on attempt, not on success, so a failed add is not
   * retried automatically — the user can still add the agent manually.
   * Returns `null` without calling the API if the key was already consumed,
   * or if `alreadyOnRoster` is true (the agent is already staffed — still
   * mark consumed so a later manual delete is never auto-undone).
   *
   * Preconditions: `teamId` and `manifestId` are non-empty strings.
   * Postconditions: the `(teamId, manifestId)` key is consumed when this
   *   session had not already consumed it; the API is called only when the
   *   key was fresh and `alreadyOnRoster` is false.
   */
  addAgentToTeam(
    teamId: string,
    manifestId: string,
    alreadyOnRoster = false,
  ): Observable<AgenticTeamAgent | null> {
    const key = `${teamId}::${manifestId}`;
    if (this.state.hasConsumedHandoff(key)) {
      return of(null);
    }
    this.state.markHandoffConsumed(key);
    if (alreadyOnRoster) {
      return of(null);
    }
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

  /** Create a persona. On success, it becomes the journey's `personaId`. */
  createPersona(payload: CreatePersonaRequest): Observable<PersonaInfo> {
    return this.personaApi.createPersona(payload).pipe(
      tap((created) => this.state.setPersonaId(created.id)),
    );
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
