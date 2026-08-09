import { SimpleChange } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { MatDialog } from '@angular/material/dialog';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { ProcessDesignerChatComponent } from './process-designer-chat.component';
import { AddAgentFromRegistryDialogComponent } from './add-agent-from-registry-dialog.component';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog/confirm-dialog.component';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type {
  AgenticConversationMessage,
  AgenticTeam,
  AgenticTeamAgent,
  ProcessDefinition,
  ProcessStep,
  RosterValidationResult,
} from '../../models';

interface ConversationStateResponse {
  conversation_id: string;
  messages: AgenticConversationMessage[];
  current_process: ProcessDefinition | null;
  suggested_questions: string[];
}

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

const step = (overrides: Partial<ProcessStep> = {}): ProcessStep => ({
  step_id: 's-1',
  name: 'Step 1',
  description: '',
  step_type: 'action',
  agents: [],
  next_steps: [],
  condition: null,
  ...overrides,
});

const process = (overrides: Partial<ProcessDefinition> = {}): ProcessDefinition => ({
  process_id: 'p-1',
  name: 'Process',
  description: 'A process',
  trigger: { trigger_type: 'manual', description: '' },
  steps: [step()],
  output: { description: '', destination: '' },
  status: 'draft',
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

  function createFlowchartFixture(proc: ProcessDefinition): ComponentFixture<ProcessDesignerChatComponent> {
    api.createConversation.mockReturnValueOnce(
      of({ conversation_id: 'c-flow', messages: [], current_process: proc, suggested_questions: [] }),
    );
    const flowFixture = TestBed.createComponent(ProcessDesignerChatComponent);
    flowFixture.componentInstance.team = team();
    flowFixture.detectChanges();
    return flowFixture;
  }

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

  // ── startConversation: sequence-token guard against out-of-order responses ──

  it('drops a stale createConversation response: an older call completing last cannot clobber the newer state', () => {
    const r1 = new Subject<ConversationStateResponse>();
    const r2 = new Subject<ConversationStateResponse>();
    api.createConversation.mockReturnValueOnce(r1.asObservable()).mockReturnValueOnce(r2.asObservable());

    component.newConversation(); // A -> subscribes r1
    component.newConversation(); // B -> subscribes r2 (now the latest)

    // B resolves first, then the stale A completes last.
    r2.next({ conversation_id: 'c-b', messages: [], current_process: null, suggested_questions: ['B?'] });
    r2.complete();
    r1.next({ conversation_id: 'c-a-stale', messages: [], current_process: null, suggested_questions: ['A (stale)?'] });
    r1.complete();

    // Only B's state survives; the late stale A is ignored.
    expect((component as unknown as { conversationId: string | null }).conversationId).toBe('c-b');
    expect(component.suggestedQuestions()).toEqual(['B?']);
  });

  it('drops a stale createConversation error: an older failure completing last cannot clobber the newer state', () => {
    const r1 = new Subject<ConversationStateResponse>();
    const r2 = new Subject<ConversationStateResponse>();
    api.createConversation.mockReturnValueOnce(r1.asObservable()).mockReturnValueOnce(r2.asObservable());

    component.newConversation(); // A -> subscribes r1
    component.newConversation(); // B -> subscribes r2 (now the latest)

    // B resolves successfully first, then the stale A errors last.
    r2.next({ conversation_id: 'c-b', messages: [], current_process: null, suggested_questions: [] });
    r2.complete();
    r1.error({ error: { detail: 'stale failure' } });

    // The stale error must not surface, since it belongs to a superseded call.
    expect(component.error()).toBeNull();
    expect((component as unknown as { conversationId: string | null }).conversationId).toBe('c-b');
  });

  it('emits rosterChanged with the validation result on a successful refresh', () => {
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));
    component.refreshRoster();
    expect(seen).toEqual([validation()]);
  });

  it('surfaces an error, stops loading, and clears the staffing gate when listTeamAgents fails', () => {
    // Seed a prior fully-staffed validation so we can prove the failed refresh
    // clears it (otherwise the embedding stage keeps "Test this team" enabled).
    component.rosterValidation.set(validation({ is_fully_staffed: true }));
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));

    api.listTeamAgents.mockReturnValueOnce(throwError(() => ({ error: { detail: 'boom' } })));
    component.refreshRoster();

    expect(component.rosterActionError()).toBe('boom');
    expect(component.rosterLoading()).toBe(false);
    // Gate cleared: validation dropped and rosterChanged emitted null.
    expect(component.rosterValidation()).toBeNull();
    expect(seen).toEqual([null]);
  });

  it('keeps rosterLoading true until validateRoster resolves', () => {
    const pendingValidation = new Subject<RosterValidationResult>();
    api.validateRoster.mockReturnValueOnce(pendingValidation.asObservable());
    component.refreshRoster();
    // listTeamAgents (of) resolved synchronously, but validation is still pending.
    expect(component.rosterLoading()).toBe(true);
    pendingValidation.next(validation());
    pendingValidation.complete();
    expect(component.rosterLoading()).toBe(false);
  });

  it('surfaces an error and clears the gate when validateRoster fails', () => {
    // The list load succeeds but validation blips. The gate must clear (so the
    // embedding stage drops "Test this team →") AND an error must surface, rather
    // than the forward button silently disabling with no explanation.
    component.rosterValidation.set(validation({ is_fully_staffed: true }));
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));

    api.validateRoster.mockReturnValueOnce(throwError(() => ({ error: { detail: 'nope' } })));
    component.refreshRoster();

    expect(component.rosterActionError()).toBe('nope');
    expect(component.rosterLoading()).toBe(false);
    expect(component.rosterValidation()).toBeNull();
    expect(seen).toEqual([null]);
  });

  it('drops a stale refresh: an older validateRoster completing last cannot clobber the newer result', () => {
    const v1 = new Subject<RosterValidationResult>();
    const v2 = new Subject<RosterValidationResult>();
    // listTeamAgents resolves synchronously (of), so both refreshes reach their
    // (pending) validateRoster; we control completion order.
    api.validateRoster.mockReturnValueOnce(v1.asObservable()).mockReturnValueOnce(v2.asObservable());
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));

    component.refreshRoster(); // A → subscribes v1
    component.refreshRoster(); // B → subscribes v2 (now the latest)

    // B resolves first, then the stale A completes last.
    v2.next(validation({ is_fully_staffed: false, summary: 'B' }));
    v2.complete();
    v1.next(validation({ is_fully_staffed: true, summary: 'A (stale)' }));
    v1.complete();

    // Only B was applied/emitted; the late stale A is ignored.
    expect(component.rosterValidation()?.summary).toBe('B');
    expect(seen).toEqual([expect.objectContaining({ summary: 'B' })]);
  });

  it('emits rosterChanged with null when validation fails', () => {
    api.validateRoster.mockReturnValueOnce(throwError(() => new Error('boom')));
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));
    component.refreshRoster();
    expect(seen).toEqual([null]);
    expect(component.rosterValidation()).toBeNull();
  });

  // ── ngOnDestroy: in-flight subscriptions must not touch a destroyed component ─

  it('ngOnDestroy stops a late createConversation response from updating a destroyed component', () => {
    const pending = new Subject<ConversationStateResponse>();
    api.createConversation.mockReturnValueOnce(pending.asObservable());

    component.newConversation(); // subscribes to `pending`, still in flight
    const conversationIdBefore = (component as unknown as { conversationId: string | null })
      .conversationId;
    const messagesBefore = component.messages();

    fixture.destroy(); // runs real Angular teardown, invoking ngOnDestroy()

    // The response arrives after teardown; without the takeUntil guard this would
    // still run applyState() against the destroyed component.
    expect(() =>
      pending.next({
        conversation_id: 'late',
        messages: [{ role: 'assistant', content: 'late reply', timestamp: '2024-01-01T00:00:00Z' }],
        current_process: null,
        suggested_questions: [],
      }),
    ).not.toThrow();

    expect((component as unknown as { conversationId: string | null }).conversationId).toBe(
      conversationIdBefore,
    );
    expect(component.messages()).toEqual(messagesBefore);
  });

  it('ngOnDestroy stops a late nested validateRoster response from updating a destroyed component', () => {
    const pendingValidation = new Subject<RosterValidationResult>();
    api.validateRoster.mockReturnValueOnce(pendingValidation.asObservable());
    const seen: (RosterValidationResult | null)[] = [];
    component.rosterChanged.subscribe((v) => seen.push(v));

    // listTeamAgents (of) resolves synchronously, subscribing the nested
    // validateRoster call, which is left pending on `pendingValidation`.
    component.refreshRoster();
    const validationBefore = component.rosterValidation();
    const loadingBefore = component.rosterLoading();

    fixture.destroy();

    expect(() => pendingValidation.next(validation({ summary: 'late' }))).not.toThrow();

    expect(component.rosterValidation()).toBe(validationBefore);
    expect(component.rosterLoading()).toBe(loadingBefore);
    expect(seen).toEqual([]); // rosterChanged never emitted — the late result was dropped
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

  it('openAddFromRegistry opens the dialog with the roster registry manifest ids', () => {
    component.rosterAgents.set([
      agent({ agent_name: 'a', source: 'registry', manifest_id: 'reg.a' }),
      agent({ agent_name: 'b', source: 'generated', manifest_id: null }),
    ]);
    // Spy on the component's OWN injected MatDialog: standalone components that
    // import MatDialogModule register it at their environment injector, so it's a
    // different instance than TestBed.inject(MatDialog).
    const dialogSpy = vi
      .spyOn((component as unknown as { dialog: MatDialog }).dialog, 'open')
      .mockReturnValue({ afterClosed: () => of(undefined) } as never);

    component.openAddFromRegistry();

    // Only the registry-source agent's manifest id is forwarded (generated has none).
    expect(dialogSpy).toHaveBeenCalledWith(AddAgentFromRegistryDialogComponent, {
      data: { existingManifestIds: ['reg.a'] },
      width: '480px',
    });
    // No API call until a manifest id is chosen.
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  // ── Suggest via chat ─────────────────────────────────────────────────────

  it('suggestAgentViaChat seeds the chat input', () => {
    component.suggestAgentViaChat();
    expect(component.form.getRawValue().message).toBe('Suggest an additional agent for this team.');
  });

  // ── Delete (Material confirm dialog) ────────────────────────────────────────

  it('deleteAgent opens a danger confirm dialog naming the agent', () => {
    const dialogSpy = vi
      .spyOn((component as unknown as { dialog: MatDialog }).dialog, 'open')
      .mockReturnValue({ afterClosed: () => of(false) } as never);

    component.deleteAgent(agent({ agent_name: 'Writer' }), new Event('click'));

    expect(dialogSpy).toHaveBeenCalledWith(
      ConfirmDialogComponent,
      expect.objectContaining({
        data: expect.objectContaining({
          message: 'Remove "Writer" from the roster?',
          confirmLabel: 'Remove',
          variant: 'danger',
        }),
      }),
    );
    // Cancelled (afterClosed → false): no removal.
    expect(api.removeTeamAgent).not.toHaveBeenCalled();
  });

  it('onDeleteAgentConfirmed removes the agent when confirmed', () => {
    component.onDeleteAgentConfirmed(agent({ agent_name: 'Writer' }), true);
    expect(api.removeTeamAgent).toHaveBeenCalledWith('t-1', 'Writer');
  });

  it('onDeleteAgentConfirmed does nothing when cancelled', () => {
    component.onDeleteAgentConfirmed(agent({ agent_name: 'Writer' }), false);
    expect(api.removeTeamAgent).not.toHaveBeenCalled();
  });

  it('onDeleteAgentConfirmed surfaces an error', () => {
    api.removeTeamAgent.mockReturnValueOnce(throwError(() => ({ error: { detail: 'cannot remove' } })));
    component.onDeleteAgentConfirmed(agent({ agent_name: 'Writer' }), true);
    expect(component.rosterActionError()).toBe('cannot remove');
  });

  it('onDeleteAgentConfirmed clears an in-progress edit on the deleted agent', () => {
    component.editingAgent.set('Writer');
    component.onDeleteAgentConfirmed(agent({ agent_name: 'Writer' }), true);
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

  it('saveAgentEdits sends only the fields changed since the edit form opened', () => {
    const a = agent({
      agent_name: 'Writer',
      role: 'Writes',
      skills: ['seo'],
      capabilities: ['gen'],
      tools: ['Git'],
      expertise: ['x'],
    });
    component.startEditAgent(a, new Event('click'));
    // The user edits only the role; every other field is left as the form opened.
    // A full-object save would clobber skills/etc. the chat may have refreshed
    // meanwhile, so only the touched field must be sent (backend PUT is partial).
    component.updateEditDraftField('role', 'Lead writer');
    component.saveAgentEdits(a, new Event('click'));
    expect(api.updateTeamAgent).toHaveBeenCalledWith('t-1', 'Writer', { role: 'Lead writer' });
    expect(component.editingAgent()).toBeNull();
  });

  it('saveAgentEdits skips the request entirely when nothing changed', () => {
    const a = agent({ agent_name: 'Writer', role: 'Writes', skills: ['seo'] });
    component.startEditAgent(a, new Event('click'));
    component.saveAgentEdits(a, new Event('click'));
    expect(api.updateTeamAgent).not.toHaveBeenCalled();
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

  // ── sendMessage: optimistic append must reconcile with backend atomicity ────
  //
  // The backend (agentic_team_provisioning's send_message) persists a turn's
  // messages only after the LLM call and roster/process save succeed; on
  // failure nothing is saved. The UI optimistically appends the user's message
  // before the API call, so a failed send must roll that optimistic message
  // back — otherwise it stays visible until a refresh silently drops it.

  it('appends the user message and the assistant reply on a successful send', () => {
    api.sendMessage.mockReturnValueOnce(
      of({
        conversation_id: 'c-1',
        messages: [
          { role: 'user', content: 'hi', timestamp: '2024-01-01T00:00:00Z' },
          { role: 'assistant', content: 'hello', timestamp: '2024-01-01T00:00:01Z' },
        ],
        current_process: null,
        suggested_questions: [],
      }),
    );

    component.form.setValue({ message: 'hi' });
    component.onSubmit();

    expect(api.sendMessage).toHaveBeenCalledWith('c-1', 'hi');
    expect(component.messages().map((m) => m.content)).toEqual(['hi', 'hello']);
    expect(component.error()).toBeNull();
  });

  it('rolls back the optimistic user message when the send fails', () => {
    const send$ = new Subject<unknown>();
    api.sendMessage.mockReturnValueOnce(send$.asObservable() as never);

    component.form.setValue({ message: 'hi' });
    component.onSubmit();

    // Before the API responds, the optimistic message must already be visible —
    // otherwise the later empty-array assertion can't distinguish a real rollback
    // from an implementation that never appended anything in the first place.
    expect(component.messages().map((m) => m.content)).toEqual(['hi']);

    send$.error({
      error: { detail: 'Failed to update the team roster or process; please try again.' },
    });

    expect(component.messages()).toHaveLength(0);
    expect(component.error()).toBe(
      'Failed to update the team roster or process; please try again.',
    );
  });

  it('ignores a suggested-question click while a send is already in flight', () => {
    const send$ = new Subject<unknown>();
    api.sendMessage.mockReturnValueOnce(send$.asObservable() as never);

    component.form.setValue({ message: 'hi' });
    component.onSubmit(); // first send now in flight

    component.onSuggestedQuestion('another question'); // must be ignored

    expect(api.sendMessage).toHaveBeenCalledTimes(1);
    expect(component.messages().map((m) => m.content)).toEqual(['hi']);
  });

  // ── createNewProcess: create + link to the active conversation ─────────────

  it('createNewProcess creates the process and links it to the active conversation', () => {
    api.createProcess.mockReturnValueOnce(of(process({ process_id: 'p-new' })));
    component.createNewProcess();

    expect(component.currentProcess()?.process_id).toBe('p-new');
    expect(component.saving()).toBe(false);
    expect(api.setConversationProcess).toHaveBeenCalledWith('c-1', 'p-new');
    expect(component.error()).toBeNull();
  });

  it('createNewProcess surfaces an error when linking the process to the conversation fails', () => {
    api.createProcess.mockReturnValueOnce(of(process({ process_id: 'p-new' })));
    api.setConversationProcess.mockReturnValueOnce(
      throwError(() => ({ error: { detail: 'link failed' } })),
    );

    component.createNewProcess();

    // The link failure is surfaced, but the already-created process is kept —
    // it was not rolled back, matching the unchanged happy-path create step.
    expect(component.error()).toBe('link failed');
    expect(component.currentProcess()?.process_id).toBe('p-new');
  });

  it('createNewProcess falls back to the error message when the link failure has no detail field', () => {
    // Regression test for the link-failure handler: it must go through
    // extractErrorDetail (which falls back to err.message) rather than a raw
    // err?.error?.detail chain, which would report the generic fallback text
    // instead of this network-style error's message.
    api.createProcess.mockReturnValueOnce(of(process({ process_id: 'p-new' })));
    api.setConversationProcess.mockReturnValueOnce(throwError(() => ({ message: 'network down' })));

    component.createNewProcess();

    expect(component.error()).toBe('network down');
  });

  it('createNewProcess falls back to the generic message when detail is a non-string 422 shape', () => {
    api.createProcess.mockReturnValueOnce(of(process({ process_id: 'p-new' })));
    api.setConversationProcess.mockReturnValueOnce(
      throwError(() => ({ error: { detail: [{ msg: 'field required' }] } })),
    );

    component.createNewProcess();

    // Regression for #5172: the old chain treated a truthy non-string `detail`
    // (e.g. FastAPI's 422 validation-error array) as the message itself instead
    // of falling through to a readable fallback string.
    expect(component.error()).toBe('Failed to link process to conversation');
  });

  // ── Process CRUD: roll back optimistic mutations on save failure ───────────

  it('addStep rolls back the new step when updateProcess fails', () => {
    const original = process({ steps: [step({ step_id: 's-1' })] });
    component.currentProcess.set(original);

    api.updateProcess.mockReturnValueOnce(throwError(() => ({ error: { detail: 'save failed' } })));
    component.addStep('action');

    expect(component.currentProcess()?.steps).toEqual([step({ step_id: 's-1' })]);
    expect(component.error()).toBe('save failed');
  });

  it('addStep keeps the optimistic step when updateProcess succeeds', () => {
    const original = process({ steps: [step({ step_id: 's-1' })] });
    component.currentProcess.set(original);

    component.addStep('action');

    expect(component.currentProcess()?.steps.length).toBe(2);
    expect(component.error()).toBeNull();
  });

  it('does not leak the step counter across component instances', () => {
    // Regression guard: the step counter used to be module-scope (`let _stepCounter`),
    // shared by every component instance. It must now be a per-instance field.
    const original = process({ steps: [step({ step_id: 's-1' })] });
    component.currentProcess.set(original);
    component.addStep('action');
    component.addStep('action');
    expect((component as unknown as { _stepCounter: number })._stepCounter).toBe(2);

    const fixture2 = TestBed.createComponent(ProcessDesignerChatComponent);
    const component2 = fixture2.componentInstance;
    component2.team = team();
    fixture2.detectChanges();

    expect((component2 as unknown as { _stepCounter: number })._stepCounter).toBe(0);
  });

  it('onStepUpdated rolls back the edit when updateProcess fails', () => {
    const original = process({ steps: [step({ step_id: 's-1', name: 'Original name' })] });
    component.currentProcess.set(original);

    api.updateProcess.mockReturnValueOnce(throwError(() => ({ error: { detail: 'save failed' } })));
    component.onStepUpdated(step({ step_id: 's-1', name: 'Edited name' }));

    expect(component.currentProcess()?.steps).toEqual([step({ step_id: 's-1', name: 'Original name' })]);
    expect(component.error()).toBe('save failed');
  });

  it('onStepDeleted restores the deleted step when updateProcess fails', () => {
    const original = process({
      steps: [step({ step_id: 's-1', next_steps: ['s-2'] }), step({ step_id: 's-2' })],
    });
    component.currentProcess.set(original);

    api.updateProcess.mockReturnValueOnce(throwError(() => ({ error: { detail: 'save failed' } })));
    component.onStepDeleted('s-2');

    expect(component.currentProcess()?.steps).toEqual(original.steps);
    expect(component.error()).toBe('save failed');
  });

  it('saveProcessMeta rolls back name/description when updateProcess fails', () => {
    const original = process({ name: 'Original', description: 'Original desc' });
    component.currentProcess.set(original);
    component.processNameEdit.set('Edited');
    component.processDescEdit.set('Edited desc');

    api.updateProcess.mockReturnValueOnce(throwError(() => ({ error: { detail: 'save failed' } })));
    component.saveProcessMeta();

    expect(component.currentProcess()?.name).toBe('Original');
    expect(component.currentProcess()?.description).toBe('Original desc');
    expect(component.error()).toBe('save failed');
  });

  // ── Flowchart click delegation ────────────────────────────────────────────
  //
  // The flowchart SVG is injected via [innerHTML], so Angular can't bind
  // (click)/(keydown) to its individual nodes. Instead single (click) and
  // (keydown) listeners on the container (declared in the template) walk up
  // to the closest [data-step-id] ancestor of the event target. Because the
  // listeners live on the stable container rather than the replaced SVG
  // children, they need no manual rebinding or cleanup when buildFlowchart
  // replaces the DOM. Each generated node carries tabindex="0" so it's
  // reachable by keyboard, with Enter/Space mirroring a click.

  describe('flowchart click delegation', () => {
    it('clicking a rendered node invokes onStepClick', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const node = flowFixture.nativeElement.querySelector('[data-step-id]') as HTMLElement;
      expect(node).toBeTruthy();

      node.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(onStepClickSpy).toHaveBeenCalledWith('s-1');
    });

    it('clicking a descendant of a node (its label text) still resolves the step id', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const node = flowFixture.nativeElement.querySelector('[data-step-id]') as HTMLElement;
      const descendant = node.querySelector('text') as HTMLElement;
      expect(descendant).toBeTruthy();

      descendant.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(onStepClickSpy).toHaveBeenCalledWith('s-1');
    });

    it('clicking the container background does not invoke onStepClick', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;
      expect(container).toBeTruthy();

      container.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(onStepClickSpy).not.toHaveBeenCalled();
    });

    it('the single container listener still handles clicks after the flowchart is rebuilt', () => {
      const proc = process({
        steps: [step({ step_id: 's-1', next_steps: ['s-2'] }), step({ step_id: 's-2' })],
      });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      // Triggers buildFlowchart, which fully replaces the injected SVG DOM.
      flowComponent.onStepDeleted('s-2');
      flowFixture.detectChanges();

      const nodeAfter = flowFixture.nativeElement.querySelector('[data-step-id="s-1"]') as HTMLElement;
      expect(nodeAfter).toBeTruthy();

      nodeAfter.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(onStepClickSpy).toHaveBeenCalledWith('s-1');
    });

    it('pressing Enter on a focused node invokes onStepClick (keyboard activation)', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const node = flowFixture.nativeElement.querySelector('[data-step-id]') as HTMLElement;
      expect(node.getAttribute('tabindex')).toBe('0');

      node.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

      expect(onStepClickSpy).toHaveBeenCalledWith('s-1');
    });

    it('pressing Space on a focused node invokes onStepClick and prevents page scroll', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const node = flowFixture.nativeElement.querySelector('[data-step-id]') as HTMLElement;
      const spaceEvent = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      node.dispatchEvent(spaceEvent);

      expect(onStepClickSpy).toHaveBeenCalledWith('s-1');
      expect(spaceEvent.defaultPrevented).toBe(true);
    });

    it('pressing an unrelated key does not invoke onStepClick', () => {
      const proc = process({ steps: [step({ step_id: 's-1' })] });
      const flowFixture = createFlowchartFixture(proc);
      const flowComponent = flowFixture.componentInstance;
      const onStepClickSpy = vi.spyOn(flowComponent, 'onStepClick');

      const node = flowFixture.nativeElement.querySelector('[data-step-id]') as HTMLElement;
      node.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));

      expect(onStepClickSpy).not.toHaveBeenCalled();
    });
  });

  // buildFlowchart string-templates raw SVG and trusts the result via
  // sanitizer.bypassSecurityTrustHtml (rendered through [innerHTML] in the
  // template). trigger.description, step.name, agent_name, and
  // output.description can all originate from LLM-generated content seeded
  // by untrusted chat input, so every one of them must be passed through
  // escSvg before interpolation.
  describe('flowchart SVG escaping (XSS hardening)', () => {
    it('escapes a <script> payload in the trigger description', () => {
      const payload = '<script>window.__pwned = true;</script>';
      const proc = process({
        trigger: { trigger_type: 'manual', description: payload },
        steps: [step({ step_id: 's-1' })],
      });
      const flowFixture = createFlowchartFixture(proc);
      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;

      expect(container).toBeTruthy();
      expect(container.innerHTML).not.toContain('<script');
      expect(container.innerHTML).toContain('&lt;script');
    });

    it('escapes a quote-breakout payload in a step name', () => {
      const payload = '"><img src=x onerror=alert(1)>';
      const proc = process({ steps: [step({ step_id: 's-1', name: payload })] });
      const flowFixture = createFlowchartFixture(proc);
      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;

      expect(container.innerHTML).not.toContain('<img');
      // Browsers only re-escape &, <, > when serializing text-node content
      // back to innerHTML — quotes stay literal outside of attribute values.
      expect(container.innerHTML).toContain('"&gt;&lt;img');
    });

    it('escapes an XSS payload in an agent name', () => {
      const payload = '<img src=x onerror=alert(1)>';
      const proc = process({
        steps: [step({ step_id: 's-1', agents: [agent({ agent_name: payload })] })],
      });
      const flowFixture = createFlowchartFixture(proc);
      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;

      expect(container.innerHTML).not.toContain('<img');
      expect(container.innerHTML).toContain('&lt;img src=x onerror=alert(1)&gt;');
    });

    it('escapes an XSS payload in the output description', () => {
      const payload = '<svg onload=alert(1)>';
      const proc = process({
        steps: [step({ step_id: 's-1' })],
        output: { description: payload, destination: '' },
      });
      const flowFixture = createFlowchartFixture(proc);
      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;

      expect(container.innerHTML).not.toContain('<svg onload');
      expect(container.innerHTML).toContain('&lt;svg onload=alert(1)&gt;');
    });

    it('escapes a short marker in every known dynamic field in a single render (regression guard)', () => {
      // Broader guard: renders all four currently-escaped dynamic fields at
      // once with distinct markers, so a regression in ANY of them (e.g. an
      // escSvg() wrapper accidentally dropped in a future refactor) fails
      // this one test. Markers are kept short to stay under each field's
      // truncate() limit (trigger: 20, step name: 22, agent label: 28,
      // output: 22) so the full closing '>' survives truncation.
      const proc = process({
        trigger: { trigger_type: 'manual', description: '<xss-trig>' },
        steps: [
          step({
            step_id: 's-1',
            name: '<xss-step>',
            agents: [agent({ agent_name: '<xss-agent>' })],
          }),
        ],
        output: { description: '<xss-out>', destination: '' },
      });
      const flowFixture = createFlowchartFixture(proc);
      const container = flowFixture.nativeElement.querySelector('.flowchart-container') as HTMLElement;
      const raw = container.innerHTML;

      expect(raw).not.toContain('<xss-');
      expect(raw).toContain('&lt;xss-trig&gt;');
      expect(raw).toContain('&lt;xss-step&gt;');
      expect(raw).toContain('&lt;xss-agent&gt;');
      expect(raw).toContain('&lt;xss-out&gt;');
    });
  });

  describe('escSvg', () => {
    it('escapes &, <, >, ", and \' (defense-in-depth for future attribute changes)', () => {
      const raw = `&<>"'`;
      const escaped = (component as unknown as { escSvg(t: string): string }).escSvg(raw);
      expect(escaped).toBe('&amp;&lt;&gt;&quot;&#39;');
    });
  });

  // ── auto-scroll: only scroll when a message is added while near the bottom ──
  //
  // ngAfterViewChecked used to force scrollTop = scrollHeight unconditionally on
  // every change-detection cycle, yanking the view back down even when the user
  // had deliberately scrolled up to read earlier messages. It must now only
  // scroll when a message was actually added (via sendMessage/applyState) and
  // the user was already near the bottom beforehand.

  describe('auto-scroll on new messages', () => {
    function mockScrollMetrics(
      el: HTMLElement,
      { scrollHeight, clientHeight, scrollTop }: { scrollHeight: number; clientHeight: number; scrollTop: number },
    ): void {
      Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
      Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
      Object.defineProperty(el, 'scrollTop', { value: scrollTop, writable: true, configurable: true });
    }

    it('does not scroll on a view-checked cycle with no new message', () => {
      const el = component.messagesContainer.nativeElement;
      mockScrollMetrics(el, { scrollHeight: 1000, clientHeight: 200, scrollTop: 150 });

      component.ngAfterViewChecked(); // no message added since init; must be a no-op

      expect(el.scrollTop).toBe(150);
    });

    it('scrolls to bottom when a message is sent while the user is near the bottom', () => {
      const el = component.messagesContainer.nativeElement;
      // 10px from the bottom — within the "near bottom" threshold.
      mockScrollMetrics(el, { scrollHeight: 500, clientHeight: 200, scrollTop: 290 });

      component.form.setValue({ message: 'hi' });
      component.onSubmit(); // optimistic append + synchronous mocked response
      component.ngAfterViewChecked();

      expect(el.scrollTop).toBe(500);
    });

    it('does not force-scroll when a message is sent while the user has scrolled up to read history', () => {
      const el = component.messagesContainer.nativeElement;
      // Far from the bottom.
      mockScrollMetrics(el, { scrollHeight: 1000, clientHeight: 200, scrollTop: 0 });

      component.form.setValue({ message: 'hi' });
      component.onSubmit();
      component.ngAfterViewChecked();

      expect(el.scrollTop).toBe(0);
    });
  });
});
