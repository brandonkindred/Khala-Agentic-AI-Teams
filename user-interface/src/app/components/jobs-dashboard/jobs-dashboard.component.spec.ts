import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BloggingApiService } from '../../services/blogging-api.service';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { InvestmentApiService } from '../../services/investment-api.service';
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
    };

    await TestBed.configureTestingModule({
      imports: [JobsDashboardComponent],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: seApi },
        { provide: BloggingApiService, useValue: bloggingApi },
        { provide: AISystemsApiService, useValue: aiApi },
        { provide: AgentProvisioningApiService, useValue: provApi },
        { provide: SocialMarketingApiService, useValue: socialApi },
        { provide: InvestmentApiService, useValue: investmentApi },
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
    const seJob = (status: string) =>
      ({ unified: { source: 'software_engineering', jobId: 'j1', status }, seDetail: null } as any);

    it('returns true for failed status', () => {
      expect(component.canResumeJob(seJob('failed'))).toBe(true);
    });
    it('returns false for cancelled status', () => {
      expect(component.canResumeJob(seJob('cancelled'))).toBe(false);
    });
    it('returns true for agent_crash status', () => {
      expect(component.canResumeJob(seJob('agent_crash'))).toBe(true);
    });
    it('returns false for running status', () => {
      expect(component.canResumeJob(seJob('running'))).toBe(false);
    });
    it('returns false for pending status', () => {
      expect(component.canResumeJob(seJob('pending'))).toBe(false);
    });
    it('returns false for completed status', () => {
      expect(component.canResumeJob(seJob('completed'))).toBe(false);
    });
    it('returns false for non-resumable source', () => {
      const job = { unified: { source: 'market_research', jobId: 'j1', status: 'failed' }, seDetail: null } as any;
      expect(component.canResumeJob(job)).toBe(false);
    });
  });

  describe('canStopJob', () => {
    it('returns true for software_engineering when pending or running', () => {
      expect(component.canStopJob({ unified: { source: 'software_engineering', status: 'pending' }, seDetail: null } as any)).toBe(true);
      expect(component.canStopJob({ unified: { source: 'software_engineering', status: 'running' }, seDetail: null } as any)).toBe(true);
    });
    it('returns true for blogging when pending or running', () => {
      expect(component.canStopJob({ unified: { source: 'blogging', status: 'pending' }, seDetail: null } as any)).toBe(true);
      expect(component.canStopJob({ unified: { source: 'blogging', status: 'running' }, seDetail: null } as any)).toBe(true);
    });
    it('returns true for agent_provisioning, ai_systems, social_marketing when pending or running', () => {
      expect(component.canStopJob({ unified: { source: 'agent_provisioning', status: 'running' }, seDetail: null } as any)).toBe(true);
      expect(component.canStopJob({ unified: { source: 'ai_systems', status: 'pending' }, seDetail: null } as any)).toBe(true);
      expect(component.canStopJob({ unified: { source: 'social_marketing', status: 'running' }, seDetail: null } as any)).toBe(true);
    });
    it('returns false when status is not pending or running', () => {
      expect(component.canStopJob({ unified: { source: 'blogging', status: 'completed' }, seDetail: null } as any)).toBe(false);
    });
  });

  describe('canDeleteJob', () => {
    it('returns true for software_engineering, blogging, agent_provisioning, ai_systems, social_marketing', () => {
      expect(component.canDeleteJob({ unified: { source: 'software_engineering' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'blogging' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'agent_provisioning' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'ai_systems' } } as any)).toBe(true);
      expect(component.canDeleteJob({ unified: { source: 'social_marketing' } } as any)).toBe(true);
    });
  });
});
