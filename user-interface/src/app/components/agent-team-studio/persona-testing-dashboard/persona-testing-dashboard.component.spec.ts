import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { PersonaTestingDashboardComponent } from './persona-testing-dashboard.component';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import { JobActionsService } from '../../../services/job-actions.service';
import type { PersonaInfo, PersonaTestRun } from '../../../models';

describe('PersonaTestingDashboardComponent', () => {
  let component: PersonaTestingDashboardComponent;
  let fixture: ComponentFixture<PersonaTestingDashboardComponent>;
  let jobActionsSpy: {
    stop: ReturnType<typeof vi.fn>;
    resume: ReturnType<typeof vi.fn>;
    restart: ReturnType<typeof vi.fn>;
    delete: ReturnType<typeof vi.fn>;
  };
  let apiStub: {
    getPersonas: ReturnType<typeof vi.fn>;
    getTestableTeams: ReturnType<typeof vi.fn>;
    getRuns: ReturnType<typeof vi.fn>;
    startTest: ReturnType<typeof vi.fn>;
    createPersona: ReturnType<typeof vi.fn>;
    updatePersona: ReturnType<typeof vi.fn>;
    deletePersona: ReturnType<typeof vi.fn>;
  };

  const sampleRun = (overrides: Partial<PersonaTestRun> = {}): PersonaTestRun => ({
    run_id: 'run-abc',
    status: 'running',
    created_at: '2026-04-24T00:00:00Z',
    updated_at: '2026-04-24T00:00:00Z',
    ...overrides,
  });

  const samplePersona = (overrides: Partial<PersonaInfo> = {}): PersonaInfo => ({
    id: 'p-1',
    name: 'Custom Persona',
    description: 'desc',
    icon: 'person',
    is_builtin: false,
    system_prompt: 'sp',
    spec_generation_prompt: 'gp',
    created_at: '2026-04-24T00:00:00Z',
    updated_at: '2026-04-24T00:00:00Z',
    ...overrides,
  });

  beforeEach(async () => {
    jobActionsSpy = {
      stop: vi.fn().mockReturnValue(of({})),
      resume: vi.fn().mockReturnValue(of({})),
      restart: vi.fn().mockReturnValue(of({})),
      delete: vi.fn().mockReturnValue(of({})),
    };
    // Stubs prevent ngOnInit's timer-based /runs poll from firing real HTTP
    // in jsdom, which otherwise leaks as an unhandled HttpErrorResponse and
    // fails the Angular UI CI job.
    apiStub = {
      getPersonas: vi.fn().mockReturnValue(of({ personas: [] })),
      getTestableTeams: vi.fn().mockReturnValue(of({ teams: [] })),
      getRuns: vi.fn().mockReturnValue(of({ runs: [] })),
      startTest: vi.fn().mockReturnValue(of({ job_id: '', status: '', message: '' })),
      createPersona: vi.fn().mockReturnValue(of(samplePersona())),
      updatePersona: vi.fn().mockReturnValue(of(samplePersona())),
      deletePersona: vi.fn().mockReturnValue(of(undefined)),
    };
    await TestBed.configureTestingModule({
      imports: [PersonaTestingDashboardComponent],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: PersonaTestingApiService, useValue: apiStub },
        { provide: JobActionsService, useValue: jobActionsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PersonaTestingDashboardComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('filters agentic_team entries out of the testable-teams list', () => {
    // The legacy start dialog can't supply process_id, so agentic teams (which
    // require one) must not be offered here — only static targets remain.
    apiStub.getTestableTeams.mockReturnValue(
      of({
        teams: [
          { team_key: 'software_engineering', display_name: 'Software Engineering' },
          { team_key: 'agentic_team:abc', display_name: 'Acme' },
        ],
      }),
    );
    component.ngOnInit();
    expect(component.teams.map((t) => t.team_key)).toEqual(['software_engineering']);
  });

  it('routes stop to JobActionsService with user_agent_founder source', () => {
    component.stopRun(sampleRun(), new Event('click'));
    expect(jobActionsSpy.stop).toHaveBeenCalledWith('user_agent_founder', 'run-abc');
  });

  it('routes resume to JobActionsService with user_agent_founder source', () => {
    component.resumeRun(sampleRun({ status: 'failed' }), new Event('click'));
    expect(jobActionsSpy.resume).toHaveBeenCalledWith('user_agent_founder', 'run-abc');
  });

  it('routes restart to JobActionsService with user_agent_founder source', () => {
    component.restartRun(sampleRun({ status: 'completed' }), new Event('click'));
    expect(jobActionsSpy.restart).toHaveBeenCalledWith('user_agent_founder', 'run-abc');
  });

  it('routes delete to JobActionsService with user_agent_founder source', () => {
    component.deleteRun(sampleRun({ status: 'completed' }), new Event('click'));
    expect(jobActionsSpy.delete).toHaveBeenCalledWith('user_agent_founder', 'run-abc');
  });

  it('gates per-row actions by status', () => {
    const running = sampleRun({ status: 'running' });
    const failed = sampleRun({ status: 'failed' });
    const completed = sampleRun({ status: 'completed' });

    expect(component.canStop(running)).toBe(true);
    expect(component.canStop(completed)).toBe(false);
    expect(component.canResume(failed)).toBe(true);
    expect(component.canResume(completed)).toBe(false);
    expect(component.canRestart(completed)).toBe(true);
    expect(component.canRestart(running)).toBe(false);
  });

  it('allows stopping during orchestrator Q&A phases', () => {
    // Codex P2: orchestrator emits these non-terminal statuses during question
    // loops and the backend's ``_cancellable_statuses()`` accepts them.
    expect(component.canStop(sampleRun({ status: 'answering_analysis_questions' }))).toBe(true);
    expect(component.canStop(sampleRun({ status: 'answering_build_questions' }))).toBe(true);
    expect(component.canStop(sampleRun({ status: 'generating_spec' }))).toBe(true);
  });

  // ── Persona CRUD dialogs ────────────────────────────────────────────────
  //
  // We test the post-dialog handlers (`onCreateDialogClosed`,
  // `onEditDialogClosed`, `onStartTestDialogClosed`) directly rather than
  // driving them through `openXxxDialog()`. Reason: with standalone
  // components that `import MatDialogModule`, the real `MatDialog`
  // service is registered at the component's environment-injector level
  // and wins over a `{ provide: MatDialog, useValue: dialogStub }` mock
  // in `TestBed.providers`. The opener methods themselves are thin
  // delegators — the meaningful logic is in the handlers.

  it('dispatches createPersona when the create dialog closes with a result', () => {
    component.onCreateDialogClosed({
      name: 'QA',
      description: 'd',
      icon: 'bug_report',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    });
    expect(apiStub.createPersona).toHaveBeenCalledWith({
      name: 'QA',
      description: 'd',
      icon: 'bug_report',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    });
  });

  it('skips API call when the create dialog is cancelled', () => {
    component.onCreateDialogClosed(undefined);
    expect(apiStub.createPersona).not.toHaveBeenCalled();
  });

  it('dispatches updatePersona with the persona id when the edit dialog closes with a result', () => {
    const p = samplePersona({ id: 'p-99' });
    component.onEditDialogClosed(p, {
      name: 'New Name',
      description: 'd',
      icon: 'person',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    });
    expect(apiStub.updatePersona).toHaveBeenCalledWith(
      'p-99',
      expect.objectContaining({ name: 'New Name' }),
    );
  });

  it('skips API call when the edit dialog is cancelled', () => {
    component.onEditDialogClosed(samplePersona({ id: 'p-99' }), undefined);
    expect(apiStub.updatePersona).not.toHaveBeenCalled();
  });

  it('confirms then dispatches deletePersona', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deletePersona(samplePersona({ id: 'p-99' }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(apiStub.deletePersona).toHaveBeenCalledWith('p-99');
    confirmSpy.mockRestore();
  });

  it('aborts delete when user cancels confirm', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deletePersona(samplePersona({ id: 'p-99' }));
    expect(apiStub.deletePersona).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('Edit and Delete are available even on builtin personas (per user choice)', () => {
    // Behaviorally, Edit and Delete fire on any persona regardless of
    // is_builtin. The is_builtin flag is purely visual (extra chip).
    const builtin = samplePersona({ id: 'startup-founder', is_builtin: true });
    component.onEditDialogClosed(builtin, {
      name: 'Renamed',
      description: 'd',
      icon: 'person',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    });
    expect(apiStub.updatePersona).toHaveBeenCalledWith(
      'startup-founder',
      expect.objectContaining({ name: 'Renamed' }),
    );

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deletePersona(builtin);
    expect(apiStub.deletePersona).toHaveBeenCalledWith('startup-founder');
    confirmSpy.mockRestore();
  });

  // ── Start Test dialog ────────────────────────────────────────────────────

  it('dispatches startTest when the start-test dialog closes with a payload', () => {
    component.personas = [samplePersona()];
    component.teams = [{ team_key: 'software_engineering', display_name: 'Software Engineering' }];
    apiStub.startTest.mockReturnValueOnce(
      of({ job_id: 'new-run', status: 'running', message: '' }),
    );
    component.onStartTestDialogClosed({
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: 'taskflow-mvp',
    });
    expect(apiStub.startTest).toHaveBeenCalledWith({
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: 'taskflow-mvp',
    });
  });

  it('skips startTest when the start-test dialog is cancelled', () => {
    component.onStartTestDialogClosed(undefined);
    expect(apiStub.startTest).not.toHaveBeenCalled();
  });

  it('openStartTestDialog short-circuits if no personas or teams are loaded yet', () => {
    component.personas = [];
    component.teams = [];
    component.openStartTestDialog();
    // No API call attempted; the opener bails before touching MatDialog.
    expect(apiStub.startTest).not.toHaveBeenCalled();
  });

  // ── Run lookup helpers ───────────────────────────────────────────────────

  it('personaName resolves through the loaded persona list with fallback', () => {
    component.personas = [samplePersona({ id: 'p-1', name: 'QA' })];
    expect(component.personaName('p-1')).toBe('QA');
    expect(component.personaName('unknown')).toBe('unknown');
    expect(component.personaName(undefined)).toBe('—');
  });

  it('teamName resolves through the loaded team list with fallback', () => {
    component.teams = [{ team_key: 'software_engineering', display_name: 'Software Engineering' }];
    expect(component.teamName('software_engineering')).toBe('Software Engineering');
    expect(component.teamName('mystery')).toBe('mystery');
    expect(component.teamName(undefined)).toBe('—');
  });
});
