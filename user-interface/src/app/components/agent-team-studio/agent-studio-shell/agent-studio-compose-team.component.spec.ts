import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import { ProcessDesignerChatComponent } from '../../process-designer-chat/process-designer-chat.component';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgenticTeamSummary,
  ProcessDefinition,
  RosterValidationResult,
} from '../../../models';

/** Stub the heavy embedded chat/roster panel; tests exercise `onRosterChanged`
 *  directly rather than through the stub's (unused) output emissions. */
@Component({ selector: 'app-process-designer-chat', standalone: true, template: '' })
class StubProcessDesignerChatComponent {
  @Input() team!: AgenticTeam;
  @Output() readonly rosterChanged = new EventEmitter<RosterValidationResult | null>();
}

const teamSummary = (id: string, name = id): AgenticTeamSummary => ({
  team_id: id,
  name,
  description: '',
  process_count: 0,
  created_at: '',
  updated_at: '',
});

const process = (overrides: Partial<ProcessDefinition> = {}): ProcessDefinition => ({
  process_id: 'p-1',
  name: 'Onboarding',
  description: '',
  trigger: { trigger_type: 'manual', description: '' },
  steps: [],
  output: { description: '', destination: '' },
  status: 'draft',
  ...overrides,
});

const agent = (overrides: Partial<AgenticTeamAgent> = {}): AgenticTeamAgent => ({
  agent_name: 'Planner',
  role: 'Plans',
  skills: [],
  capabilities: [],
  tools: [],
  expertise: [],
  source: 'registry',
  manifest_id: 'blogging.planner',
  ...overrides,
});

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

