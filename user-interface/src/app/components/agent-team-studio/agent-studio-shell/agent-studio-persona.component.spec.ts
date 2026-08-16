import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { provideRouter, Router } from '@angular/router';
import { of, throwError, Subject, tap } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
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

// A complete process with a known 4-step DAG, so "step N of M" can be exercised.
const STEPS = [
  { step_id: 's1', name: 'Plan', description: '', step_type: 'action', agents: [], next_steps: [] },
  { step_id: 's2', name: 'Write', description: '', step_type: 'action', agents: [], next_steps: [] },
  { step_id: 's3', name: 'Review', description: '', step_type: 'action', agents: [], next_steps: [] },
  { step_id: 's4', name: 'Publish', description: '', step_type: 'action', agents: [], next_steps: [] },
];
const TEAM_WITH_STEPS = {
  ...TEAM,
  processes: [
    { process_id: 'p1', name: 'Content pipeline', description: '', steps: STEPS, status: 'complete' },
  ],
};

// Build a status payload that carries an se_job_id (the pipeline run id), so the
// component piggybacks a pipeline-run read on the founder poll.
const statusWithJob = (over: Record<string, unknown> = {}) => ({
  run_id: 'run-1',
  status: 'polling_build',
  se_job_id: 'pipe-1',
  decisions: [],
  ...over,
});

const stepResult = (id: string, status = 'completed') => ({
  step_id: id,
  step_name: '',
  agent_name: '',
  input: '',
  output: '',
  status,
});

// Build a TestPipelineRun modeling the real runner: `completed` steps are
// finished (recorded with status 'completed'), and the cursor has advanced to
// the next, still-running step `s${completed+1}` — whose result is NOT yet
// recorded (the runner records an action/decision step only on completion). So
// the "current step" is completed+1, and step_results holds only the finished
// ones. Override `step_results`/`current_step_id`/`status` via `over` for WAIT
// or mixed-status cases.
const pipelineRun = (completed: number, over: Record<string, unknown> = {}) => ({
  run_id: 'pipe-1',
  team_id: 't1',
  process_id: 'p1',
  status: 'running',
  current_step_id: `s${completed + 1}`,
  initial_input: null,
  step_results: Array.from({ length: completed }, (_, i) => stepResult(`s${i + 1}`)),
  human_prompt: null,
  error: null,
  started_at: '',
  finished_at: null,
  ...over,
});

const PERSONAS = [
  { id: 'startup-founder', name: 'Startup Founder', description: '', icon: 'rocket', is_builtin: true },
  { id: 'impatient-pm', name: 'Impatient PM', description: '', icon: 'person', is_builtin: false },
];

