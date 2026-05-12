import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
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

describe('JobsDashboardComponent a11y', () => {
  // Shape matches BlogJobListItem (snake_case) — the component's
  // fromBlogJobListItem mapper reads job_id / created_at, not camelCase.
  const buildBlogJob = (overrides: Record<string, unknown>) => ({
    job_id: 'j1',
    status: 'completed',
    brief: 'Sample Job',
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    ...overrides,
  });

  const setupTestBed = async (bloggingJobs: ReturnType<typeof buildBlogJob>[]) => {
    const seApi = {
      getRunningJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      getJobStatus: vi.fn(),
      getPlanningV2Status: vi.fn(),
      getProductAnalysisStatus: vi.fn(),
      getBackendCodeV2Status: vi.fn(),
      getFrontendCodeV2Status: vi.fn(),
    };
    const bloggingApi = {
      getJobs: vi.fn().mockReturnValue(of(bloggingJobs)),
      cancelJob: vi.fn(),
      deleteJob: vi.fn(),
    };
    const aiApi = { listJobs: vi.fn().mockReturnValue(of({ jobs: [] })), cancelJob: vi.fn(), deleteJob: vi.fn() };
    const provApi = { listJobs: vi.fn().mockReturnValue(of({ jobs: [] })), cancelJob: vi.fn(), deleteJob: vi.fn() };
    const socialApi = { listJobs: vi.fn().mockReturnValue(of([])), cancelJob: vi.fn(), deleteJob: vi.fn() };
    const investmentApi = { listStrategyLabJobs: vi.fn().mockReturnValue(of({ jobs: [] })), deleteJob: vi.fn() };
    const personaApi = { listJobs: vi.fn().mockReturnValue(of({ jobs: [] })), cancelJob: vi.fn(), deleteJob: vi.fn() };
    const salesApi = { listPipelineJobs: vi.fn().mockReturnValue(of([])), cancelJob: vi.fn(), deleteJob: vi.fn() };
    const planningV3Api = { getJobs: vi.fn().mockReturnValue(of({ jobs: [] })) };
    const codingTeamApi = {};
    const genericJobsApi = {
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      cancel: vi.fn(),
      resume: vi.fn(),
      restart: vi.fn(),
      delete: vi.fn(),
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
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    }).compileComponents();
  };

  // `color-contrast` is disabled because jsdom can't paint, so axe can't
  // compute real composited colors and hangs on HTMLCanvasElement.getContext.
  // Contrast is verified via math in the PR description + browser axe
  // DevTools, not in this unit spec.
  const axeOptions = {
    rules: {
      'color-contrast': { enabled: false },
    },
  };

  it('has no axe violations with rendered jobs', async () => {
    const cancelledJob = buildBlogJob({ job_id: 'j-cancel', status: 'cancelled', brief: 'Cancelled Job' });
    const completedJob = buildBlogJob({ job_id: 'j-done', status: 'completed', brief: 'Completed Job' });
    await setupTestBed([cancelledJob, completedJob]);

    const fixture = TestBed.createComponent(JobsDashboardComponent);
    fixture.componentInstance.lastUpdated = new Date();
    fixture.detectChanges();
    // The component polls via timer(0, 20s) — calling whenStable() would
    // hang forever. Flush the timer(0) microtask manually instead.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    // Guard: don't pass axe vacuously against an empty DOM.
    expect(fixture.nativeElement.querySelector('tbody tr.job-row')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);

  it('has no axe violations in the empty state', async () => {
    await setupTestBed([]);

    const fixture = TestBed.createComponent(JobsDashboardComponent);
    fixture.detectChanges();
    // The component polls via timer(0, 20s) — calling whenStable() would
    // hang forever. Flush the timer(0) microtask manually instead.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});
