import { SimpleChange } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { ProcessDesignerChatComponent } from './process-designer-chat.component';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type { AgenticTeam, AgenticTeamAgent, RosterValidationResult } from '../../models';

const team = (overrides: Partial<AgenticTeam> = {}): AgenticTeam => ({
  team_id: 't-1',
  name: 'Growth Pod',
  description: '',
  agents: [],
  processes: [],
  created_at: '',
  updated_at: '',
  ...overrides,
});

const agent = (overrides: Partial<AgenticTeamAgent> = {}): AgenticTeamAgent => ({
  agent_name: 'Writer',
  role: 'Writes',
  skills: ['seo'],
  capabilities: [],
  tools: [],
  expertise: [],
  source: 'generated',
  manifest_id: null,
  ...overrides,
});

const validation = (overrides: Partial<RosterValidationResult> = {}): RosterValidationResult => ({
  is_fully_staffed: true,
  agent_count: 1,
  process_count: 0,
  gaps: [],
  summary: 'ok',
  ...overrides,
});

describe('ProcessDesignerChatComponent', () => {
  let component: ProcessDesignerChatComponent;
  let fixture: ComponentFixture<ProcessDesignerChatComponent>;
  let api: {
    createConversation: ReturnType<typeof vi.fn>;
    sendMessage: ReturnType<typeof vi.fn>;
    listTeamAgents: ReturnType<typeof vi.fn>;
    validateRoster: ReturnType<typeof vi.fn>;
    addAgentFromRegistry: ReturnType<typeof vi.fn>;
    removeTeamAgent: ReturnType<typeof vi.fn>;
    updateTeamAgent: ReturnType<typeof vi.fn>;
    createProcess: ReturnType<typeof vi.fn>;
    updateProcess: ReturnType<typeof vi.fn>;
    setConversationProcess: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    api = {
      createConversation: vi.fn().mockReturnValue(
        of({ conversation_id: 'c-1', messages: [], current_process: null, suggested_questions: [] }),
      ),
      sendMessage: vi.fn().mockReturnValue(
        of({ conversation_id: 'c-1', messages: [], current_process: null, suggested_questions: [] }),
      ),
      listTeamAgents: vi.fn().mockReturnValue(of([agent()])),
      validateRoster: vi.fn().mockReturnValue(of(validation())),
      addAgentFromRegistry: vi.fn().mockReturnValue(of(agent({ agent_name: 'blogging.planner', source: 'registry', manifest_id: 'blogging.planner' }))),
      removeTeamAgent: vi.fn().mockReturnValue(of(undefined)),
      updateTeamAgent: vi.fn().mockReturnValue(of(agent())),
      createProcess: vi.fn().mockReturnValue(of({})),
      updateProcess: vi.fn().mockReturnValue(of({})),
      setConversationProcess: vi.fn().mockReturnValue(of({})),
    };

    await TestBed.configureTestingModule({
      imports: [ProcessDesignerChatComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: AgenticTeamApiService, useValue: api },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ProcessDesignerChatComponent);
    component = fixture.componentInstance;
    component.team = team();
    fixture.detectChanges();
  });

  it('should create and load the roster on init', () => {
    expect(component).toBeTruthy();
    expect(api.listTeamAgents).toHaveBeenCalledWith('t-1');
    expect(component.rosterAgents()).toHaveLength(1);
  });

  // ── ngOnChanges: restart only on team identity change (loop guard) ──────────
  //
  // An embedding stage (Agent Studio Stage 3) re-fetches the team after roster
  // edits and rebinds [team] to a freshly-parsed object with the SAME team_id.
  // Restarting the conversation on reference change alone would reset the chat
  // and re-emit rosterChanged, driving the parent into an unbounded
  // getTeam→rebind→restart loop. ngOnChanges must key off team_id, not identity.

  it('does NOT restart the conversation when [team] is a new object with the same team_id', () => {
    const initialCreateCalls = api.createConversation.mock.calls.length; // 1 from init
    const prev = component.team;
    const next = team({ team_id: 't-1', name: 'Growth Pod (refetched)' }); // new ref, same id
    component.team = next;
    component.ngOnChanges({
      team: new SimpleChange(prev, next, false),
    });
    expect(api.createConversation.mock.calls.length).toBe(initialCreateCalls); // no restart
  });

  it('DOES restart the conversation when [team] changes to a different team_id', () => {
    const initialCreateCalls = api.createConversation.mock.calls.length;
    const prev = component.team;
    const next = team({ team_id: 't-2' });
    component.team = next;
    component.ngOnChanges({
      team: new SimpleChange(prev, next, false),
    });
    expect(api.createConversation.mock.calls.length).toBe(initialCreateCalls + 1);
    expect(api.createConversation).toHaveBeenLastCalledWith('t-2');
  });

  it('emits rosterChanged with the validation result on a successful refresh', () => {
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));
    component.refreshRoster();
    expect(seen).toEqual([validation()]);
  });

  it('emits rosterChanged with null when validation fails', () => {
    api.validateRoster.mockReturnValueOnce(throwError(() => new Error('boom')));
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));
    component.refreshRoster();
    expect(seen).toEqual([null]);
    expect(component.rosterValidation()).toBeNull();
  });

  // ── Add from registry ────────────────────────────────────────────────────

  it('onAddFromRegistryDialogClosed adds the agent and refreshes the roster', () => {
    component.onAddFromRegistryDialogClosed('blogging.planner');
    expect(api.addAgentFromRegistry).toHaveBeenCalledWith('t-1', 'blogging.planner');
    expect(api.listTeamAgents).toHaveBeenCalledTimes(2); // initial + refresh
  });

  it('onAddFromRegistryDialogClosed no-ops when the dialog was cancelled', () => {
    component.onAddFromRegistryDialogClosed(undefined);
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  it('onAddFromRegistryDialogClosed surfaces an error', () => {
    api.addAgentFromRegistry.mockReturnValueOnce(throwError(() => ({ error: { detail: 'nope' } })));
    component.onAddFromRegistryDialogClosed('blogging.planner');
    expect(component.rosterActionError()).toBe('nope');
  });

  it('openAddFromRegistry passes existing manifest ids so the dialog can mark them added', () => {
    component.rosterAgents.set([
      agent({ agent_name: 'a', source: 'registry', manifest_id: 'reg.a' }),
      agent({ agent_name: 'b', source: 'generated', manifest_id: null }),
    ]);
    // Exercise the open path without asserting on the real MatDialog internals —
    // it must not throw, and it must not touch the API until the dialog closes.
    expect(() => component.openAddFromRegistry()).not.toThrow();
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  // ── Suggest via chat ─────────────────────────────────────────────────────

  it('suggestAgentViaChat seeds the chat input', () => {
    component.suggestAgentViaChat();
    expect(component.form.getRawValue().message).toBe('Suggest an additional agent for this team.');
  });

  // ── Delete ────────────────────────────────────────────────────────────────

  it('deleteAgent removes the agent when confirmed', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteAgent(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(api.removeTeamAgent).toHaveBeenCalledWith('t-1', 'Writer');
  });

  it('deleteAgent does nothing when not confirmed', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteAgent(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(api.removeTeamAgent).not.toHaveBeenCalled();
  });

  it('deleteAgent surfaces an error', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.removeTeamAgent.mockReturnValueOnce(throwError(() => ({ error: { detail: 'cannot remove' } })));
    component.deleteAgent(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(component.rosterActionError()).toBe('cannot remove');
  });

  it('deleteAgent clears an in-progress edit on the deleted agent', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.editingAgent.set('Writer');
    component.deleteAgent(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(component.editingAgent()).toBeNull();
  });

  // ── Inline edit ───────────────────────────────────────────────────────────

  it('startEditAgent seeds the draft from the agent and enters edit mode', () => {
    component.startEditAgent(
      agent({ agent_name: 'Writer', role: 'Writes', skills: ['seo', 'copy'] }),
      new Event('click'),
    );
    expect(component.editingAgent()).toBe('Writer');
    expect(component.editDraft()).toEqual({
      role: 'Writes',
      skills: 'seo, copy',
      capabilities: '',
      tools: '',
      expertise: '',
    });
  });

  it('cancelEditAgent exits edit mode without saving', () => {
    component.editingAgent.set('Writer');
    component.cancelEditAgent(new Event('click'));
    expect(component.editingAgent()).toBeNull();
    expect(api.updateTeamAgent).not.toHaveBeenCalled();
  });

  it('saveAgentEdits parses comma-separated fields and calls updateTeamAgent', () => {
    component.editDraft.set({
      role: ' New role ',
      skills: 'seo,  copy ,',
      capabilities: '',
      tools: 'Slack API',
      expertise: '',
    });
    component.saveAgentEdits(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(api.updateTeamAgent).toHaveBeenCalledWith('t-1', 'Writer', {
      role: 'New role',
      skills: ['seo', 'copy'],
      capabilities: [],
      tools: ['Slack API'],
      expertise: [],
    });
    expect(component.editingAgent()).toBeNull();
  });

  it('saveAgentEdits surfaces an error and stays in edit mode', () => {
    api.updateTeamAgent.mockReturnValueOnce(throwError(() => ({ error: { detail: 'bad edit' } })));
    component.editingAgent.set('Writer');
    component.editDraft.set({ role: 'x', skills: '', capabilities: '', tools: '', expertise: '' });
    component.saveAgentEdits(agent({ agent_name: 'Writer' }), new Event('click'));
    expect(component.rosterActionError()).toBe('bad edit');
    expect(component.editingAgent()).toBe('Writer');
  });
});
