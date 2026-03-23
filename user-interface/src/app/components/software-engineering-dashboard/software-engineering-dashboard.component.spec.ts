import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { SoftwareEngineeringDashboardComponent } from './software-engineering-dashboard.component';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('SoftwareEngineeringDashboardComponent', () => {
  let component: SoftwareEngineeringDashboardComponent;
  let fixture: ComponentFixture<SoftwareEngineeringDashboardComponent>;
  let apiSpy: {
    runTeam: ReturnType<typeof vi.fn>;
    getRunningJobs: ReturnType<typeof vi.fn>;
    runProductAnalysis: ReturnType<typeof vi.fn>;
    runPlanningV2: ReturnType<typeof vi.fn>;
    health: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      runTeam: vi.fn(),
      getRunningJobs: vi.fn(),
      runProductAnalysis: vi.fn(),
      runPlanningV2: vi.fn(),
      health: vi.fn(),
    };
    apiSpy.getRunningJobs.mockReturnValue(of({ jobs: [] }));
    apiSpy.health.mockReturnValue(of({ status: 'ok' }));

    const planningV3ApiSpy = {
      getJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      run: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [SoftwareEngineeringDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: apiSpy },
        { provide: PlanningV3ApiService, useValue: planningV3ApiSpy },
        { provide: ActivatedRoute, useValue: { queryParams: of({}) } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SoftwareEngineeringDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should call getRunningJobs on init', () => {
    expect(apiSpy.getRunningJobs).toHaveBeenCalledWith(false);
  });

  it('should set selectedTabIndex when changing tab', () => {
    expect(component.selectedTabIndex).toBe(0);
    component.selectedTabIndex = 1;
    expect(component.selectedTabIndex).toBe(1);
  });

  it('should set jobId on run-team submit', () => {
    component.onRunTeamSubmit({ job_id: 'job-1', status: 'running', message: 'started' });
    expect(component.jobId).toBe('job-1');
  });

  it('should set jobId on run-team submit with different job id', () => {
    component.onRunTeamSubmit({ job_id: 'test-job-id', status: 'running', message: 'Workspace created.' });
    expect(component.jobId).toBe('test-job-id');
  });

  it('should call runProductAnalysis and set productAnalysisJobId on success', () => {
    apiSpy.runProductAnalysis.mockReturnValue(of({ job_id: 'pa-1', status: 'running' }));
    component.onProductAnalysisSubmit({ repo_path: '/tmp', brief: 'Brief' });
    expect(apiSpy.runProductAnalysis).toHaveBeenCalledWith({ repo_path: '/tmp', brief: 'Brief' });
    expect(component.productAnalysisJobId).toBe('pa-1');
    expect(component.loading).toBe(false);
  });

  it('should call runPlanningV2 and set planningV2JobId on success', () => {
    apiSpy.runPlanningV2.mockReturnValue(of({ job_id: 'plan-1', status: 'running' }));
    component.onPlanningV2Submit({ repo_path: '/tmp', goal: 'Goal' });
    expect(apiSpy.runPlanningV2).toHaveBeenCalledWith({ repo_path: '/tmp', goal: 'Goal' });
    expect(component.planningV2JobId).toBe('plan-1');
    expect(component.loading).toBe(false);
  });

  it('should set jobStatus when onAnswersSubmitted is called', () => {
    const status = { job_id: 'j1', status: 'completed', task_results: [], task_ids: [], failed_tasks: [], pending_questions: [] };
    component.onAnswersSubmitted(status as any);
    expect(component.jobStatus).toEqual(status);
  });

  it('should show pending questions when jobStatus has pending_questions', () => {
    component.jobId = 'j1';
    component.jobStatus = { job_id: 'j1', status: 'running', task_results: [], task_ids: [], failed_tasks: [], pending_questions: [{ question_id: 'q1', question: 'Q?', answer: null }] } as any;
    fixture.detectChanges();
    expect(component.jobStatus?.pending_questions?.length).toBe(1);
  });

  it('isRunTeamJobResumable returns false for running status', () => {
    component.jobStatus = { status: 'running' } as any;
    expect(component.isRunTeamJobResumable()).toBe(false);
  });

  it('isRunTeamJobResumable returns false for pending status', () => {
    component.jobStatus = { status: 'pending' } as any;
    expect(component.isRunTeamJobResumable()).toBe(false);
  });

  it('isRunTeamJobResumable returns true for failed status', () => {
    component.jobStatus = { status: 'failed' } as any;
    expect(component.isRunTeamJobResumable()).toBe(true);
  });

  it('isRunTeamJobResumable returns true for cancelled status', () => {
    component.jobStatus = { status: 'cancelled' } as any;
    expect(component.isRunTeamJobResumable()).toBe(true);
  });

  it('isRunTeamJobResumable returns true for agent_crash status', () => {
    component.jobStatus = { status: 'agent_crash' } as any;
    expect(component.isRunTeamJobResumable()).toBe(true);
  });

  it('isRunTeamJobResumable returns false for completed status', () => {
    component.jobStatus = { status: 'completed' } as any;
    expect(component.isRunTeamJobResumable()).toBe(false);
  });

  it('clearRunTeamJob clears jobId and jobStatus', () => {
    component.jobId = 'j1';
    component.jobStatus = {} as any;
    component.clearRunTeamJob();
    expect(component.jobId).toBeNull();
    expect(component.jobStatus).toBeNull();
  });

  it('runningJobTypeLabel returns label for known type', () => {
    expect(component.runningJobTypeLabel('run_team')).toBe('Run Team');
    expect(component.runningJobTypeLabel('planning_v2')).toBe('Planning (v2)');
  });
});
