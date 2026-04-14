import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BloggingApiService } from '../../services/blogging-api.service';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { InvestmentApiService } from '../../services/investment-api.service';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { SalesApiService } from '../../services/sales-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { GenericJobsApiService } from '../../services/generic-jobs-api.service';
import { JobsDashboardComponent } from './jobs-dashboard.component';

describe('JobsDashboardComponent', () => {
  let component: JobsDashboardComponent;
  let fixture: ComponentFixture<JobsDashboardComponent>;
  let routerSpy: { navigate: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    routerSpy = { navigate: vi.fn() };
    const seApi = {
      getRunningJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      getJobStatus: vi.fn(),
      getPlanningV2Status: vi.fn(),
      getProductAnalysisStatus: vi.fn(),
      getBackendCodeV2Status: vi.fn(),
      getFrontendCodeV2Status: vi.fn(),
    };
    const bloggingApi = {
      getJobs: vi.fn().mockReturnValue(of([])),
      cancelJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'cancelled', message: 'Ok' })),
      deleteJob: vi.fn().mockReturnValue(of({ job_id: 'j1', message: 'Deleted' })),
    };
    const aiApi = {
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      cancelJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'cancelled', message: 'Ok' })),
      deleteJob: vi.fn().mockReturnValue(of({ job_id: 'j1', message: 'Deleted' })),
    };
    const provApi = {
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      cancelJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'cancelled', message: 'Ok' })),
      deleteJob: vi.fn().mockReturnValue(of({ job_id: 'j1', message: 'Deleted' })),
    };
    const socialApi = {
      listJobs: vi.fn().mockReturnValue(of([])),
      cancelJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'cancelled', message: 'Ok' })),
      deleteJob: vi.fn().mockReturnValue(of({ job_id: 'j1', message: 'Deleted' })),
    };
    const investmentApi = {
      listStrategyLabJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      deleteJob: vi.fn().mockReturnValue(of({ job_id: 'j1', deleted: true })),
    };
    const personaApi = {
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      cancelJob: vi.fn().mockReturnValue(of({ status: 'cancelled' })),
      deleteJob: vi.fn().mockReturnValue(of({ deleted: 'true' })),
    };
    const salesApi = {
      listPipelineJobs: vi.fn().mockReturnValue(of([])),
      cancelJob: vi.fn().mockReturnValue(of({})),
      deleteJob: vi.fn().mockReturnValue(of({})),
    };
    const planningV3Api = {
      getJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
    };
    const codingTeamApi = {};
    const genericJobsApi = {
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      cancel: vi.fn().mockReturnValue(of({})),
      resume: vi.fn().mockReturnValue(of({})),
      restart: vi.fn().mockReturnValue(of({})),
      delete: vi.fn().mockReturnValue(of({})),
    };

    await TestBed.configureTestingModule({
      imports: [JobsDashboardComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: SoftwareEngineeringApiService, useValue: seApi },
        { provide: BloggingApiService, useValue: bloggingApi },
        { provide: AISystemsApiService, useValue: aiApi },
        { provide: AgentProvisioningApiService, useValue: provApi },
        { provide: SocialMarketingApiService, useValue: socialApi },
        { provide: InvestmentApiService, useValue: investmentApi },
        { provide: PersonaTestingApiService, useValue: personaApi },
        { provide: SalesApiService, useValue: salesApi },
        { provide: PlanningV3ApiService, useValue: planningV3Api },
        { provide: CodingTeamApiService, useValue: codingTeamApi },
        { provide: GenericJobsApiService, useValue: genericJobsApi },
        { provide: Router, useValue: routerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(JobsDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have SOURCE_DISPLAY', () => {
    expect(component.SOURCE_DISPLAY).toBeDefined();
  });

  it('SOURCE_DISPLAY includes all 14 sources', () => {
    const sources = Object.keys(component.SOURCE_DISPLAY);
    expect(sources).toContain('software_engineering');
    expect(sources).toContain('blogging');
    expect(sources).toContain('ai_systems');
    expect(sources).toContain('agent_provisioning');
    expect(sources).toContain('social_marketing');
    expect(sources).toContain('investment');
    expect(sources).toContain('user_agent_founder');
    expect(sources).toContain('soc2_compliance');
    expect(sources).toContain('personal_assistant');
    expect(sources).toContain('planning_v3');
    expect(sources).toContain('road_trip_planning');
    expect(sources).toContain('nutrition_meal_planning');
    expect(sources).toContain('coding_team');
    expect(sources).toContain('sales');
  });

  it('getJobTypeInfo returns info for software_engineering job', () => {
    const job = {
      unified: { source: 'software_engineering', jobType: 'run_team', jobId: 'j1', status: 'running', createdAt: '', label: 'Run' },
      seDetail: undefined,
    } as any;
    const info = component.getJobTypeInfo(job);
    expect(info.label).toBe('Run Team');
    expect(info.route).toBe('/software-engineering');
  });

  it('getRepoName returns last segment of path', () => {
    expect(component.getRepoName('/a/b/repo-name')).toBe('repo-name');
  });

  it('getStatusClass returns class for status', () => {
    const job = { unified: { status: 'running' }, seDetail: null } as any;
    expect(component.getStatusClass(job)).toContain('status-running');
  });

  it('navigateToJob navigates with jobId and tab for SE job', () => {
    const job = {
      unified: { source: 'software_engineering', jobType: 'run_team', jobId: 'j1' },
      seDetail: undefined,
    } as any;
    component.navigateToJob(job);
    expect(routerSpy.navigate).toHaveBeenCalledWith(['/software-engineering'], { queryParams: { jobId: 'j1', tab: 0 } });
  });

  it('refresh sets loading and restarts polling', () => {
    component.refresh();
    expect(component.loading).toBe(true);
  });

  it('trackByJobId returns composite key', () => {
    const job = { unified: { source: 'se', jobId: 'j1' } } as any;
    expect(component.trackByJobId(0, job)).toBe('se:j1');
  });

  describe('canResumeJob', () => {
    const makeJob = (status: string, source = 'software_engineering') =>
      ({ unified: { source, jobId: 'j1', status }, seDetail: null } as any);

    it('returns true for failed, interrupted, agent_crash, cancelled', () => {
      expect(component.canResumeJob(makeJob('failed'))).toBe(true);
      expect(component.canResumeJob(makeJob('interrupted'))).toBe(true);
      expect(component.canResumeJob(makeJob('agent_crash'))).toBe(true);
      expect(component.canResumeJob(makeJob('cancelled'))).toBe(true);
    });
    it('returns false for running, pending, completed', () => {
      expect(component.canResumeJob(makeJob('running'))).toBe(false);
      expect(component.canResumeJob(makeJob('pending'))).toBe(false);
      expect(component.canResumeJob(makeJob('completed'))).toBe(false);
    });
    it('works for any source — no allowlist', () => {
      expect(component.canResumeJob(makeJob('failed', 'soc2_compliance'))).toBe(true);
      expect(component.canResumeJob(makeJob('failed', 'sales'))).toBe(true);
      expect(component.canResumeJob(makeJob('failed', 'nutrition_meal_planning'))).toBe(true);
      expect(component.canResumeJob(makeJob('failed', 'coding_team'))).toBe(true);
    });
  });

  describe('canStopJob', () => {
    const makeJob = (status: string, source = 'software_engineering') =>
      ({ unified: { source, status }, seDetail: null } as any);

    it('returns true for running or pending regardless of source', () => {
      expect(component.canStopJob(makeJob('running', 'software_engineering'))).toBe(true);
      expect(component.canStopJob(makeJob('pending', 'blogging'))).toBe(true);
      expect(component.canStopJob(makeJob('running', 'investment'))).toBe(true);
      expect(component.canStopJob(makeJob('running', 'soc2_compliance'))).toBe(true);
      expect(component.canStopJob(makeJob('running', 'sales'))).toBe(true);
      expect(component.canStopJob(makeJob('pending', 'planning_v3'))).toBe(true);
    });
    it('returns false for non-active statuses', () => {
      expect(component.canStopJob(makeJob('completed'))).toBe(false);
      expect(component.canStopJob(makeJob('failed'))).toBe(false);
      expect(component.canStopJob(makeJob('cancelled'))).toBe(false);
    });
  });

  describe('canRestartJob', () => {
    const makeJob = (status: string, source = 'blogging') =>
      ({ unified: { source, status }, seDetail: null } as any);

    it('returns true for terminal statuses regardless of source', () => {
      expect(component.canRestartJob(makeJob('completed'))).toBe(true);
      expect(component.canRestartJob(makeJob('failed'))).toBe(true);
      expect(component.canRestartJob(makeJob('cancelled'))).toBe(true);
      expect(component.canRestartJob(makeJob('interrupted'))).toBe(true);
      expect(component.canRestartJob(makeJob('agent_crash'))).toBe(true);
      expect(component.canRestartJob(makeJob('failed', 'road_trip_planning'))).toBe(true);
      expect(component.canRestartJob(makeJob('completed', 'personal_assistant'))).toBe(true);
    });
    it('returns false for running or pending', () => {
      expect(component.canRestartJob(makeJob('running'))).toBe(false);
      expect(component.canRestartJob(makeJob('pending'))).toBe(false);
    });
  });

  describe('canDeleteJob', () => {
    it('returns true for terminal job statuses', () => {
      expect(component.canDeleteJob({ unified: { source: 'software_engineering', status: 'completed' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'soc2_compliance', status: 'failed' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'coding_team', status: 'cancelled' } } as any)).toBe(true);
    });

    it('returns false for running or pending jobs', () => {
      expect(component.canDeleteJob({ unified: { source: 'software_engineering', status: 'running' } } as any)).toBe(false);
      expect(component.canDeleteJob({ unified: { source: 'sales', status: 'pending' } } as any)).toBe(false);
    });
  });
});
