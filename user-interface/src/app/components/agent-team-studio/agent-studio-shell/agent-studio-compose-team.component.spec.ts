import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, tap, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
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

/** Stand-in for the catalog: same selector + the one output Stage 3 wires, so
 *  no catalog HTTP fetch runs in these unit tests. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
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
  source: 'registry',
  manifest_id: 'blogging.planner',
  role: 'Plans',
  skills: [],
  capabilities: [],
  tools: [],
  expertise: [],
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
  let facade: {
    listTeams: ReturnType<typeof vi.fn>;
    getTeam: ReturnType<typeof vi.fn>;
    composeTeam: ReturnType<typeof vi.fn>;
    addAgentToTeam: ReturnType<typeof vi.fn>;
    addAgentFromCatalog: ReturnType<typeof vi.fn>;
  };

  function configure(): void {
    TestBed.configureTestingModule({
      imports: [AgentStudioComposeTeamComponent, NoopAnimationsModule],
      providers: [
        AgentStudioStateService,
        { provide: AgentStudioFacade, useValue: facade },
      ],
    })
      .overrideComponent(AgentStudioComposeTeamComponent, {
        remove: { imports: [ProcessDesignerChatComponent, AgentCatalogComponent] },
        add: { imports: [StubProcessDesignerChatComponent, StubAgentCatalogComponent] },
      });

    fixture = TestBed.createComponent(AgentStudioComposeTeamComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
  }

  beforeEach(() => {
    facade = {
      listTeams: vi.fn().mockReturnValue(of([teamSummary('t-1'), teamSummary('t-2')])),
      getTeam: vi.fn().mockReturnValue(of({ team: team() })),
      composeTeam: vi.fn().mockImplementation((req) =>
        of({ team_id: 't-new', name: req.name, description: req.description ?? '', created_at: '' }).pipe(
          tap((resp) => state.setTeamId(resp.team_id)),
        ),
      ),
      addAgentFromCatalog: vi.fn().mockReturnValue(of(agent())),
      addAgentToTeam: vi.fn().mockImplementation((teamId: string, manifestId: string, alreadyOnRoster = false) => {
        const key = `${teamId}::${manifestId}`;
        if (state.hasConsumedHandoff(key)) {
          return of(null);
        }
        state.markHandoffConsumed(key);
        if (alreadyOnRoster) {
          return of(null);
        }
        return of(agent());
      }),
    };
    configure();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('loads the team list on init', () => {
    fixture.detectChanges();
    expect(facade.listTeams).toHaveBeenCalled();
    expect(component.teams()).toHaveLength(2);
  });

  it('loads the already-selected team on init (returning from Stage 4 iterateRoster)', () => {
    state.setTeamId('t-1');
    fixture.detectChanges();
    expect(facade.getTeam).toHaveBeenCalledWith('t-1');
    expect(component.team()?.team_id).toBe('t-1');
  });

  it('does not load a team on init when none is selected yet', () => {
    fixture.detectChanges();
    expect(facade.getTeam).not.toHaveBeenCalled();
  });

  it('surfaces a teams-list load error', () => {
    facade.listTeams.mockReturnValueOnce(throwError(() => ({ error: { detail: 'down' } })));
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
    expect(facade.getTeam).toHaveBeenCalledWith('t-1');
  });

  it('switching teams rapidly cannot apply a stale earlier response (switchMap)', () => {
    // First team's fetch is held pending on a Subject; the second resolves
    // immediately. switchMap must cancel the first so its late response is dropped.
    const slow = new Subject<{ team: AgenticTeam }>();
    facade.getTeam
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
    facade.getTeam.mockReturnValueOnce(of({ team: team({ processes: [process()] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.processId()).toBe('p-1');
    expect(state.composeProcessStatus()).toBe('draft');
  });

  it('does not auto-select when the team has multiple processes', () => {
    facade.getTeam.mockReturnValueOnce(
      of({ team: team({ processes: [process({ process_id: 'p-1' }), process({ process_id: 'p-2' })] }) }),
    );
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.processId()).toBeNull();
    expect(state.composeProcessStatus()).toBeNull();
  });

  it('selectProcess sets the handoff process id and its gate status', () => {
    facade.getTeam.mockReturnValueOnce(
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
    facade.getTeam.mockReturnValueOnce(of({ team: null }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Team not found.');
  });

  it('surfaces a team-load error on request failure', () => {
    facade.getTeam.mockReturnValueOnce(throwError(() => new Error('boom')));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Could not load this team.');
  });

  // ── onRosterChanged ─────────────────────────────────────────────────────

  it('onRosterChanged reflects the fully-staffed flag into state', () => {
    facade.getTeam.mockReturnValue(of({ team: team({ processes: [process({ status: 'complete' })] }) }));
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
    facade.getTeam
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
    facade.getTeam.mockClear();
    component.onRosterChanged(null);
    expect(facade.getTeam).not.toHaveBeenCalled();
  });

  it('a failed background re-sync does NOT surface a full-stage error or tear down the team', () => {
    // Initial load succeeds; the background re-sync fetch then blips.
    facade.getTeam
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
    facade.getTeam.mockReturnValueOnce(throwError(() => new Error('down')));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(component.teamLoadError()).toBe('Could not load this team.');
  });

  // ── Stage-2 → Stage-3 handoff: auto-add the tested agent (idempotent) ────────

  it('auto-adds the handoff agent to the team when it is not already on the roster', () => {
    state.setRegistryAgentId('blogging.planner'); // arrived via "Add to team →"
    facade.getTeam.mockReturnValue(of({ team: team({ agents: [] }) })); // roster lacks it
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(facade.addAgentToTeam).toHaveBeenCalledWith('t-1', 'blogging.planner', false);
  });

  it('does NOT auto-add when the team already carries that manifest (idempotent)', () => {
    state.setRegistryAgentId('blogging.planner');
    facade.getTeam.mockReturnValue(
      of({ team: team({ agents: [agent({ manifest_id: 'blogging.planner' })] }) }),
    );
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(facade.addAgentToTeam).toHaveBeenCalledWith('t-1', 'blogging.planner', true);
  });

  it('does NOT auto-add when there is no handoff agent', () => {
    // registryAgentId stays null (reached Stage 3 without a Stage-2 selection).
    facade.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(facade.addAgentToTeam).not.toHaveBeenCalled();
  });

  it('attempts the auto-add at most once per team (a later re-sync does not re-add)', () => {
    state.setRegistryAgentId('blogging.planner');
    facade.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1'); // first load → one add attempt
    expect(facade.addAgentToTeam).toHaveBeenCalledTimes(1);

    // A background re-sync (another applyTeam for the same team) must not re-add —
    // otherwise a subsequent manual delete would be silently undone.
    component.onRosterChanged({ is_fully_staffed: false, agent_count: 0, process_count: 0, gaps: [], summary: '' });
    expect(facade.addAgentToTeam).toHaveBeenCalledTimes(1);
  });

  it('does not re-add to a team on return after visiting another team (per-team attempt set)', () => {
    state.setRegistryAgentId('blogging.planner');
    // A returns without the agent (user manually removed it after the first add).
    facade.getTeam.mockImplementation((id: string) => of({ team: team({ team_id: id, agents: [] }) }));
    fixture.detectChanges();

    component.selectTeam('t-1'); // attempt #1 for A
    component.selectTeam('t-2'); // attempt #1 for B (different team)
    facade.addAgentToTeam.mockClear();

    component.selectTeam('t-1'); // return to A — must NOT re-add (already attempted for A)
    expect(facade.addAgentToTeam).not.toHaveBeenCalled();
  });

  it('preserves registryAgentId after auto-add (Stage 4 back-loop still works)', () => {
    state.setRegistryAgentId('blogging.planner');
    facade.getTeam.mockReturnValue(of({ team: team({ agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1');
    expect(state.registryAgentId()).toBe('blogging.planner');
  });

  it('does not re-add after a Stage-4 back-loop recreates the component (shared-state guard)', () => {
    state.setRegistryAgentId('blogging.planner');
    // Team returns without the agent (user manually removed it after the first add).
    facade.getTeam.mockImplementation((id: string) => of({ team: team({ team_id: id, agents: [] }) }));
    fixture.detectChanges();
    component.selectTeam('t-1'); // attempt #1 for A
    expect(facade.addAgentToTeam).toHaveBeenCalledTimes(1);
    facade.addAgentToTeam.mockClear();

    // Stage 4 → "iterate roster" back-loop: the shell destroys and recreates the
    // Compose component, but the shell-scoped state service (and its
    // consumed-handoff set) survives. A fresh component sharing that state — whose
    // ngOnInit re-loads the still-selected team — must NOT re-add the agent the
    // user removed, since registryAgentId intentionally stays set for the back-loop.
    fixture.destroy();
    const fixture2 = TestBed.createComponent(AgentStudioComposeTeamComponent);
    fixture2.detectChanges(); // ngOnInit → loadTeam('t-1') (state.teamId persists)
    expect(facade.addAgentToTeam).not.toHaveBeenCalled();
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
    expect(facade.composeTeam).not.toHaveBeenCalled();
  });

  it('onCreateTeam creates the team, refreshes the list, and selects it', () => {
    fixture.detectChanges();
    component.form.patchValue({ name: 'New Team', description: 'desc' });
    component.onCreateTeam();
    expect(facade.composeTeam).toHaveBeenCalledWith({ name: 'New Team', description: 'desc' });
    expect(state.teamId()).toBe('t-new');
    expect(component.showCreateForm()).toBe(false);
  });

  it('onCreateTeam surfaces an error and keeps the form open', () => {
    facade.composeTeam.mockReturnValueOnce(throwError(() => ({ error: { detail: 'name taken' } })));
    fixture.detectChanges();
    component.form.patchValue({ name: 'Dup' });
    component.onCreateTeam();
    expect(component.createError()).toBe('name taken');
  });

  // ── Browse-agents overlay ────────────────────────────────────────────────

  it('disables the Browse-agents trigger until a team is selected', () => {
    fixture.detectChanges();
    const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.browse-agents-btn');
    expect(btn.disabled).toBe(true);

    component.selectTeam('t-1');
    fixture.detectChanges();
    expect(btn.disabled).toBe(false);
  });

  it('openBrowse no-ops when no team is selected', () => {
    fixture.detectChanges();
    component.openBrowse();
    expect(component.browseOpen()).toBe(false);
  });

  it('opens and closes the Browse-agents overlay', () => {
    fixture.detectChanges();
    component.selectTeam('t-1');
    fixture.detectChanges();

    fixture.nativeElement.querySelector('.browse-agents-btn').click();
    fixture.detectChanges();
    expect(component.browseOpen()).toBe(true);
    expect(fixture.nativeElement.querySelector('.studio-slide-out__panel')).toBeTruthy();

    fixture.nativeElement.querySelector('.studio-slide-out__scrim').click();
    fixture.detectChanges();
    expect(component.browseOpen()).toBe(false);
  });

  it('adds a catalog selection via addAgentFromCatalog (not addAgentToTeam) and closes the overlay', () => {
    fixture.detectChanges();
    component.selectTeam('t-1');
    fixture.detectChanges();
    facade.addAgentToTeam.mockClear();
    component.openBrowse();
    fixture.detectChanges();

    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.writer');
    fixture.detectChanges();

    expect(facade.addAgentFromCatalog).toHaveBeenCalledWith('t-1', 'blogging.writer');
    expect(facade.addAgentToTeam).not.toHaveBeenCalled();
    expect(component.browseOpen()).toBe(false);
    expect(component.browseAddError()).toBeNull();
  });

  it('calls addAgentFromCatalog even when the pair was already handoff-consumed (dedup bypass)', () => {
    state.markHandoffConsumed('t-1::blogging.writer');
    fixture.detectChanges();
    component.selectTeam('t-1');
    fixture.detectChanges();
    component.openBrowse();
    fixture.detectChanges();

    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.writer');

    expect(facade.addAgentFromCatalog).toHaveBeenCalledWith('t-1', 'blogging.writer');
  });

  it('surfaces a failed catalog add and keeps the overlay open', () => {
    facade.addAgentFromCatalog.mockReturnValueOnce(throwError(() => ({ error: { detail: 'not eligible' } })));
    fixture.detectChanges();
    component.selectTeam('t-1');
    fixture.detectChanges();
    component.openBrowse();
    fixture.detectChanges();

    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.writer');
    fixture.detectChanges();

    expect(component.browseOpen()).toBe(true);
    expect(component.browseAddError()).toBe('not eligible');
    expect(fixture.nativeElement.querySelector('.studio-slide-out__body .error-text').textContent).toContain(
      'not eligible',
    );
  });

  it('falls back to a generic message when a catalog-add error has no detail', () => {
    facade.addAgentFromCatalog.mockReturnValueOnce(throwError(() => ({})));
    fixture.detectChanges();
    component.selectTeam('t-1');
    fixture.detectChanges();
    component.openBrowse();
    fixture.detectChanges();

    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.writer');
    fixture.detectChanges();

    expect(component.browseAddError()).toBe('Could not add this agent — try again.');
  });
});
