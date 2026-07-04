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

// Build a TestPipelineRun with `count` recorded steps (N) and a given status.
const pipelineRun = (count: number, over: Record<string, unknown> = {}) => ({
  run_id: 'pipe-1',
  team_id: 't1',
  process_id: 'p1',
  status: 'running',
  current_step_id: count > 0 ? `s${count}` : null,
  initial_input: null,
  step_results: Array.from({ length: count }, (_, i) => ({
    step_id: `s${i + 1}`,
    step_name: '',
    agent_name: '',
    input: '',
    output: '',
    status: 'completed',
  })),
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
  let agenticApi: { getTeam: ReturnType<typeof vi.fn>; getPipelineRun: ReturnType<typeof vi.fn> };
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
    agenticApi = {
      getTeam: vi.fn().mockReturnValue(of({ team })),
      // Default: no pipeline run available (header falls back to indeterminate).
      getPipelineRun: vi.fn().mockReturnValue(of(null)),
    };
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

  it('surfaces an error when the launch response is null (no startPolling(undefined))', () => {
    build();
    fixture.detectChanges();
    personaApi.startTest.mockReturnValue(of(null));
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

  it('surfaces an error when the response has no team (not perpetual loading)', () => {
    build();
    agenticApi.getTeam.mockReturnValue(of({ team: null }));
    fixture.detectChanges();
    expect(component.teamError()).toBe('Team not found.');
    expect(component.teamLoading()).toBe(false);
  });

  it('surfaces an error when the team response itself is null (no TypeError)', () => {
    build();
    // A null body (empty 200 / network-mapped null) must not throw on resp.team.
    agenticApi.getTeam.mockReturnValue(of(null));
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.teamError()).toBe('Team not found.');
    expect(component.team()).toBeNull();
  });

  it('degrades to an empty library when the personas response is null', () => {
    build();
    personaApi.getPersonas.mockReturnValue(of(null));
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.personas()).toEqual([]);
    expect(component.personasLoading()).toBe(false);
  });

  it('surfaces a personas-load error in the library (not the shared error signal)', () => {
    build();
    personaApi.getPersonas.mockReturnValue(throwError(() => new Error('nope')));
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
    personaApi.createPersona.mockReturnValue(of(null));
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
    personaApi.createPersona.mockReturnValue(pending);
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
    expect(personaApi.createPersona).not.toHaveBeenCalled();
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

  it('ticks the elapsed counter every second while the run is live, then freezes at terminal', () => {
    vi.useFakeTimers();
    try {
      build();
      fixture.detectChanges();
      personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      // A non-terminal status keeps the per-second elapsed counter running.
      personaApi.getRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
      );
      component.launch();
      expect(component.elapsedSec()).toBe(0);

      vi.advanceTimersByTime(3000);
      expect(component.elapsedSec()).toBe(3);

      // The next poll reports a terminal status: the counter must stop advancing.
      personaApi.getRunStatus.mockReturnValue(
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
      personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
      // Non-terminal status: without teardown, polling + the 1s timer keep firing.
      personaApi.getRunStatus.mockReturnValue(
        of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
      );
      component.launch();
      // One immediate fetch on launch.
      expect(personaApi.getRunStatus).toHaveBeenCalledTimes(1);
      const frozenElapsed = component.elapsedSec();

      fixture.destroy();
      // takeUntilDestroyed must tear down both the poll and the elapsed interval:
      // no further getRunStatus calls and the counter no longer advances.
      vi.advanceTimersByTime(60_000);
      expect(personaApi.getRunStatus).toHaveBeenCalledTimes(1);
      expect(component.elapsedSec()).toBe(frozenElapsed);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows "persona is thinking…" while the run is live and hides it at terminal', () => {
    build();
    fixture.detectChanges();
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' }));
    // Hold the run non-terminal so the thinking indicator stays visible.
    personaApi.getRunStatus.mockReturnValue(
      of({ run_id: 'run-1', status: 'polling_build', decisions: [] }),
    );
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('persona is thinking…');

    // A terminal status removes the indicator (polling has stopped).
    personaApi.getRunStatus.mockReturnValue(
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

  it('shows "Loading team…" in persona mode while the team is being fetched', () => {
    build();
    // Hold the team response pending so the loading branch is observable.
    const pending = new Subject<{ team: unknown }>();
    agenticApi.getTeam.mockReturnValue(pending);
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
    agenticApi.getTeam.mockReturnValue(pending);
    fixture.detectChanges();
    // Seed a persona + process as if from a handoff, but the team never resolved.
    state.setPersonaId('startup-founder');
    component.selectProcess('p1');
    component.launch();
    expect(personaApi.startTest).not.toHaveBeenCalled();
  });

  it('renders the decision transcript when a run has decisions', () => {
    build();
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(
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
    personaApi.getRunStatus.mockReturnValue(
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
      personaApi.startTest.mockReturnValue(new Subject()); // never emits
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
    personaApi.getRunStatus.mockReturnValue(of(null));
    expect(() => component.launch()).not.toThrow();
    expect(component.run()).toBeNull();
  });

  // ── Run-progress header (step bar + WAIT indicator) ─────────────────────────

  it('renders "step N of M" when the pipeline run and process DAG are known', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(2)));
    component.launch();
    fixture.detectChanges();
    // Pipeline read is piggybacked on the founder poll, keyed on se_job_id.
    expect(agenticApi.getPipelineRun).toHaveBeenCalledWith('t1', 'pipe-1');
    expect(component.totalSteps()).toBe(4);
    expect(component.currentStepCount()).toBe(2);
    expect(component.stepProgressKnown()).toBe(true);
    expect(component.stepPercent()).toBe(50);
    expect(component.currentStepName()).toBe('Write');
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 2 of 4');
    expect(text).toContain('Write');
    // Determinate bar is shown (not the indeterminate fallback).
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('determinate');
  });

  it('falls back to the indeterminate bar when the DAG length is unknown', () => {
    // TEAM's single complete process has no steps → totalSteps 0 → no "step N of M".
    build();
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(2)));
    component.launch();
    fixture.detectChanges();
    expect(component.totalSteps()).toBe(0);
    expect(component.stepProgressKnown()).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('step ');
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('falls back to the indeterminate bar before any pipeline run is available', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    // Non-terminal founder status carries an se_job_id, but the pipeline read
    // returns nothing yet (run just started) → no step count → indeterminate.
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(null));
    component.launch();
    fixture.detectChanges();
    expect(component.pipelineRun()).toBeNull();
    expect(component.currentStepCount()).toBe(0);
    expect(component.stepProgressKnown()).toBe(false);
    const bar = fixture.nativeElement.querySelector('mat-progress-bar');
    expect(bar?.getAttribute('mode')).toBe('indeterminate');
  });

  it('flags isWaiting and shows the WAIT note during a waiting_for_input step', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob({ status: 'answering_build_questions' })));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(2, { status: 'waiting_for_input' })));
    component.launch();
    fixture.detectChanges();
    expect(component.isWaiting()).toBe(true);
    // The animated thinking indicator is shown, with a WAIT-specific note wired
    // off isWaiting() (not dead code).
    expect(fixture.nativeElement.textContent).toContain('persona is thinking…');
    expect(fixture.nativeElement.textContent).toContain('answering a question');
  });

  it('does not show the WAIT note when the pipeline is not waiting', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(2, { status: 'running' })));
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
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(2, { process_id: 'p1' })));
    component.launch();
    expect(component.totalSteps()).toBe(4);

    // User switches the dropdown mid-run to the 2-step process pB.
    component.selectProcess('pB');
    expect(component.selectedProcessId()).toBe('pB');
    // Denominator + step name still follow the running p1 (4 steps · Write), not pB.
    expect(component.totalSteps()).toBe(4);
    expect(component.currentStepName()).toBe('Write');
  });

  it('clamps the step label so an over-count never reads "5 of 4"', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    // A looped/revisited step can append more step_results than declared steps.
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(5)));
    component.launch();
    fixture.detectChanges();
    expect(component.currentStepCount()).toBe(5);
    expect(component.totalSteps()).toBe(4);
    expect(component.displayStepCount()).toBe(4);
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('step 4 of 4');
    expect(text).not.toContain('step 5 of 4');
  });

  it('does not read the pipeline once the run is terminal (no wasted GET)', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob({ status: 'completed' })));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(4)));
    component.launch();
    expect(component.runTerminal()).toBe(true);
    expect(agenticApi.getPipelineRun).not.toHaveBeenCalled();
    expect(component.pipelineRun()).toBeNull();
  });

  it('ignores a stale pipeline response whose run_id is not the current se_job_id', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob())); // se_job_id 'pipe-1'
    // A pipeline read for a different (superseded) run must not populate the signal.
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(3, { run_id: 'pipe-OTHER' })));
    component.launch();
    fixture.detectChanges();
    expect(component.pipelineRun()).toBeNull();
    expect(component.stepProgressKnown()).toBe(false);
  });

  it('swallows a pipeline-read error and degrades to the indeterminate bar', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(throwError(() => new Error('blip')));
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
    personaApi.getRunStatus.mockReturnValue(of({ run_id: 'run-1', status: 'generating_spec', decisions: [] }));
    component.launch();
    expect(agenticApi.getPipelineRun).not.toHaveBeenCalled();
    expect(component.stepProgressKnown()).toBe(false);
  });

  it('resets pipeline progress on a new launch so a prior run does not bleed through', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(of(statusWithJob()));
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(3)));
    component.launch();
    expect(component.currentStepCount()).toBe(3);

    // A second launch clears the prior pipeline state before the first read lands.
    personaApi.startTest.mockReturnValue(of({ job_id: 'run-2', status: 'running', message: '' }));
    personaApi.getRunStatus.mockReturnValue(new Subject());
    component.launch();
    expect(component.pipelineRun()).toBeNull();
    expect(component.currentStepCount()).toBe(0);
  });

  it('hides the progress bar and thinking indicator once the run is terminal', () => {
    build({ team: TEAM_WITH_STEPS });
    fixture.detectChanges();
    personaApi.getRunStatus.mockReturnValue(
      of(statusWithJob({ status: 'completed' })),
    );
    agenticApi.getPipelineRun.mockReturnValue(of(pipelineRun(4, { status: 'completed' })));
    component.launch();
    fixture.detectChanges();
    expect(component.runTerminal()).toBe(true);
    // stepProgressKnown is gated on !runTerminal, so no bar/thinking is shown.
    expect(component.stepProgressKnown()).toBe(false);
    expect(fixture.nativeElement.querySelector('mat-progress-bar')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('persona is thinking…');
  });
});
