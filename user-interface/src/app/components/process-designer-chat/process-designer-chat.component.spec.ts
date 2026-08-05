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
  AgenticTeam,
  AgenticTeamAgent,
  ProcessDefinition,
  ProcessStep,
  RosterValidationResult,
} from '../../models';

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
});
