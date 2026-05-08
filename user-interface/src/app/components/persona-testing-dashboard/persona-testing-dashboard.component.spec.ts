import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { MatDialog, MatDialogRef } from '@angular/material/dialog';
import { of, Subject } from 'rxjs';
import { vi } from 'vitest';
import { PersonaTestingDashboardComponent } from './persona-testing-dashboard.component';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { JobActionsService } from '../../services/job-actions.service';
import type { PersonaInfo, PersonaTestRun } from '../../models';

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
  let dialogStub: { open: ReturnType<typeof vi.fn> };

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
    dialogStub = { open: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [PersonaTestingDashboardComponent],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: PersonaTestingApiService, useValue: apiStub },
        { provide: JobActionsService, useValue: jobActionsSpy },
        { provide: MatDialog, useValue: dialogStub },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PersonaTestingDashboardComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
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

  function makeDialogRef<T>(emit: T | undefined) {
    const closed = new Subject<T | undefined>();
    queueMicrotask(() => {
      closed.next(emit);
      closed.complete();
    });
    return {
      afterClosed: () => closed.asObservable(),
    } as unknown as MatDialogRef<unknown, T>;
  }

  it('opens create dialog and dispatches createPersona on save', async () => {
    dialogStub.open.mockReturnValueOnce(
      makeDialogRef({
        name: 'QA',
        description: 'd',
        icon: 'bug_report',
        system_prompt: 's',
        spec_generation_prompt: 'g',
      }),
    );
    component.openCreateDialog();
    await Promise.resolve();
    expect(apiStub.createPersona).toHaveBeenCalledWith({
      name: 'QA',
      description: 'd',
      icon: 'bug_report',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    });
  });

  it('skips API call when create dialog is cancelled', async () => {
    dialogStub.open.mockReturnValueOnce(makeDialogRef(undefined));
    component.openCreateDialog();
    await Promise.resolve();
    expect(apiStub.createPersona).not.toHaveBeenCalled();
  });

  it('opens edit dialog with the selected persona and dispatches updatePersona on save', async () => {
    const p = samplePersona({ id: 'p-99' });
    dialogStub.open.mockReturnValueOnce(
      makeDialogRef({
        name: 'New Name',
        description: 'd',
        icon: 'person',
        system_prompt: 's',
        spec_generation_prompt: 'g',
      }),
    );
    component.openEditDialog(p);
    expect(dialogStub.open).toHaveBeenCalledTimes(1);
    const passedData = dialogStub.open.mock.calls[0][1].data;
    expect(passedData).toEqual({ mode: 'edit', persona: p });
    await Promise.resolve();
    expect(apiStub.updatePersona).toHaveBeenCalledWith(
      'p-99',
      expect.objectContaining({ name: 'New Name' }),
    );
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
    // Visual is just an extra chip; behaviorally, Edit and Delete fire on any
    // persona regardless of is_builtin. Both methods take a PersonaInfo and
    // dispatch unconditionally — no read-only branch exists.
    const builtin = samplePersona({ id: 'startup-founder', is_builtin: true });
    dialogStub.open.mockReturnValueOnce(makeDialogRef(undefined));
    component.openEditDialog(builtin);
    expect(dialogStub.open).toHaveBeenCalled();

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deletePersona(builtin);
    expect(apiStub.deletePersona).toHaveBeenCalledWith('startup-founder');
    confirmSpy.mockRestore();
  });

  // ── Start Test dialog ────────────────────────────────────────────────────

  it('opens start-test dialog and starts test with the selected payload', async () => {
    component.personas = [samplePersona()];
    component.teams = [{ team_key: 'software_engineering', display_name: 'Software Engineering' }];
    apiStub.startTest.mockReturnValueOnce(
      of({ job_id: 'new-run', status: 'running', message: '' }),
    );
    dialogStub.open.mockReturnValueOnce(
      makeDialogRef({
        persona_id: 'p-1',
        target_team_key: 'software_engineering',
        project_name: 'taskflow-mvp',
      }),
    );
    component.openStartTestDialog();
    await Promise.resolve();
    expect(apiStub.startTest).toHaveBeenCalledWith({
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: 'taskflow-mvp',
    });
  });

  it('does nothing if no personas or teams are loaded yet', () => {
    component.personas = [];
    component.teams = [];
    component.openStartTestDialog();
    expect(dialogStub.open).not.toHaveBeenCalled();
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