describe('AgentStudioComposeTeamComponent', () => {
  let fixture: ComponentFixture<AgentStudioComposeTeamComponent>;
  let component: AgentStudioComposeTeamComponent;
  let state: AgentStudioStateService;
  let api: {
    listTeams: ReturnType<typeof vi.fn>;
    getTeam: ReturnType<typeof vi.fn>;
    createTeam: ReturnType<typeof vi.fn>;
    addAgentFromRegistry: ReturnType<typeof vi.fn>;
  };

  function configure(): void {
    TestBed.configureTestingModule({
      imports: [AgentStudioComposeTeamComponent],
      providers: [
        AgentStudioStateService,
        { provide: AgenticTeamApiService, useValue: api },
      ],
    })
      .overrideComponent(AgentStudioComposeTeamComponent, {
        remove: { imports: [ProcessDesignerChatComponent] },
        add: { imports: [StubProcessDesignerChatComponent] },
      });

    fixture = TestBed.createComponent(AgentStudioComposeTeamComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
  }

  beforeEach(() => {
    api = {
      listTeams: vi.fn().mockReturnValue(of([teamSummary('t-1'), teamSummary('t-2')])),
      getTeam: vi.fn().mockReturnValue(of({ team: team() })),
      createTeam: vi.fn().mockReturnValue(of({ team_id: 't-new', name: 'New', description: '', created_at: '' })),
      addAgentFromRegistry: vi.fn().mockReturnValue(of(agent())),
    };
    configure();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('loads the team list on init', () => {
    fixture.detectChanges();
    expect(api.listTeams).toHaveBeenCalled();
    expect(component.teams()).toHaveLength(2);
  });

  it('loads the already-selected team on init (returning from Stage 4 iterateRoster)', () => {
    state.setTeamId('t-1');
    fixture.detectChanges();
    expect(api.getTeam).toHaveBeenCalledWith('t-1');
    expect(component.team()?.team_id).toBe('t-1');
  });

  it('does not load a team on init when none is selected yet', () => {
    fixture.detectChanges();
    expect(api.getTeam).not.toHaveBeenCalled();
  });

  it('surfaces a teams-list load error', () => {
    api.listTeams.mockReturnValueOnce(throwError(() => ({ error: { detail: 'down' } })));
    fixture.detectChanges();
    expect(component.teamsError()).toBe('down');
  });

  it('selectTeam records the id, clears prior gate state, and loads the team', () => {
    fixture.detectChanges();
    state.setRosterFullyStaffed(true);
    state.setComposeProcessStatus('complete');

    component.selectTeam('t-1');
    expect(state.teamId()).toBe('t-1');
    expect(state.rosterFullyStaffed()).toBe(false);
    expect(state.composeProcessStatus()).toBeNull();
    expect(api.getTeam).toHaveBeenCalledWith('t-1');
  });

  it('switching teams rapidly cannot apply a stale earlier response (switchMap)', () => {
    // First team's fetch is held pending on a Subject; the second resolves
    // immediately. switchMap must cancel the first so its late response is dropped.
    const slow = new Subject<{ team: AgenticTeam }>();
    api.getTeam
      .mockReturnValueOnce(slow.asObservable())
      .mockReturnValueOnce(of({ team: team({ team_id: 't-2', name: 'Second' }) }));
    fixture.detectChanges();

    component.selectTeam('t-1'); // fetch pending on `slow`
    component.selectTeam('t-2'); // resolves immediately → team = Second; cancels t-1

    // The stale t-1 response arrives late — it must be ignored.
    slow.next({ team: team({ team_id: 't-1', name: 'First (stale)' }) });
    slow.complete();

    expect(component.team()?.team_id).toBe('t-2');
  });

  it('auto-selects the sole process on a freshly loaded team', () => {
    api.getTeam.mockReturnValueOnce(of({ team: team({ processes: [process()] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.processId()).toBe('p-1');
    expect(state.composeProcessStatus()).toBe('draft');
  });

  it('does not auto-select when the team has multiple processes', () => {
    api.getTeam.mockReturnValueOnce(
      of({ team: team({ processes: [process({ process_id: 'p-1' }), process({ process_id: 'p-2' })] }) }),
    );
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.processId()).toBeNull();
    expect(state.composeProcessStatus()).toBeNull();
  });

  it('selectProcess sets the handoff process id and its gate status', () => {
    api.getTeam.mockReturnValueOnce(
      of({ team: team({ processes: [process({ process_id: 'p-1', status: 'complete' })] }) }),
    );
    fixture.detectChanges();
    component.selectTeam('t-1');
    component.selectProcess('p-1');
    expect(state.processId()).toBe('p-1');
    expect(state.composeProcessStatus()).toBe('complete');
  });

  it('selectProcess(null) clears the handoff process and gate status', () => {
    fixture.detectChanges();
    component.selectProcess(null);
    expect(state.processId()).toBeNull();
    expect(state.composeProcessStatus()).toBeNull();
  });

  it('surfaces a team-load error when the response has no team', () => {
    api.getTeam.mockReturnValueOnce(of({ team: null }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Team not found.');
  });

  it('surfaces a team-load error on request failure', () => {
    api.getTeam.mockReturnValueOnce(throwError(() => new Error('boom')));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Could not load this team.');
  });

  // ── onRosterChanged ─────────────────────────────────────────────────────

  it('onRosterChanged reflects the fully-staffed flag into state', () => {
    api.getTeam.mockReturnValue(of({ team: team({ processes: [process({ status: 'complete' })] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');

    const validation: RosterValidationResult = {
      is_fully_staffed: true,
      agent_count: 1,
      process_count: 1,
      gaps: [],
      summary: 'ok',
    };
    component.onRosterChanged(validation);
    expect(state.rosterFullyStaffed()).toBe(true);
  });

  it('onRosterChanged(null) marks the roster as not fully staffed', () => {
    fixture.detectChanges();
    component.selectTeam('t-1');
    state.setRosterFullyStaffed(true);
    component.onRosterChanged(null);
    expect(state.rosterFullyStaffed()).toBe(false);
  });

  it('onRosterChanged re-syncs the process gate status after a chat-side process edit', () => {
    api.getTeam
      .mockReturnValueOnce(of({ team: team({ processes: [process({ process_id: 'p-1', status: 'draft' })] }) }))
      .mockReturnValueOnce(of({ team: team({ processes: [process({ process_id: 'p-1', status: 'complete' })] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1'); // auto-selects p-1 as draft
    expect(state.composeProcessStatus()).toBe('draft');

    component.onRosterChanged({ is_fully_staffed: true, agent_count: 1, process_count: 1, gaps: [], summary: '' });
    expect(state.composeProcessStatus()).toBe('complete');
  });

  it('onRosterChanged is a no-op API-wise when no team is selected', () => {
    fixture.detectChanges();
    api.getTeam.mockClear();
    component.onRosterChanged(null);
    expect(api.getTeam).not.toHaveBeenCalled();
  });

  it('a failed background re-sync does NOT surface a full-stage error or tear down the team', () => {
    // Initial load succeeds; the background re-sync fetch then blips.
    api.getTeam
      .mockReturnValueOnce(of({ team: team() }))
      .mockReturnValueOnce(throwError(() => new Error('transient')));
    fixture.detectChanges();
    component.selectTeam('t-1'); // user-initiated load succeeds
    expect(component.team()).not.toBeNull();
    expect(component.teamLoadError()).toBeNull();

    component.onRosterChanged({ is_fully_staffed: true, agent_count: 1, process_count: 1, gaps: [], summary: '' });

    // A background re-sync failure must NOT set teamLoadError (which would unmount
    // the working chat/roster) — the current team stays on screen.
    expect(component.teamLoadError()).toBeNull();
    expect(component.team()).not.toBeNull();
  });

  it('a user-initiated load failure DOES surface a full-stage error', () => {
    api.getTeam.mockReturnValueOnce(throwError(() => new Error('down')));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Could not load this team.');
  });

  // ── Stage-2 → Stage-3 handoff: auto-add the tested agent (idempotent) ────────

  it('auto-adds the handoff agent to the team when it is not already on the roster', () => {
    state.setRegistryAgentId('blogging.planner'); // arrived via "Add to team →"
    api.getTeam.mockReturnValue(of({ team: team({ agents: [] }) })); // roster lacks it
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(api.addAgentFromRegistry).toHaveBeenCalledWith('t-1', 'blogging.planner');
  });

  it('does NOT auto-add when the team already carries that manifest (idempotent)', () => {
    state.setRegistryAgentId('blogging.planner');
    api.getTeam.mockReturnValue(
      of({ team: team({ agents: [agent({ manifest_id: 'blogging.planner' })] }) }),
    );
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  it('does NOT auto-add when there is no handoff agent', () => {
    // registryAgentId stays null (reached Stage 3 without a Stage-2 selection).
    api.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  it('attempts the auto-add at most once per team (a later re-sync does not re-add)', () => {
    state.setRegistryAgentId('blogging.planner');
    api.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1'); // first load → one add attempt
    expect(api.addAgentFromRegistry).toHaveBeenCalledTimes(1);

    // A background re-sync (another applyTeam for the same team) must not re-add —
    // otherwise a subsequent manual delete would be silently undone.
    component.onRosterChanged({ is_fully_staffed: false, agent_count: 0, process_count: 0, gaps: [], summary: '' });
    expect(api.addAgentFromRegistry).toHaveBeenCalledTimes(1);
  });

  it('does not re-add to a team on return after visiting another team (per-team attempt set)', () => {
    state.setRegistryAgentId('blogging.planner');
    // A returns without the agent (user manually removed it after the first add).
    api.getTeam.mockImplementation((id: string) => of({ team: team({ team_id: id, agents: [] }) }));
    fixture.detectChanges();

    component.selectTeam('t-1'); // attempt #1 for A
    component.selectTeam('t-2'); // attempt #1 for B (different team)
    api.addAgentFromRegistry.mockClear();

    component.selectTeam('t-1'); // return to A — must NOT re-add (already attempted for A)
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  it('preserves registryAgentId after auto-add (Stage 4 back-loop still works)', () => {
    state.setRegistryAgentId('blogging.planner');
    api.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.registryAgentId()).toBe('blogging.planner');
  });

  it('does not re-add after a Stage-4 back-loop recreates the component (shared-state guard)', () => {
    state.setRegistryAgentId('blogging.planner');
    // Team returns without the agent (user manually removed it after the first add).
    api.getTeam.mockImplementation((id: string) => of({ team: team({ team_id: id, agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1'); // attempt #1 for A
    expect(api.addAgentFromRegistry).toHaveBeenCalledTimes(1);
    api.addAgentFromRegistry.mockClear();

    // Stage 4 → "iterate roster" back-loop: the shell destroys and recreates the
    // Compose component, but the shell-scoped state service (and its
    // consumed-handoff set) survives. A fresh component sharing that state — whose
    // ngOnInit re-loads the still-selected team — must NOT re-add the agent the
    // user removed, since registryAgentId intentionally stays set for the back-loop.
    fixture.destroy();
    const fixture2 = TestBed.createComponent(AgentStudioComposeTeamComponent);
    fixture2.detectChanges(); // ngOnInit → loadTeam('t-1') (state.teamId persists)
    expect(api.addAgentFromRegistry).not.toHaveBeenCalled();
  });

  // ── Create team ──────────────────────────────────────────────────────────

  it('toggleCreateForm shows/hides the form and resets it on close', () => {
    fixture.detectChanges();
    component.form.patchValue({ name: 'Draft' });
    component.toggleCreateForm();
    expect(component.showCreateForm()).toBe(true);
    component.toggleCreateForm();
    expect(component.showCreateForm()).toBe(false);
    expect(component.form.getRawValue().name).toBe('');
  });

  it('onCreateTeam no-ops when the form is invalid', () => {
    fixture.detectChanges();
    component.onCreateTeam();
    expect(api.createTeam).not.toHaveBeenCalled();
  });

  it('onCreateTeam creates the team, refreshes the list, and selects it', () => {
    fixture.detectChanges();
    component.form.patchValue({ name: 'New Team', description: 'desc' });
    component.onCreateTeam();
    expect(api.createTeam).toHaveBeenCalledWith({ name: 'New Team', description: 'desc' });
    expect(state.teamId()).toBe('t-new');
    expect(component.showCreateForm()).toBe(false);
  });

  it('onCreateTeam surfaces an error and keeps the form open', () => {
    api.createTeam.mockReturnValueOnce(throwError(() => ({ error: { detail: 'name taken' } })));
    fixture.detectChanges();
    component.form.patchValue({ name: 'Dup' });
    component.onCreateTeam();
    expect(component.createError()).toBe('name taken');
  });
});
