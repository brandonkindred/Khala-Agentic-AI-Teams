import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError, Subject } from 'rxjs';
import { vi } from 'vitest';
import type { PersonaInfo, PersonaTestRunDetail } from '../../models/persona-testing.model';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgenticTeamTestPanelComponent } from '../agentic-team-test-panel/agentic-team-test-panel.component';

// Stub the heavy manual-testing panel so these tests isolate Stage 4 wiring.
@Component({ selector: 'app-agentic-team-test-panel', standalone: true, template: '' })
class StubTestPanelComponent {
  @Input() team: unknown;
}

const TEAM = {
  team_id: 't1',
  name: 'Growth Pod',
  description: '',
  agents: [],
  processes: [
    { process_id: 'p1', name: 'Content pipeline', description: '', steps: [], status: 'complete' },
    { process_id: 'p2', name: 'Draft pipeline', description: '', steps: [], status: 'draft' },
  ],
  created_at: '',
  updated_at: '',
};

const TEAM_NO_COMPLETE = {
  ...TEAM,
  processes: [
    { process_id: 'p2', name: 'Draft pipeline', description: '', steps: [], status: 'draft' },
  ],
};

const PERSONAS = [
  { id: 'startup-founder', name: 'Startup Founder', description: '', icon: 'rocket', is_builtin: true },
  { id: 'impatient-pm', name: 'Impatient PM', description: '', icon: 'person', is_builtin: false },
];

