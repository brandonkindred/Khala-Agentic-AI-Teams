import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BloggingApiService } from '../../services/blogging-api.service';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
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
    const bloggingApi = { getJobs: vi.fn().mockReturnValue(of([])) };
    const aiApi = { listJobs: vi.fn().mockReturnValue(of({ jobs: [] })) };
    const provApi = { listJobs: vi.fn().mockReturnValue(of({ jobs: [] })) };
    const socialApi = { listJobs: vi.fn().mockReturnValue(of([])) };

    await TestBed.configureTestingModule({
      imports: [JobsDashboardComponent],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: seApi },
        { provide: BloggingApiService, useValue: bloggingApi },
        { provide: AISystemsApiService, useValue: aiApi },
        { provide: AgentProvisioningApiService, useValue: provApi },
        { provide: SocialMarketingApiService, useValue: socialApi },
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
});
