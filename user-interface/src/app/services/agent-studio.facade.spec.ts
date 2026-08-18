import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioFacade } from './agent-studio.facade';
import { AgentStudioApiService } from './agent-studio-api.service';
import { AgentRunnerApiService } from './agent-runner-api.service';
import { AgenticTeamApiService } from './agentic-team-api.service';
import { PersonaTestingApiService } from './persona-testing-api.service';
import { AgentStudioStateService } from './agent-studio-state.service';

describe('AgentStudioFacade', () => {
  let facade: AgentStudioFacade;
  let studioApi: AgentStudioApiService;
  let runnerApi: AgentRunnerApiService;
  let agenticTeamApi: AgenticTeamApiService;
  let personaApi: PersonaTestingApiService;
  let state: {
    setRegistryAgentId: ReturnType<typeof vi.fn>;
    setTeamId: ReturnType<typeof vi.fn>;
    setDraftAgentId: ReturnType<typeof vi.fn>;
    setPersonaId: ReturnType<typeof vi.fn>;
    hasConsumedHandoff: ReturnType<typeof vi.fn>;
    markHandoffConsumed: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    const studioApiStub = {
      startConversation: vi.fn(() => of({ conversation_id: 'c1' })),
      sendMessage: vi.fn(() => of({ conversation_id: 'c1' })),
      cloneFromRegistry: vi.fn(() => of({ name: 'Cloned Agent' })),
      saveAgent: vi.fn(() => of({ agent_id: 'a1', created: true })),
      createDraft: vi.fn(() => of({ draft_id: 'd1' })),
      updateDraft: vi.fn(() => of({ draft_id: 'd1' })),
      listDrafts: vi.fn(() => of([])),
      getDraft: vi.fn(() => of({ draft_id: 'd1' })),
      renameDraft: vi.fn(() => of({ draft_id: 'd1', name: 'Renamed' })),
      deleteDraft: vi.fn(() => of({ draft_id: 'd1', status: 'deleted' })),
    };
    const runnerApiStub = {
      ensureWarm: vi.fn(() => of({ agent_id: 'a1', status: 'ready' })),
      invoke: vi.fn(() => of({ body: {} })),
    };
    const agenticTeamApiStub = {
      listTeams: vi.fn(() => of([])),
      getTeam: vi.fn(() => of({ team_id: 't1' })),
      createTeam: vi.fn(() => of({ team_id: 't1' })),
      addAgentFromRegistry: vi.fn(() => of({ name: 'agent-1' })),
      getPipelineRun: vi.fn(() => of({ run_id: 'r1' })),
    };
    const personaApiStub = {
      getPersonas: vi.fn(() => of({ personas: [] })),
      createPersona: vi.fn(() => of({ id: 'p1', name: 'Persona 1' })),
      startTest: vi.fn(() => of({ job_id: 'j1', status: 'started', message: 'ok' })),
      getRunStatus: vi.fn(() => of({ run_id: 'r1' })),
      cancelJob: vi.fn(() => of({})),
    };
    const stateStub = {
      setRegistryAgentId: vi.fn(),
      setTeamId: vi.fn(),
      setDraftAgentId: vi.fn(),
      setPersonaId: vi.fn(),
      hasConsumedHandoff: vi.fn(() => false),
      markHandoffConsumed: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        AgentStudioFacade,
        { provide: AgentStudioApiService, useValue: studioApiStub },
        { provide: AgentRunnerApiService, useValue: runnerApiStub },
        { provide: AgenticTeamApiService, useValue: agenticTeamApiStub },
        { provide: PersonaTestingApiService, useValue: personaApiStub },
        { provide: AgentStudioStateService, useValue: stateStub },
      ],
    });

    facade = TestBed.inject(AgentStudioFacade);
    studioApi = TestBed.inject(AgentStudioApiService);
    runnerApi = TestBed.inject(AgentRunnerApiService);
    agenticTeamApi = TestBed.inject(AgenticTeamApiService);
    personaApi = TestBed.inject(PersonaTestingApiService);
    state = TestBed.inject(AgentStudioStateService) as unknown as typeof state;
  });

  // ---------------------------------------------------------------------
  // Stage 1 — Build Agent
  // ---------------------------------------------------------------------

  it('starts an agent conversation via the studio api', () => {
    const req = { mode: 'new' as const };
    facade.startAgentConversation(req).subscribe();
    expect(studioApi.startConversation).toHaveBeenCalledWith(req);
  });

  it('sends an agent message via the studio api', () => {
    const req = { message: 'hello' };
    facade.sendAgentMessage('c1', req).subscribe();
    expect(studioApi.sendMessage).toHaveBeenCalledWith('c1', req);
  });

  it('selects (clones) an agent from the registry and stamps a fresh draftAgentId', () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('11111111-1111-1111-1111-111111111111');
    facade.selectAgent('blog/writer').subscribe();
    expect(studioApi.cloneFromRegistry).toHaveBeenCalledWith('blog/writer');
    expect(state.setDraftAgentId).toHaveBeenCalledWith('11111111-1111-1111-1111-111111111111');
  });

  it('does not stamp a draftAgentId when cloning an agent fails', () => {
    const error = new Error('clone failed');
    (studioApi.cloneFromRegistry as ReturnType<typeof vi.fn>).mockReturnValue(
      throwError(() => error),
    );
    let caught: unknown;
    facade.selectAgent('blog/writer').subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
    expect(state.setDraftAgentId).not.toHaveBeenCalled();
  });

  it('saves an agent and registers its agent_id', () => {
    const req = { name: 'My Agent' };
    facade.saveAgent(req).subscribe();
    expect(studioApi.saveAgent).toHaveBeenCalledWith(req);
    expect(state.setRegistryAgentId).toHaveBeenCalledWith('a1');
  });

  it('does not register a registryAgentId when saving an agent fails', () => {
    const error = new Error('save failed');
    (studioApi.saveAgent as ReturnType<typeof vi.fn>).mockReturnValue(throwError(() => error));
    let caught: unknown;
    facade.saveAgent({ name: 'My Agent' }).subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
    expect(state.setRegistryAgentId).not.toHaveBeenCalled();
  });

  it('saves a draft by creating one when no draftId is given', () => {
    const req = { name: 'Draft 1' };
    facade.saveDraft(req).subscribe();
    expect(studioApi.createDraft).toHaveBeenCalledWith(req);
    expect(studioApi.updateDraft).not.toHaveBeenCalled();
  });

  it('saves a draft by updating it when a draftId is given', () => {
    const req = { name: 'Renamed' };
    facade.saveDraft(req, 'd1').subscribe();
    expect(studioApi.updateDraft).toHaveBeenCalledWith('d1', req);
    expect(studioApi.createDraft).not.toHaveBeenCalled();
  });

  it('loads a draft', () => {
    facade.loadDraft('d1').subscribe();
    expect(studioApi.getDraft).toHaveBeenCalledWith('d1');
  });

  it('lists drafts, passing pagination through', () => {
    facade.listDrafts(10, 20).subscribe();
    expect(studioApi.listDrafts).toHaveBeenCalledWith(10, 20);
  });

  it('renames a draft', () => {
    facade.renameDraft('d1', 'New Name').subscribe();
    expect(studioApi.renameDraft).toHaveBeenCalledWith('d1', 'New Name');
  });

  it('deletes a draft', () => {
    facade.deleteDraft('d1').subscribe();
    expect(studioApi.deleteDraft).toHaveBeenCalledWith('d1');
  });

  // ---------------------------------------------------------------------
  // Stage 2 — Test Agent
  // ---------------------------------------------------------------------

  it('ensures an agent sandbox is warm', () => {
    facade.ensureAgentSandbox('a1').subscribe();
    expect(runnerApi.ensureWarm).toHaveBeenCalledWith('a1');
  });

  it('invokes an agent, passing the saved input id through', () => {
    const body = { foo: 'bar' };
    facade.invokeAgent('a1', body, 'saved-1').subscribe();
    expect(runnerApi.invoke).toHaveBeenCalledWith('a1', body, 'saved-1');
  });

  // ---------------------------------------------------------------------
  // Stage 3 — Compose Team
  // ---------------------------------------------------------------------

  it('lists teams', () => {
    facade.listTeams().subscribe();
    expect(agenticTeamApi.listTeams).toHaveBeenCalled();
  });

  it('gets a team', () => {
    facade.getTeam('t1').subscribe();
    expect(agenticTeamApi.getTeam).toHaveBeenCalledWith('t1');
  });

  it('composes (creates) a team and registers its team_id', () => {
    const req = { name: 'New Team', description: 'desc' };
    facade.composeTeam(req).subscribe();
    expect(agenticTeamApi.createTeam).toHaveBeenCalledWith(req);
    expect(state.setTeamId).toHaveBeenCalledWith('t1');
  });

  it('does not register a teamId when composing a team fails', () => {
    const error = new Error('compose failed');
    (agenticTeamApi.createTeam as ReturnType<typeof vi.fn>).mockReturnValue(throwError(() => error));
    let caught: unknown;
    facade
      .composeTeam({ name: 'New Team', description: 'desc' })
      .subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
    expect(state.setTeamId).not.toHaveBeenCalled();
  });

  it('adds a registry agent to a team and marks the handoff key consumed', () => {
    facade.addAgentToTeam('t1', 'blog/writer').subscribe();
    expect(state.markHandoffConsumed).toHaveBeenCalledWith('t1::blog/writer');
    expect(agenticTeamApi.addAgentFromRegistry).toHaveBeenCalledWith('t1', 'blog/writer');
  });

  it('marks the handoff key consumed on attempt even when the add fails, and propagates the error', () => {
    const error = new Error('add failed');
    (agenticTeamApi.addAgentFromRegistry as ReturnType<typeof vi.fn>).mockReturnValue(
      throwError(() => error),
    );
    let caught: unknown;
    facade.addAgentToTeam('t1', 'blog/writer').subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
    expect(state.markHandoffConsumed).toHaveBeenCalledWith('t1::blog/writer');
  });

  it('skips the API call and emits null when the handoff key was already consumed', () => {
    state.hasConsumedHandoff.mockReturnValue(true);
    let result: unknown;
    facade.addAgentToTeam('t1', 'blog/writer').subscribe((value) => (result = value));
    expect(result).toBeNull();
    expect(agenticTeamApi.addAgentFromRegistry).not.toHaveBeenCalled();
    expect(state.markHandoffConsumed).not.toHaveBeenCalled();
  });

  it('marks the handoff key consumed and skips the API when the agent is already on the roster', () => {
    let result: unknown;
    facade.addAgentToTeam('t1', 'blog/writer', true).subscribe((value) => (result = value));
    expect(result).toBeNull();
    expect(state.markHandoffConsumed).toHaveBeenCalledWith('t1::blog/writer');
    expect(agenticTeamApi.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  it('adds a registry agent from the Browse overlay without touching the handoff-consumed set', () => {
    facade.addAgentFromCatalog('t1', 'blog/writer').subscribe();
    expect(agenticTeamApi.addAgentFromRegistry).toHaveBeenCalledWith('t1', 'blog/writer');
    expect(state.markHandoffConsumed).not.toHaveBeenCalled();
    expect(state.hasConsumedHandoff).not.toHaveBeenCalled();
  });

  it('still calls the API for a manual add even when the pair was already handoff-consumed', () => {
    state.hasConsumedHandoff.mockReturnValue(true);
    facade.addAgentFromCatalog('t1', 'blog/writer').subscribe();
    expect(agenticTeamApi.addAgentFromRegistry).toHaveBeenCalledWith('t1', 'blog/writer');
  });

  it('propagates a failed manual add unchanged', () => {
    const error = new Error('add failed');
    (agenticTeamApi.addAgentFromRegistry as ReturnType<typeof vi.fn>).mockReturnValue(
      throwError(() => error),
    );
    let caught: unknown;
    facade.addAgentFromCatalog('t1', 'blog/writer').subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
  });

  // ---------------------------------------------------------------------
  // Stage 4 — Test Team w/ Personas
  // ---------------------------------------------------------------------

  it('gets a team pipeline run', () => {
    facade.getTeamPipelineRun('t1', 'r1').subscribe();
    expect(agenticTeamApi.getPipelineRun).toHaveBeenCalledWith('t1', 'r1');
  });

  it('lists personas', () => {
    facade.listPersonas().subscribe();
    expect(personaApi.getPersonas).toHaveBeenCalled();
  });

  it('creates a persona and registers its personaId', () => {
    const payload = {
      name: 'Persona 1',
      description: 'd',
      icon: 'bug_report',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    };
    facade.createPersona(payload).subscribe();
    expect(personaApi.createPersona).toHaveBeenCalledWith(payload);
    expect(state.setPersonaId).toHaveBeenCalledWith('p1');
  });

  it('does not register a personaId when creating a persona fails', () => {
    const error = new Error('create persona failed');
    (personaApi.createPersona as ReturnType<typeof vi.fn>).mockReturnValue(throwError(() => error));
    let caught: unknown;
    facade
      .createPersona({
        name: 'Persona 1',
        description: 'd',
        icon: 'bug_report',
        system_prompt: 's',
        spec_generation_prompt: 'g',
      })
      .subscribe({ error: (err) => (caught = err) });
    expect(caught).toBe(error);
    expect(state.setPersonaId).not.toHaveBeenCalled();
  });

  it('starts a persona run', () => {
    const payload = { persona_id: 'p1', target_team_key: 'agentic_team:t1', process_id: 'pr1' };
    facade.startPersonaRun(payload).subscribe();
    expect(personaApi.startTest).toHaveBeenCalledWith(payload);
  });

  it('gets a persona run status', () => {
    facade.getPersonaRunStatus('r1').subscribe();
    expect(personaApi.getRunStatus).toHaveBeenCalledWith('r1');
  });

  it('cancels a persona run', () => {
    facade.cancelPersonaRun('r1').subscribe();
    expect(personaApi.cancelJob).toHaveBeenCalledWith('r1');
  });
});
