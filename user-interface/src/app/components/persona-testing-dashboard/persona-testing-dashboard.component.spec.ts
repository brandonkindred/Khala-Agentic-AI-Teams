import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { PersonaTestingDashboardComponent } from './persona-testing-dashboard.component';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { JobActionsService } from '../../services/job-actions.service';
import type { PersonaTestRun } from '../../models';

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
    getRuns: ReturnType<typeof vi.fn>;
    startTest: ReturnType<typeof vi.fn>;
  };

  const sampleRun = (overrides: Partial<PersonaTestRun> = {}): PersonaTestRun => ({
    run_id: 'run-abc',
    status: 'running',
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
      getRuns: vi.fn().mockReturnValue(of({ runs: [] })),
      startTest: vi.fn().mockReturnValue(of({ run_id: '', status: '', message: '' })),
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
});