describe('AgentStudioPersonaComponent', () => {
  let component: AgentStudioPersonaComponent;
  let fixture: ComponentFixture<AgentStudioPersonaComponent>;
  let state: AgentStudioStateService;
  let facade: {
    getTeam: ReturnType<typeof vi.fn>;
    getTeamPipelineRun: ReturnType<typeof vi.fn>;
    listPersonas: ReturnType<typeof vi.fn>;
    startPersonaRun: ReturnType<typeof vi.fn>;
    getPersonaRunStatus: ReturnType<typeof vi.fn>;
    createPersona: ReturnType<typeof vi.fn>;
    cancelPersonaRun: ReturnType<typeof vi.fn>;
  };
  let dialog: { open: ReturnType<typeof vi.fn> };
  let dialogClose: ReturnType<typeof vi.fn>;

  const build = (opts: {
    teamId?: string | null;
    team?: unknown;
    dialogResult?: unknown;
  } = {}) => {
    const { teamId = 't1', team = TEAM, dialogResult } = opts;
    facade = {
      getTeam: vi.fn().mockReturnValue(of({ team })),
      // Default: no pipeline run available (header falls back to indeterminate).
      getTeamPipelineRun: vi.fn().mockReturnValue(of(null)),
      listPersonas: vi.fn().mockReturnValue(of({ personas: PERSONAS })),
      startPersonaRun: vi.fn().mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' })),
      getPersonaRunStatus: vi
        .fn()
        .mockReturnValue(of({ run_id: 'run-1', status: 'completed', decisions: [] })),
      createPersona: vi.fn().mockImplementation((payload) =>
        of({
          id: 'new-1',
          name: payload?.name ?? 'New',
          description: payload?.description ?? '',
          icon: payload?.icon ?? 'person',
          is_builtin: false,
        }).pipe(tap((created) => state.setPersonaId(created.id))),
      ),
      cancelPersonaRun: vi.fn().mockReturnValue(of({})),
    };
    dialogClose = vi.fn();
    dialog = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(dialogResult), close: dialogClose }),
    };

    TestBed.configureTestingModule({
      imports: [AgentStudioPersonaComponent, NoopAnimationsModule],
      providers: [
        AgentStudioStateService,
        provideRouter([]),
        { provide: AgentStudioFacade, useValue: facade },
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
    expect(facade.getTeam).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Compose a team');
  });

  it('loads team and personas on init', () => {
    build();
    fixture.detectChanges();
    expect(facade.getTeam).toHaveBeenCalledWith('t1');
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
    expect(facade.startPersonaRun).toHaveBeenCalledWith({
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
    expect(facade.startPersonaRun).not.toHaveBeenCalled();
  });

  it('surfaces a launch error', () => {
    build();
    fixture.detectChanges();
    facade.startPersonaRun.mockReturnValue(throwError(() => new Error('boom')));
    component.launch();
    expect(component.error()).toContain('Could not start');
  });

  it('surfaces an error when the launch response is null (no startPolling(undefined))', () => {
    build();
    fixture.detectChanges();
    facade.startPersonaRun.mockReturnValue(of(null));
    component.launch();
    expect(component.error()).toContain('Could not start');
    expect(component.launching()).toBe(false);
    // A null response must not start a run view.
    expect(component.run()).toBeNull();
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
    expect(facade.createPersona).toHaveBeenCalled();
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
    facade.getTeam.mockReturnValue(throwError(() => new Error('nope')));
    fixture.detectChanges();
    expect(component.teamError()).toContain('Could not load');
  });

  it('surfaces an error when the response has no team (not perpetual loading)', () => {
    build();
    facade.getTeam.mockReturnValue(of({ team: null }));
    fixture.detectChanges();
    expect(component.teamError()).toBe('Team not found.');
    expect(component.teamLoading()).toBe(false);
  });

  it('surfaces an error when the team response itself is null (no TypeError)', () => {
    build();
    // A null body (empty 200 / network-mapped null) must not throw on resp.team.
    facade.getTeam.mockReturnValue(of(null));
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.teamError()).toBe('Team not found.');
    expect(component.team()).toBeNull();
  });

  it('degrades to an empty library when the personas response is null', () => {
    build();
    facade.listPersonas.mockReturnValue(of(null));
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.personas()).toEqual([]);
    expect(component.personasLoading()).toBe(false);
  });

  it('surfaces a personas-load error in the library (not the shared error signal)', () => {
    build();
    facade.listPersonas.mockReturnValue(throwError(() => new Error('nope')));
    fixture.detectChanges();
    expect(component.personasError()).toContain('Could not load personas');
    // The run/launch `error` signal is untouched by a persona-library failure.
    expect(component.error()).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Could not load personas');
  });

  it('ignores a stale status response from a superseded run', () => {
    build();
    fixture.detectChanges();
    // First launch: the immediate getRunStatus is held pending via a Subject so
    // it's still "in flight" when the next run starts.
    const stale = new Subject<PersonaTestRunDetail>();
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValueOnce(stale).mockReturnValue(new Subject());
    component.launch();
    expect(component.run()).toBeNull(); // run-1 status not yet emitted

    // Second launch supersedes run-1.
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValueOnce(new Subject()).mockReturnValue(new Subject());
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
    facade.getPersonaRunStatus.mockReturnValue(
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
    facade.createPersona.mockReturnValue(throwError(() => new Error('boom')));
    component.newPersona();
    expect(component.error()).toContain('Could not create');
  });

  it('surfaces an error when the created-persona response is null', () => {
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
    facade.createPersona.mockReturnValue(of(null));
    const before = component.personas().length;
    component.newPersona();
    expect(component.error()).toContain('Could not create');
    // A null body must not be pushed into the library.
    expect(component.personas().length).toBe(before);
  });

  it('flags creatingPersona while the create POST is in flight and guards re-entry', () => {
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
    // Hold the create response pending so the in-flight state is observable.
    const pending = new Subject<PersonaInfo>();
    facade.createPersona.mockReturnValue(pending);
    expect(component.creatingPersona()).toBe(false);

    component.newPersona();
    expect(component.creatingPersona()).toBe(true);
    // A second trigger is guarded out while a create is still in flight.
    component.newPersona();
    expect(dialog.open).toHaveBeenCalledTimes(1);

    pending.next({
      id: 'new-1',
      name: 'New',
      description: '',
      icon: 'person',
      is_builtin: false,
      system_prompt: 's',
      spec_generation_prompt: 'g',
      created_at: '',
      updated_at: '',
    });
    pending.complete();
    expect(component.creatingPersona()).toBe(false);
    expect(component.personas().some((p) => p.id === 'new-1')).toBe(true);
  });

  it('does nothing when the persona editor is cancelled', () => {
    build({ dialogResult: undefined });
    fixture.detectChanges();
    component.newPersona();
    expect(facade.createPersona).not.toHaveBeenCalled();
  });

  it('closes an open persona dialog when the component is destroyed', () => {
    build({ dialogResult: undefined });
    fixture.detectChanges();
    // Hold afterClosed pending so the dialog is genuinely still open (not
    // already closed) when the component is destroyed — build()'s default
    // `of(dialogResult)` mock resolves synchronously, which would exercise the
    // "already closed" cleanup path instead of the "still open" force-close path.
    dialog.open.mockReturnValueOnce({ afterClosed: () => new Subject(), close: dialogClose });
    component.newPersona();
    expect(dialogClose).not.toHaveBeenCalled();
    fixture.destroy();
    expect(dialogClose).toHaveBeenCalled();
  });

  it('does not force-close the dialog on destroy once it has already closed normally', () => {
    // The onDestroy force-close hook is removed once afterClosed fires, so a
    // dialog that already closed (e.g. cancelled) must not be double-closed.
    build({ dialogResult: undefined });
    fixture.detectChanges();
    component.newPersona();
    expect(dialogClose).not.toHaveBeenCalled();
    fixture.destroy();
    expect(dialogClose).not.toHaveBeenCalled();
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
      facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      facade.getPersonaRunStatus
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

  it('ticks the elapsed counter every second while the run is live, then freezes at terminal', () => {
    vi.useFakeTimers();
    try {
      build();
      fixture.detectChanges();
      facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      // A non-terminal status keeps the per-second elapsed counter running.
      facade.getPersonaRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
      );
      component.launch();
      expect(component.elapsedSec()).toBe(0);

      vi.advanceTimersByTime(3000);
      expect(component.elapsedSec()).toBe(3);

      // The next poll reports a terminal status: the counter must stop advancing.
      facade.getPersonaRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'completed', decisions: [] }),
      );
      vi.advanceTimersByTime(10_000);
      expect(component.runTerminal()).toBe(true);
      const frozen = component.elapsedSec();
      vi.advanceTimersByTime(5000);
      expect(component.elapsedSec()).toBe(frozen);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops polling (and the elapsed timer) once the component is destroyed', () => {
    vi.useFakeTimers();
    try {
      build();
      fixture.detectChanges();
      facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      // Non-terminal status: without teardown, polling + the 1s timer keep firing.
      facade.getPersonaRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
      );
      component.launch();
      // One immediate fetch on launch.
      expect(facade.getPersonaRunStatus).toHaveBeenCalledTimes(1);
      const frozenElapsed = component.elapsedSec();

      fixture.destroy();
      // takeUntilDestroyed must tear down both the poll and the elapsed interval:
      // no further getRunStatus calls and the counter no longer advances.
      vi.advanceTimersByTime(60_000);
      expect(facade.getPersonaRunStatus).toHaveBeenCalledTimes(1);
      expect(component.elapsedSec()).toBe(frozenElapsed);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows "persona is thinking…" while the run is live and hides it at terminal', () => {
    build();
    fixture.detectChanges();
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    // Hold the run non-terminal so the thinking indicator stays visible.
    facade.getPersonaRunStatus.mockReturnValue(
      of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('persona is thinking…');

    // A terminal status removes the indicator (polling has stopped).
    facade.getPersonaRunStatus.mockReturnValue(
      of({ run_id: 'run-1', status: 'completed', decisions: [] }),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(true);
    expect(fixture.nativeElement.textContent).not.toContain('persona is thinking…');
  });

  it('shows a loading indicator (not the empty state) while personas load', () => {
    build();
    // Hold the personas response pending so the loading branch is observable.
    const pending = new Subject<{ personas: PersonaInfo[] }>();
    facade.listPersonas.mockReturnValue(pending);
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
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValueOnce(stale).mockReturnValue(new Subject());
    component.launch();

    // Second launch supersedes run-1 (activeRunId becomes run-2).
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValueOnce(new Subject()).mockReturnValue(new Subject());
    component.launch();

    // The stale run-1 immediate fetch now errors — it must NOT stamp run-2 with
    // the lost-contact banner.
    stale.error(new Error('blip'));
    expect(component.error()).toBeNull();
  });

  it('shows "Loading team…" in persona mode while the team is being fetched', () => {
    build();
    // Hold the team response pending so the loading branch is observable.
    const pending = new Subject<{ team: unknown }>();
    facade.getTeam.mockReturnValue(pending);
    fixture.detectChanges();
    expect(component.teamLoading()).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Loading team');
    // The launcher (process dropdown) is not shown yet.
    expect(fixture.nativeElement.querySelector('.persona__launcher')).toBeNull();

    pending.next({ team: TEAM });
    fixture.detectChanges();
    expect(component.teamLoading()).toBe(false);
    expect(fixture.nativeElement.querySelector('.persona__launcher')).toBeTruthy();
  });

  it('does not launch before the team has loaded', () => {
    build();
    const pending = new Subject<{ team: unknown }>();
    facade.getTeam.mockReturnValue(pending);
    fixture.detectChanges();
    // Seed a persona + process as if from a handoff, but the team never resolved.
    state.setPersonaId('startup-founder');
    component.selectProcess('p1');
    component.launch();
    expect(facade.startPersonaRun).not.toHaveBeenCalled();
  });

  it('renders the decision transcript when a run has decisions', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(
      of({
        run_id: 'run-1',
        status: 'running',
        decisions: [
          {
            decision_id: 1,
            question_text: 'What is the MVP scope?',
            answer_text: 'Auth + dashboard only',
            rationale: 'Ship fast, validate demand',
          },
        ],
      }),
    );
    component.launch();
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('What is the MVP scope?');
    expect(text).toContain('Auth + dashboard only');
    expect(text).toContain('Ship fast, validate demand');
    expect(text).not.toContain('Waiting for the first decision');
  });

  it('shows "Waiting for the first decision…" for a non-terminal run with no decisions', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(
      of({ run_id: 'run-1', status: 'running', decisions: [] }),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('Waiting for the first decision');
  });

  it('navigates the sub-mode tabs with arrow / Home / End keys', () => {
    build();
    fixture.detectChanges();
    component.setMode('manual');
    fixture.detectChanges();
    // The keydown handler lives on the focusable (active) tab button.
    const fire = (key: string) => {
      const active = fixture.nativeElement.querySelector('.persona__mode.is-active');
      active.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
      fixture.detectChanges();
    };
    fire('ArrowRight');
    expect(component.mode()).toBe('persona');
    fire('Home');
    expect(component.mode()).toBe('manual');
    fire('End');
    expect(component.mode()).toBe('persona');
    // Only the active tab is in the tab order (roving tabindex).
    const manualTab = fixture.nativeElement.querySelector('#studio-tab-manual');
    const personaTab = fixture.nativeElement.querySelector('#studio-tab-persona');
    expect(personaTab.getAttribute('tabindex')).toBe('0');
    expect(manualTab.getAttribute('tabindex')).toBe('-1');
    // A non-navigation key is ignored.
    fire('a');
    expect(component.mode()).toBe('persona');
  });

  it('defaults the persona when the handoff id is not in the loaded list', () => {
    build();
    state.setPersonaId('ghost-persona'); // not present in the loaded PERSONAS
    fixture.detectChanges();
    expect(component.selectedPersonaId()).toBe('startup-founder');
  });

  it('times out a hung launch request and re-enables Run', () => {
    vi.useFakeTimers();
    try {
      build();
      fixture.detectChanges();
      facade.startPersonaRun.mockReturnValue(new Subject()); // never emits
      component.launch();
      expect(component.launching()).toBe(true);
      vi.advanceTimersByTime(30_000);
      expect(component.launching()).toBe(false);
      expect(component.error()).toContain('Could not start');
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not permanently block newPersona when dialog.open throws', () => {
    build();
    fixture.detectChanges();
    dialog.open.mockImplementationOnce(() => {
      throw new Error('overlay boom');
    });
    component.newPersona();
    expect(component.error()).toContain('Could not open');
    // A later click can retry — the guard wasn't left stuck true.
    component.newPersona();
    expect(dialog.open).toHaveBeenCalledTimes(2);
  });

  it('does not throw when the team response omits the processes field', () => {
    build({ team: { team_id: 't1', name: 'T', processes: undefined } as unknown });
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.completeProcesses()).toEqual([]);
    expect(component.noCompleteProcess()).toBe(true);
  });

  it('ignores a null status payload in handleStatus (no throw, run untouched)', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(null));
    expect(() => component.launch()).not.toThrow();
    expect(component.run()).toBeNull();
  });

  // ── Run-progress header (step bar + WAIT indicator) ─────────────────────────

  it('renders "step N of M" aligned with the current (running) step', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // 1 step finished; the runner has advanced to the 2nd (running) step s2.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(1)));
    component.launch();
    fixture.detectChanges();
    // Pipeline read is piggybacked on the founder poll, keyed on se_job_id.
    expect(facade.getTeamPipelineRun).toHaveBeenCalledWith('t1', 'pipe-1');
    expect(component.totalSteps()).toBe(4);
    expect(component.completedStepCount()).toBe(1);
    // Number = current (running) step, not the finished count — aligned with the
    // step NAME (both step 2 · Write), which the old length-based count desynced.
    expect(component.currentStepNumber()).toBe(2);
    expect(component.currentStepName()).toBe('Write');
    expect(component.stepProgressKnown()).toBe(true);
    expect(component.stepPercent()).toBe(25);
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 2 of 4');
    expect(text).toContain('Write');
    // Determinate bar is shown (a step has finished; not the indeterminate fallback).
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('determinate');
  });

  it('falls back to the indeterminate bar when the DAG length is unknown', () => {
    // TEAM's single complete process has no steps → totalSteps 0 → no "step N of M".
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2)));
    component.launch();
    fixture.detectChanges();
    expect(component.totalSteps()).toBe(0);
    expect(component.stepProgressKnown()).toBe(false);
    // The divide-by-zero guard returns 0 rather than NaN for a 0-step process.
    expect(component.stepPercent()).toBe(0);
    expect(fixture.nativeElement.textContent).not.toContain('step ');
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('falls back to the indeterminate bar before any pipeline run is available', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    // Non-terminal founder status carries an se_job_id, but the pipeline read
    // returns nothing yet (run just started) → no step count → indeterminate.
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    facade.getTeamPipelineRun.mockReturnValue(of(null));
    component.launch();
    fixture.detectChanges();
    expect(component.pipelineRun()).toBeNull();
    expect(component.completedStepCount()).toBe(0);
    // The DAG length IS known here (p1 has 4 steps via the launcher fallback),
    // yet with no pipeline run there is no step position to show, so the bar
    // stays indeterminate — exercising the "no run yet" gate, not "no DAG".
    expect(component.totalSteps()).toBe(4);
    expect(component.stepProgressKnown()).toBe(false);
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('flags isWaiting and shows the WAIT note during a waiting_for_input step', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'answering_build_questions' })));
    // Realistic WAIT run: 2 steps finished, the 3rd is a WAIT recorded as
    // waiting_for_input (the runner appends a WAIT immediately, unlike an action).
    facade.getTeamPipelineRun.mockReturnValue(
      of(
        pipelineRun(2, {
          status: 'waiting_for_input',
          current_step_id: 's3',
          step_results: [stepResult('s1'), stepResult('s2'), stepResult('s3', 'waiting_for_input')],
        }),
      ),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.isWaiting()).toBe(true);
    // The waiting step is not counted as completed → still "step 3 of 4".
    expect(component.completedStepCount()).toBe(2);
    expect(component.currentStepNumber()).toBe(3);
    // The animated thinking indicator is shown, with a WAIT-specific note wired
    // off isWaiting() (not dead code).
    expect(fixture.nativeElement.textContent).toContain('persona is thinking…');
    expect(fixture.nativeElement.textContent).toContain('answering a question');
    expect(fixture.nativeElement.textContent).toContain('step 3 of 4');
  });

  it('does not show the WAIT note when the pipeline is not waiting', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2, { status: 'running' })));
    component.launch();
    fixture.detectChanges();
    expect(component.isWaiting()).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('answering a question');
  });

  it('keeps the step denominator on the live run process when the launcher selection changes', () => {
    // Two complete processes with different step counts. A mid-run change of the
    // Target-process dropdown must NOT repoint the "of M" denominator: it stays
    // on the process the run is actually executing (keyed off pipelineRun.process_id).
    const twoComplete = {
      ...TEAM,
      processes: [
        { process_id: 'p1', name: 'A', description: '', steps: STEPS, status: 'complete' },
        { process_id: 'pB', name: 'B', description: '', steps: STEPS.slice(0, 2), status: 'complete' },
      ],
    };
    build({ team: twoComplete });
    state.setProcessId('p1'); // handoff seeds p1 (two complete → no auto-select)
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // 2 finished, on step 3 (s3). s3 exists in p1 (Review) but NOT in pB — so the
    // step name distinguishes the fixed (run-process) logic from the old launcher logic.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2, { process_id: 'p1' })));
    component.launch();
    expect(component.totalSteps()).toBe(4);

    // User switches the dropdown mid-run to the 2-step process pB.
    component.selectProcess('pB');
    expect(component.selectedProcessId()).toBe('pB');
    // Denominator + step name still follow the running p1 (4 steps, s3 · Review),
    // not pB (which has no s3) — proving both key off pipelineRun.process_id.
    expect(component.totalSteps()).toBe(4);
    expect(component.currentStepName()).toBe('Review');
  });

  it('clamps the step label so an over-count never reads "5 of 4"', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // A looped/revisited run can complete more steps than the DAG declares.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(5)));
    component.launch();
    fixture.detectChanges();
    expect(component.completedStepCount()).toBe(5);
    expect(component.totalSteps()).toBe(4);
    expect(component.currentStepNumber()).toBe(4); // min(5 + 1, 4)
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 4 of 4');
    expect(text).not.toContain('step 5 of 4');
    expect(text).not.toContain('step 6 of 4');
  });

  it('keeps the bar below 100% while the final step is still running', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // 4 declared steps; 3 finished, the 4th (current) still running (not recorded
    // as completed). A determinate action step is only recorded on completion, so
    // this models the in-flight final step.
    const results = [
      stepResult('s1'),
      stepResult('s2'),
      stepResult('s3'),
      stepResult('s4', 'running'),
    ];
    facade.getTeamPipelineRun.mockReturnValue(
      of(pipelineRun(3, { step_results: results, current_step_id: 's4' })),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.completedStepCount()).toBe(3); // only 3 finished…
    expect(component.currentStepNumber()).toBe(4); // …on step 4 of 4
    expect(component.stepPercent()).toBe(75); // bar reflects work done, not started
    expect(fixture.nativeElement.textContent).toContain('step 4 of 4');
  });

  it('does not read the pipeline once the run is terminal (no wasted GET)', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'completed' })));
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(4)));
    component.launch();
    expect(component.runTerminal()).toBe(true);
    expect(facade.getTeamPipelineRun).not.toHaveBeenCalled();
    expect(component.pipelineRun()).toBeNull();
  });

  it('ignores a stale pipeline response whose run_id is not the current se_job_id', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob())); // se_job_id 'pipe-1'
    // A pipeline read for a different (superseded) run must not populate the signal.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(3, { run_id: 'pipe-OTHER' })));
    component.launch();
    fixture.detectChanges();
    expect(component.pipelineRun()).toBeNull();
    expect(component.stepProgressKnown()).toBe(false);
  });

  it('swallows a pipeline-read error and degrades to the indeterminate bar', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    facade.getTeamPipelineRun.mockReturnValue(throwError(() => new Error('blip')));
    expect(() => component.launch()).not.toThrow();
    fixture.detectChanges();
    expect(component.pipelineRun()).toBeNull();
    // A pipeline failure must not surface as a run/launch error.
    expect(component.error()).toBeNull();
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('does not read the pipeline when the founder status carries no se_job_id', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    // se_job_id absent (e.g. spec-gen phase before the build starts).
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'generating_spec', decisions: [] }));
    component.launch();
    expect(facade.getTeamPipelineRun).not.toHaveBeenCalled();
    expect(component.stepProgressKnown()).toBe(false);
  });

  it('resets pipeline progress on a new launch so a prior run does not bleed through', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(3)));
    component.launch();
    expect(component.completedStepCount()).toBe(3);

    // A second launch clears the prior pipeline state before the first read lands.
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValue(new Subject());
    component.launch();
    expect(component.pipelineRun()).toBeNull();
    expect(component.completedStepCount()).toBe(0);
  });

  it('hides the progress bar and thinking indicator once the run is terminal', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(
      of(statusWithJob({ status: 'completed' })),
    );
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(4, { status: 'completed' })));
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(true);
    // stepProgressKnown is gated on !runTerminal, so no bar/thinking is shown.
    expect(component.stepProgressKnown()).toBe(false);
    expect(fixture.nativeElement.querySelector('mat-progress-bar')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('persona is thinking…');
  });

  it('refreshes step progress on the recurring poll tick, not just the immediate fetch', () => {
    vi.useFakeTimers();
    try {
      build({ team: TEAM_WITH_STEPS });
      fixture.detectChanges();
      facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      // Non-terminal founder status on every poll; the pipeline advances a step
      // between the immediate fetch and the next interval tick.
      facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'polling_build' })));
      facade.getTeamPipelineRun
        .mockReturnValueOnce(of(pipelineRun(1))) // immediate fetch: 1 finished
        .mockReturnValue(of(pipelineRun(2))); // next tick: 2 finished
      component.launch();
      expect(component.completedStepCount()).toBe(1);

      // Advance one poll interval: the recurring switchMap → getRunStatus →
      // handleStatus → fetchPipelineRun path must refresh progress (not just the
      // one-shot immediate fetch every other progress test exercises).
      vi.advanceTimersByTime(10_000);
      expect(component.completedStepCount()).toBe(2);
      expect(component.currentStepNumber()).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the indeterminate bar (not a frozen-0% determinate) for a just-started run', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // Pipeline run exists (DAG known) but no step has finished yet.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(0)));
    component.launch();
    fixture.detectChanges();
    expect(component.completedStepCount()).toBe(0);
    expect(component.currentStepNumber()).toBe(1);
    expect(component.stepProgressKnown()).toBe(true);
    // The "step 1 of 4" label shows (known block), but the bar is indeterminate.
    expect(fixture.nativeElement.textContent).toContain('step 1 of 4');
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('renders no step name when the pipeline cursor is null (between steps)', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // 1 step finished, but current_step_id momentarily null.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(1, { current_step_id: null })));
    component.launch();
    fixture.detectChanges();
    expect(component.currentStepName()).toBe('');
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 2 of 4');
    // No " · <name>" suffix when the cursor is null.
    expect(text).not.toContain('step 2 of 4 ·');
  });

  it('keeps the step number aligned with the name at a step boundary (cursor not yet advanced)', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob()));
    // Boundary: step 2's result is recorded 'completed' but the runner has not
    // yet advanced the cursor, so current_step_id still points at s2 (the two are
    // separate DB writes). The number must follow the cursor (2), not jump to 3.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2, { current_step_id: 's2' })));
    component.launch();
    fixture.detectChanges();
    expect(component.currentStepNumber()).toBe(2);
    expect(component.currentStepName()).toBe('Write'); // s2
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 2 of 4');
    expect(text).not.toContain('step 3 of 4'); // no number/name mismatch
  });

  it('hides live progress/thinking when the pipeline has failed before the founder status catches up', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    // Founder run still reports in-progress (it lags up to a poll interval)…
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'polling_build' })));
    // …but the underlying pipeline has already failed.
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2, { status: 'failed' })));
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(false); // founder not yet terminal
    expect(component.pipelineTerminal()).toBe(true);
    expect(component.runLive()).toBe(false);
    expect(component.stepProgressKnown()).toBe(false);
    const text = fixture.nativeElement.textContent;
    // A dead run must not keep rendering as healthy in-progress.
    expect(text).not.toContain('persona is thinking…');
    expect(text).not.toContain('step 3 of 4');
  });

  // ── UX/a11y: humanized status, stop control, launcher guard, announcements ──

  it('shows a human-readable run status, not the raw wire value', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    fixture.detectChanges();
    expect(component.statusLabel('polling_build')).toBe('Running…');
    expect(component.statusLabel('answering_build_questions')).toBe('Answering a question…');
    expect(component.statusLabel('completed')).toBe('Completed');
    // An unmapped status is prettified, never shown raw.
    expect(component.statusLabel('some_new_phase')).toBe('Some new phase');
    // An empty/absent status degrades to a safe label, never a blank chip.
    expect(component.statusLabel('')).toBe('Unknown');
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Running…');
    expect(text).not.toContain('polling_build');
  });

  it('shows a Stop control during a live run and cancels via the founder endpoint', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    fixture.detectChanges();
    expect(component.runInProgress()).toBe(true);
    // Hold the cancel response pending so the in-flight "Stopping…" is observable.
    const cancel = new Subject<unknown>();
    facade.cancelPersonaRun.mockReturnValue(cancel);
    const stop = fixture.nativeElement.querySelector('.persona__stop');
    expect(stop).toBeTruthy();
    stop.click();
    expect(facade.cancelPersonaRun).toHaveBeenCalledWith('run-1');
    expect(component.cancelling()).toBe(true);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.persona__stop').textContent).toContain('Stopping…');
    // On success the cancel synchronously marks the run terminal, so the button
    // stays disabled ("Stopping…") until the poll hides it — cancelling stays true
    // (not reset), preventing a re-click firing a redundant, error-prone cancel.
    cancel.next({});
    cancel.complete();
    expect(component.cancelling()).toBe(true);
  });

  it('does not fire a redundant cancel on a re-click while a stop is pending', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    const cancel = new Subject<unknown>();
    facade.cancelPersonaRun.mockReturnValue(cancel);
    component.stopRun();
    cancel.next({});
    cancel.complete(); // cancel succeeded; cancelling stays true (run not yet polled terminal)
    expect(component.cancelling()).toBe(true);
    // A re-click during the pre-poll window must NOT fire a second cancel (which
    // would 409 on the already-cancelled job and banner a spurious error).
    component.stopRun();
    expect(facade.cancelPersonaRun).toHaveBeenCalledTimes(1);
    expect(component.error()).toBeNull();
  });

  it('surfaces an error and re-enables Stop when the cancel request fails', () => {
    build();
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    facade.cancelPersonaRun.mockReturnValue(throwError(() => new Error('nope')));
    component.stopRun();
    expect(component.cancelling()).toBe(false);
    expect(component.error()).toContain('Could not stop the run');
  });

  it('does not banner the current run when a superseded run’s cancel fails late', () => {
    build();
    fixture.detectChanges();
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    const cancelA = new Subject<unknown>();
    facade.cancelPersonaRun.mockReturnValue(cancelA);
    component.launch(); // run A
    component.stopRun(); // cancel A in flight (held pending)

    // Supersede with run B; run() is now run-2.
    facade.startPersonaRun.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-2', status: 'polling_build', decisions: [] }));
    component.launch();
    expect(component.run()?.run_id).toBe('run-2');

    // The stale cancel for run A now fails (e.g. 409 on an already-gone job).
    cancelA.error(new Error('409'));
    // It must NOT paint an error over the healthy run B, nor reset its state.
    expect(component.error()).toBeNull();
  });

  it('keeps Stop available and the launcher locked during the founder-status lag after the pipeline ends', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    // Founder job still polling…
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'polling_build' })));
    // …but the pipeline has already terminated (failed).
    facade.getTeamPipelineRun.mockReturnValue(of(pipelineRun(2, { status: 'failed' })));
    component.launch();
    fixture.detectChanges();
    // Progress/thinking hidden (pipeline dead)…
    expect(component.runLive()).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('persona is thinking…');
    // …but the founder job is still cancellable: Stop stays, launcher stays locked
    // (so a new run can't orphan the still-running founder job).
    expect(component.runInProgress()).toBe(true);
    expect(fixture.nativeElement.querySelector('.persona__stop')).toBeTruthy();
    expect((fixture.nativeElement.querySelector('.persona__run-btn') as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('does not show Stop, and cancelJob is a no-op, once the run is terminal', () => {
    build();
    fixture.detectChanges();
    // Default getRunStatus resolves to a completed run.
    component.launch();
    fixture.detectChanges();
    expect(component.runLive()).toBe(false);
    expect(fixture.nativeElement.querySelector('.persona__stop')).toBeNull();
    component.stopRun();
    expect(facade.cancelPersonaRun).not.toHaveBeenCalled();
  });

  it('disables the launcher (Run + process select) while a run is live', () => {
    build();
    fixture.detectChanges();
    // Before any run, the launcher is enabled.
    let runBtn = fixture.nativeElement.querySelector('.persona__run-btn') as HTMLButtonElement;
    let select = fixture.nativeElement.querySelector('#persona-process-select') as HTMLSelectElement;
    expect(runBtn.disabled).toBe(false);
    expect(select.disabled).toBe(false);

    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    fixture.detectChanges();
    // While live, Run + process select are disabled so a new run can't silently
    // supersede the in-flight one.
    runBtn = fixture.nativeElement.querySelector('.persona__run-btn');
    select = fixture.nativeElement.querySelector('#persona-process-select');
    expect(component.runLive()).toBe(true);
    expect(runBtn.disabled).toBe(true);
    expect(select.disabled).toBe(true);
  });

  it('renders the announcement live region before any run so the first transition is announced', () => {
    build();
    fixture.detectChanges(); // team + personas loaded; NO run launched yet
    expect(component.run()).toBeNull();
    // The aria-live region must pre-exist (empty) in the DOM before a run — a live
    // region inserted already-populated is commonly not announced, dropping the
    // first "started" transition.
    const region = fixture.nativeElement.querySelector('p.visually-hidden[role="status"][aria-live="polite"]');
    expect(region).toBeTruthy();
    expect(region.textContent.trim()).toBe('');
  });

  it('labels the live-run region and announces run-state transitions to assistive tech', () => {
    build();
    fixture.detectChanges();
    // Running → announces "running".
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'polling_build', decisions: [] }));
    component.launch();
    fixture.detectChanges();
    const region = fixture.nativeElement.querySelector('section.persona__run');
    expect(region.getAttribute('aria-labelledby')).toBe('persona-run-title');
    expect(fixture.nativeElement.querySelector('#persona-run-title').textContent).toContain('Live run');
    expect(component.runAnnouncement()).toBe('Persona test running.');

    // Completed → announcement changes (aria-live speaks the transition).
    facade.getPersonaRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'completed', decisions: [] }));
    component.launch();
    expect(component.runAnnouncement()).toBe('Persona test completed.');
  });

  it('announces the WAIT state distinctly for assistive tech', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'answering_build_questions' })));
    facade.getTeamPipelineRun.mockReturnValue(
      of(pipelineRun(2, { status: 'waiting_for_input', current_step_id: 's3' })),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.runAnnouncement()).toBe('The persona is answering a question.');
  });

  it('does not show View full audit when there is no current run', () => {
    build();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).not.toContain('View full audit');
  });

  it('shows View full audit after a run exists and navigates to the Studio audit route', () => {
    build();
    fixture.detectChanges();
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.launch();
    fixture.detectChanges();
    expect(component.run()).toBeTruthy();
    const btn = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>,
    ).find((b) => b.textContent?.includes('View full audit'));
    expect(btn).toBeTruthy();
    btn?.click();
    expect(nav).toHaveBeenCalledWith(['/agent-studio', 'persona-run', 'run-1']);
    expect(nav.mock.calls.some((c) => String(c[0]).includes('persona-testing'))).toBe(false);
  });

  it('openFullAudit is a no-op when there is no run', () => {
    build();
    fixture.detectChanges();
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.openFullAudit();
    expect(nav).not.toHaveBeenCalled();
  });

  it('resumes an in-progress run after the component is recreated', () => {
    build();
    facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'polling_build' })));
    fixture.detectChanges();
    component.launch();
    fixture.detectChanges();
    expect(state.personaLiveRunId()).toBe('run-1');
    expect(component.runInProgress()).toBe(true);

    fixture.destroy();
    fixture = TestBed.createComponent(AgentStudioPersonaComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.run()?.run_id).toBe('run-1');
    expect(component.runInProgress()).toBe(true);
    expect(fixture.nativeElement.querySelector('.persona__stop')).toBeTruthy();
  });

  it('preserves elapsed time when the component is recreated mid-run', () => {
    vi.useFakeTimers();
    try {
      build();
      facade.getPersonaRunStatus.mockReturnValue(of(statusWithJob({ status: 'polling_build' })));
      fixture.detectChanges();
      component.launch();
      vi.advanceTimersByTime(5_000);
      expect(component.elapsedSec()).toBe(5);

      fixture.destroy();
      fixture = TestBed.createComponent(AgentStudioPersonaComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();

      expect(component.elapsedSec()).toBe(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it('caps elapsed time at the run\'s updated_at when it finished while unmounted', () => {
    vi.useFakeTimers();
    try {
      build();
      const launchedAt = new Date(Date.now()).toISOString();
      facade.getPersonaRunStatus.mockReturnValue(
        of(statusWithJob({ status: 'polling_build', created_at: launchedAt })),
      );
      fixture.detectChanges();
      component.launch();
      vi.advanceTimersByTime(5_000);
      expect(component.elapsedSec()).toBe(5);
      const finishedAt = new Date(Date.now()).toISOString();

      fixture.destroy();
      vi.advanceTimersByTime(60_000);
      facade.getPersonaRunStatus.mockReturnValue(
        of(
          statusWithJob({
            status: 'completed',
            created_at: launchedAt,
            updated_at: finishedAt,
          }),
        ),
      );

      fixture = TestBed.createComponent(AgentStudioPersonaComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();

      expect(component.runTerminal()).toBe(true);
      expect(component.elapsedSec()).toBe(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not include the idle gap when a restarted run finishes while unmounted', () => {
    vi.useFakeTimers();
    try {
      const launchedAt = new Date(Date.now()).toISOString();
      const firstFinishedAt = new Date(Date.now()).toISOString();
      build();
      facade.getPersonaRunStatus.mockReturnValue(
        of(
          statusWithJob({
            status: 'completed',
            created_at: launchedAt,
            updated_at: firstFinishedAt,
          }),
        ),
      );
      fixture.detectChanges();
      component.launch();
      expect(component.runTerminal()).toBe(true);
      expect(state.personaLiveRunEndedAtMs()).not.toBeNull();

      fixture.destroy();
      vi.advanceTimersByTime(60_000);
      const secondFinishedAt = new Date(Date.now()).toISOString();
      facade.getPersonaRunStatus.mockReturnValue(
        of(
          statusWithJob({
            status: 'completed',
            created_at: launchedAt,
            updated_at: secondFinishedAt,
          }),
        ),
      );
      fixture = TestBed.createComponent(AgentStudioPersonaComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();

      expect(component.runTerminal()).toBe(true);
      expect(component.elapsedSec()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reseeds elapsed when a persisted terminal run is running again', () => {
    vi.useFakeTimers();
    try {
      build();
      facade.getPersonaRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'completed', decisions: [] }),
      );
      fixture.detectChanges();
      component.launch();
      expect(component.runTerminal()).toBe(true);
      expect(state.personaLiveRunEndedAtMs()).not.toBeNull();

      fixture.destroy();
      facade.getPersonaRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
      );
      fixture = TestBed.createComponent(AgentStudioPersonaComponent);
      component = fixture.componentInstance;
      fixture.detectChanges();

      expect(component.runTerminal()).toBe(false);
      expect(component.elapsedSec()).toBe(0);
      expect(state.personaLiveRunEndedAtMs()).toBeNull();
      vi.advanceTimersByTime(3_000);
      expect(component.elapsedSec()).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('restores a completed run after the component is recreated', () => {
    build();
    fixture.detectChanges();
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(true);

    fixture.destroy();
    fixture = TestBed.createComponent(AgentStudioPersonaComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.run()?.run_id).toBe('run-1');
    expect(component.runTerminal()).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('View full audit');
  });
});
