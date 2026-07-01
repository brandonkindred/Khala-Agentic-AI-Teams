import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { ProcessDesignerChatComponent } from '../process-designer-chat/process-designer-chat.component';
import type { AgenticTeam, AgenticTeamSummary, ProcessDefinition, RosterValidationResult } from '../../models';

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