describe('AgentStudioPersonaComponent', () => {
  let component: AgentStudioPersonaComponent;
  let fixture: ComponentFixture<AgentStudioPersonaComponent>;
  let state: AgentStudioStateService;
  let agenticApi: { getTeam: ReturnType<typeof vi.fn> };
  let personaApi: {
    getPersonas: ReturnType<typeof vi.fn>;
    startTest: ReturnType<typeof vi.fn>;
    getRunStatus: ReturnType<typeof vi.fn>;
    createPersona: ReturnType<typeof vi.fn>;
  };
  let dialog: { open: ReturnType<typeof vi.fn> };
  let dialogClose: ReturnType<typeof vi.fn>;

  const build = (opts: {
    teamId?: string | null;
    team?: unknown;
    dialogResult?: unknown;
  } = {}) => {
    const { teamId = 't1', team = TEAM, dialogResult } = opts;
    agenticApi = { getTeam: vi.fn().mockReturnValue(of({ team })) };
    personaApi = {
      getPersonas: vi.fn().mockReturnValue(of({ personas: PERSONAS })),
      startTest: vi.fn().mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' })),
      getRunStatus: vi
        .fn()
        .mockReturnValue(of({ run_id: 'run-1', status: 'completed', decisions: [] })),
      createPersona: vi
        .fn()
        .mockReturnValue(of({ id: 'new-1', name: 'New', description: '', icon: 'person', is_builtin: false })),
    };
    dialogClose = vi.fn();
    dialog = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(dialogResult), close: dialogClose }),
    };

    TestBed.configureTestingModule({
      imports: [AgentStudioPersonaComponent, NoopAnimationsModule],
      providers: [
        AgentStudioStateService,
        { provide: AgenticTeamApiService, useValue: agenticApi },
        { provide: PersonaTestingApiService, useValue: personaApi },
      ],
    }).overrideComponent(AgentStudioPersonaComponent, {
      remove: { imports: [AgenticTeamTestPanelComponent] },
      // Component-level provider beats the imported MatDialogModule's, so the
      // stub is what the component injects.
      add: { imports: [StubTestPanelComponent], providers: [{ provide: MatDialog, useValue: dialog }] },
    });

    state = TestBed.inject(AgentStudioStateService);
    if (teamId) {
      state.setTeamId(teamId);
    }
    fixture = TestBed.createComponent(AgentStudioPersonaComponent);
    component = fixture.componentInstance;
  };

  afterEach(() => TestBed.resetTestingModule());

  it('shows the empty state and loads nothing when no team is composed', () => {
    build({ teamId: null });
    fixture.detectChanges();
    expect(agenticApi.getTeam).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Compose a team');
  });

  it('loads team and personas on init', () => {
    build();
    fixture.detectChanges();
    expect(agenticApi.getTeam).toHaveBeenCalledWith('t1');
    expect(component.team()?.team_id).toBe('t1');
    expect(component.personas()).toHaveLength(2);
    // First persona is defaulted; the single complete process is pre-selected.
    expect(component.selectedPersonaId()).toBe('startup-founder');
    expect(component.selectedProcessId()).toBe('p1');
    expect(component.completeProcesses()).toHaveLength(1);
    // A team with a complete process is not blocked.
    expect(component.noCompleteProcess()).toBe(false);
  });

  it('shows the safety net when the loaded team has no complete process', () => {
    build({ team: TEAM_NO_COMPLETE });
    fixture.detectChanges();
    expect(component.noCompleteProcess()).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('no complete process');
  });

  it('does not block on a complete-process team (local data is authoritative, not /testable-teams)', () => {
    // Even though this component no longer consults /testable-teams, a team whose
    // complete processes are loaded locally must remain launchable — the prior
    // bug hard-blocked when the testable list omitted the team on an enumeration
    // outage. Here the team has a complete process, so it is never blocked.
    build();
    fixture.detectChanges();
    expect(component.noCompleteProcess()).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('no complete process');
  });

  it('launches a persona test with the agentic_team key + process_id', () => {
    build();
    fixture.detectChanges();
    component.launch();
    expect(personaApi.startTest).toHaveBeenCalledWith({
      persona_id: 'startup-founder',
      target_team_key: 'agentic_team:t1',
      process_id: 'p1',
    });
    // Immediate status fetch populates the live run; terminal status stops polling.
    expect(component.run()?.run_id).toBe('run-1');
    expect(component.runTerminal()).toBe(true);
  });

  it('does not launch without a persona or process', () => {
    build();
    fixture.detectChanges();
    component.selectProcess('');
    state.setPersonaId(null);
    component.launch();
    expect(personaApi.startTest).not.toHaveBeenCalled();
  });

  it('surfaces a launch error', () => {
    build();
    fixture.detectChanges();
    personaApi.startTest.mockReturnValue(throwError(() => new Error('boom')));
    component.launch();
    expect(component.error()).toContain('Could not start');
  });

  it('iterate-roster jumps to Compose (Stage 3)', () => {
    build();
    fixture.detectChanges();
    component.iterateRoster();
    expect(state.activeStage()).toBe(2);
  });

  it('fix-an-agent is gated on a registry agent in focus', () => {
    build();
    fixture.detectChanges();
    expect(component.canFixAgent()).toBe(false);
    component.fixAgent();
    expect(state.activeStage()).toBe(0); // no-op without an agent
    state.setRegistryAgentId('reg-1');
    component.fixAgent();
    expect(state.activeStage()).toBe(1);
  });

  it('creates a persona from the editor dialog and selects it', () => {
    build({
      dialogResult: {
        name: 'New',
        description: '',
        icon: 'person',
        system_prompt: 's',
        spec_generation_prompt: 'g',
      },
    });
    fixture.detectChanges();
    component.newPersona();
    expect(personaApi.createPersona).toHaveBeenCalled();
    expect(component.personas().some((p) => p.id === 'new-1')).toBe(true);
    expect(component.selectedPersonaId()).toBe('new-1');
  });

  it('switches to the manual sub-mode', () => {
    build();
    fixture.detectChanges();
    component.setMode('manual');
    expect(component.mode()).toBe('manual');
    fixture.detectChanges();
    // The (stubbed) manual test panel mounts in manual mode.
    expect(fixture.nativeElement.querySelector('app-agentic-team-test-panel')).toBeTruthy();
  });

  it('surfaces a team-load error', () => {
    build();
    agenticApi.getTeam.mockReturnValue(throwError(() => new Error('nope')));
    fixture.detectChanges();
    expect(component.teamError()).toContain('Could not load');
  });

  it('surfaces a personas-load error', () => {
    build();
    personaApi.getPersonas.mockReturnValue(throwError(() => new Error('nope')));
    fixture.detectChanges();
    expect(component.error()).toContain('Could not load personas');
  });

  it('ignores a stale status response from a superseded run', () => {
    build();
    fixture.detectChanges();
    // First launch: the immediate getRunStatus is held pending via a Subject so
    // it's still "in flight" when the next run starts.
    const stale = new Subject<PersonaTestRunDetail>();
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    personaApi.getRunStatus.mockReturnValueOnce(stale).mockReturnValue(new Subject());
    component.launch();
    expect(component.run()).toBeNull(); // run-1 status not yet emitted

    // Second launch supersedes run-1.
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    personaApi.getRunStatus.mockReturnValueOnce(new Subject()).mockReturnValue(new Subject());
    component.launch();

    // The stale run-1 response now lands — it must be ignored, not clobber run-2
    // or stop the new poller.
    stale.next({ run_id: 'run-1', status: 'completed', decisions: [] } as PersonaTestRunDetail);
    expect(component.run()).toBeNull();
    expect(component.runTerminal()).toBe(false);
  });

  it('keeps the live run non-terminal while the run is still in progress', () => {
    build();
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(
      of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
    );
    component.launch();
    expect(component.run()?.status).toBe('polling_build');
    expect(component.runTerminal()).toBe(false);
  });

  it('surfaces a create-persona error', () => {
    build({
      dialogResult: {
        name: 'New',
        description: '',
        icon: 'person',
        system_prompt: 's',
        spec_generation_prompt: 'g',
      },
    });
    fixture.detectChanges();
    personaApi.createPersona.mockReturnValue(throwError(() => new Error('boom')));
    component.newPersona();
    expect(component.error()).toContain('Could not create');
  });

  it('does nothing when the persona editor is cancelled', () => {
    build({ dialogResult: undefined });
    fixture.detectChanges();
    component.newPersona();
    expect(personaApi.createPersona).not.toHaveBeenCalled();
  });

  it('closes an open persona dialog when the component is destroyed', () => {
    build({ dialogResult: undefined });
    fixture.detectChanges();
    component.newPersona();
    expect(dialogClose).not.toHaveBeenCalled();
    fixture.destroy();
    expect(dialogClose).toHaveBeenCalled();
  });

  it('finish-in-compose jumps to Stage 3', () => {
    build();
    fixture.detectChanges();
    component.finishInCompose();
    expect(state.activeStage()).toBe(2);
  });

  it('drops a handoff-seeded process that is not complete', () => {
    // TEAM: p1 complete, p2 draft. A stale Stage-3 handoff seeds the draft p2;
    // after the team loads it must be dropped (not left selected → it would
    // enable Run on a process the backend 422s), falling back to the only
    // complete process p1.
    build();
    state.setProcessId('p2');
    fixture.detectChanges();
    expect(component.selectedProcessId()).toBe('p1');
  });

  it('clears a stale "lost contact" banner when a status arrives', () => {
    build();
    fixture.detectChanges();
    component.error.set('Lost contact with the run; retrying…');
    component.launch(); // immediate getRunStatus success → handleStatus clears error
    expect(component.error()).toBeNull();
  });

  it('survives a transient poll error and recovers without tearing down the stream', () => {
    vi.useFakeTimers();
    try {
      build();
      fixture.detectChanges();
      personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      personaApi.getRunStatus
        .mockReturnValueOnce(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }))
        .mockReturnValueOnce(throwError(() => new Error('blip')))
        .mockReturnValue(of({ run_id: 'run-1', status: 'completed', decisions: [] }));
      component.launch();
      expect(component.run()?.status).toBe('polling_build');

      // First interval tick errors — caught inside switchMap, stream stays alive.
      vi.advanceTimersByTime(10_000);
      expect(component.error()).toContain('Lost contact');
      expect(component.runTerminal()).toBe(false);

      // Next tick succeeds — banner cleared, run reaches terminal (proves the
      // poller kept ticking after the error rather than dying on it).
      vi.advanceTimersByTime(10_000);
      expect(component.error()).toBeNull();
      expect(component.run()?.status).toBe('completed');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a loading indicator (not the empty state) while personas load', () => {
    build();
    // Hold the personas response pending so the loading branch is observable.
    const pending = new Subject<{ personas: PersonaInfo[] }>();
    personaApi.getPersonas.mockReturnValue(pending);
    fixture.detectChanges();
    expect(component.personasLoading()).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Loading personas');
    expect(fixture.nativeElement.textContent).not.toContain('No personas');

    pending.next({ personas: [] });
    fixture.detectChanges();
    expect(component.personasLoading()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('No personas');
  });

  it('shows "No decisions recorded." when a terminal run has no decisions', () => {
    build();
    fixture.detectChanges();
    // The default getRunStatus resolves to a completed run with empty decisions.
    component.launch();
    expect(component.runTerminal()).toBe(true);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No decisions recorded');
    expect(fixture.nativeElement.textContent).not.toContain('Waiting for the first decision');
  });

  it('does not banner a superseded run when a stale immediate fetch errors', () => {
    build();
    fixture.detectChanges();
    // First launch: hold the immediate fetch pending so it can error *after* a
    // second run starts.
    const stale = new Subject<PersonaTestRunDetail>();
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    personaApi.getRunStatus.mockReturnValueOnce(stale).mockReturnValue(new Subject());
    component.launch();

    // Second launch supersedes run-1 (activeRunId becomes run-2).
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    personaApi.getRunStatus.mockReturnValueOnce(new Subject()).mockReturnValue(new Subject());
    component.launch();

    // The stale run-1 immediate fetch now errors — it must NOT stamp run-2 with
    // the lost-contact banner.
    stale.error(new Error('blip'));
    expect(component.error()).toBeNull();
  });
});
