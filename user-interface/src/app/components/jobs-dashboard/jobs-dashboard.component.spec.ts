import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { MatDialog } from '@angular/material/dialog';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog/confirm-dialog.component';
import { JobActionsService } from '../../services/job-actions.service';
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
  let dialogSpy: { open: ReturnType<typeof vi.fn> };
  let jobActionsSpy: {
    stop: ReturnType<typeof vi.fn>;
    resume: ReturnType<typeof vi.fn>;
    restart: ReturnType<typeof vi.fn>;
    delete: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    routerSpy = { navigate: vi.fn() };
    dialogSpy = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(true) }),
    };
    jobActionsSpy = {
      stop: vi.fn().mockReturnValue(of({})),
      resume: vi.fn().mockReturnValue(of({})),
      restart: vi.fn().mockReturnValue(of({})),
      delete: vi.fn().mockReturnValue(of({})),
    };
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
        { provide: JobActionsService, useValue: jobActionsSpy },
      ],
    })
      .overrideProvider(MatDialog, { useValue: dialogSpy })
      .compileComponents();

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

  it('renders a single merged Job column (no separate Team/Type headers)', () => {
    component.jobs = [
      {
        unified: {
          source: 'software_engineering',
          jobType: 'run_team',
          jobId: 'j1',
          status: 'running',
          createdAt: new Date().toISOString(),
          label: 'Run',
          repoPath: '/work/payments-api',
        },
        seDetail: undefined,
      } as any,
    ];
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const headers = Array.from(host.querySelectorAll('thead th')).map((th) => (th.textContent ?? '').trim());
    expect(headers).toContain('Job');
    expect(headers).not.toContain('Team');
    expect(headers).not.toContain('Type');

    expect(host.querySelectorAll('td.col-job').length).toBe(1);
    expect(host.querySelectorAll('td.col-team').length).toBe(0);
    expect(host.querySelectorAll('td.col-type').length).toBe(0);

    const jobLabel = host.querySelector('td.col-job .job-label')?.textContent?.trim();
    const jobTeam = host.querySelector('td.col-job .job-team')?.textContent?.trim();
    expect(jobLabel).toBe('Run Team');
    expect(jobTeam).toBe('Software Engineering');
  });

  it('navigateToJob navigates with jobId and tab for SE job', () => {
    const job = {
      unified: { source: 'software_engineering', jobType: 'run_team', jobId: 'j1' },
      seDetail: undefined,
    } as any;
    component.navigateToJob(job);
    expect(routerSpy.navigate).toHaveBeenCalledWith(['/software-engineering'], { queryParams: { jobId: 'j1', tab: 0 } });
  });

  describe('onRowKeydown', () => {
    const seJob = () =>
      ({
        unified: { source: 'software_engineering', jobType: 'run_team', jobId: 'j1' },
        seDetail: undefined,
      }) as any;

    const makeEvent = (key: string, target: EventTarget, currentTarget: EventTarget): KeyboardEvent => {
      const evt = new KeyboardEvent('keydown', { key, cancelable: true });
      Object.defineProperty(evt, 'target', { value: target });
      Object.defineProperty(evt, 'currentTarget', { value: currentTarget });
      return evt;
    };

    it('navigates on Enter', () => {
      const tr = document.createElement('tr');
      component.onRowKeydown(makeEvent('Enter', tr, tr), seJob());
      expect(routerSpy.navigate).toHaveBeenCalled();
    });

    it('ignores Space (role="link" only activates on Enter)', () => {
      const tr = document.createElement('tr');
      component.onRowKeydown(makeEvent(' ', tr, tr), seJob());
      expect(routerSpy.navigate).not.toHaveBeenCalled();
    });

    it('ignores other keys', () => {
      const tr = document.createElement('tr');
      component.onRowKeydown(makeEvent('Tab', tr, tr), seJob());
      expect(routerSpy.navigate).not.toHaveBeenCalled();
    });

    it('ignores events whose target is a child control', () => {
      const tr = document.createElement('tr');
      const button = document.createElement('button');
      component.onRowKeydown(makeEvent('Enter', button, tr), seJob());
      expect(routerSpy.navigate).not.toHaveBeenCalled();
    });
  });

  describe('getJobAriaLabel', () => {
    it('composes team, type, repo, status, time-ago for SE job', () => {
      const job = {
        unified: {
          source: 'software_engineering',
          jobType: 'run_team',
          jobId: 'j1',
          status: 'running',
          label: 'Run',
          repoPath: '/work/payments-api',
          createdAt: new Date(Date.now() - 3 * 60_000).toISOString(),
        },
        seDetail: undefined,
      } as any;
      const label = component.getJobAriaLabel(job);
      expect(label).toContain('Run Team');
      expect(label).toContain('repo payments-api');
      expect(label).toContain('status running');
      expect(label).toContain('started 3m ago');
    });

    it('falls back to job label for non-SE rows', () => {
      const job = {
        unified: { source: 'blogging', jobType: undefined, jobId: 'j1', status: 'completed', label: 'My Post', createdAt: '' },
        seDetail: undefined,
      } as any;
      const label = component.getJobAriaLabel(job);
      expect(label).toContain('My Post');
      expect(label).toContain('status completed');
    });

    it('reports "status waiting" for SE jobs awaiting answers', () => {
      const job = {
        unified: {
          source: 'software_engineering',
          jobType: 'run_team',
          jobId: 'j1',
          status: 'running',
          label: 'Run',
          repoPath: '/work/payments-api',
          createdAt: '',
        },
        seDetail: { waitingForAnswers: true },
      } as any;
      const label = component.getJobAriaLabel(job);
      expect(label).toContain('status waiting');
    });
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

  describe('destructive actions', () => {
    const makeEvent = (): Event =>
      ({ stopPropagation: vi.fn() } as unknown as Event);

    const job = (status = 'running'): any => ({
      unified: {
        source: 'software_engineering',
        jobId: 'j1',
        label: 'My Job',
        status,
      },
      seDetail: undefined,
    });

    const dialogReturnsConfirmed = (confirmed: boolean): void => {
      dialogSpy.open.mockReturnValue({ afterClosed: () => of(confirmed) });
    };

    it('stopJob opens ConfirmDialogComponent with danger variant and Stop label', () => {
      dialogReturnsConfirmed(true);
      component.stopJob(makeEvent(), job());
      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      const [comp, config] = dialogSpy.open.mock.calls[0];
      expect(comp).toBe(ConfirmDialogComponent);
      expect(config.data).toMatchObject({
        title: 'Stop job',
        message: 'Are you sure you want to stop the job for "My Job"?',
        confirmLabel: 'Stop',
        variant: 'danger',
      });
    });

    it('stopJob calls jobActions.stop only when the dialog confirms', () => {
      dialogReturnsConfirmed(true);
      component.stopJob(makeEvent(), job());
      expect(jobActionsSpy.stop).toHaveBeenCalledWith('software_engineering', 'j1');
    });

    it('stopJob does NOT call jobActions.stop when the dialog is cancelled', () => {
      dialogReturnsConfirmed(false);
      component.stopJob(makeEvent(), job());
      expect(jobActionsSpy.stop).not.toHaveBeenCalled();
    });

    it('restartJob opens ConfirmDialogComponent with warn variant and Restart label', () => {
      dialogReturnsConfirmed(true);
      component.restartJob(makeEvent(), job('failed'));
      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      const [comp, config] = dialogSpy.open.mock.calls[0];
      expect(comp).toBe(ConfirmDialogComponent);
      expect(config.data).toMatchObject({
        title: 'Restart job',
        message: 'Restart job for "My Job" from scratch?',
        confirmLabel: 'Restart',
        variant: 'warn',
      });
    });

    it('restartJob calls jobActions.restart only when the dialog confirms', () => {
      dialogReturnsConfirmed(true);
      component.restartJob(makeEvent(), job('failed'));
      expect(jobActionsSpy.restart).toHaveBeenCalledWith('software_engineering', 'j1');
    });

    it('restartJob does NOT call jobActions.restart when the dialog is cancelled', () => {
      dialogReturnsConfirmed(false);
      component.restartJob(makeEvent(), job('failed'));
      expect(jobActionsSpy.restart).not.toHaveBeenCalled();
    });

    it('deleteJob opens ConfirmDialogComponent with danger variant and Delete label', () => {
      dialogReturnsConfirmed(true);
      component.deleteJob(makeEvent(), job('completed'));
      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      const [comp, config] = dialogSpy.open.mock.calls[0];
      expect(comp).toBe(ConfirmDialogComponent);
      expect(config.data).toMatchObject({
        title: 'Delete job',
        message: 'Permanently delete this job? It will be removed from the list.',
        confirmLabel: 'Delete',
        variant: 'danger',
      });
    });

    it('deleteJob calls jobActions.delete only when the dialog confirms', () => {
      dialogReturnsConfirmed(true);
      component.deleteJob(makeEvent(), job('completed'));
      expect(jobActionsSpy.delete).toHaveBeenCalledWith('software_engineering', 'j1');
    });

    it('deleteJob does NOT call jobActions.delete when the dialog is cancelled', () => {
      dialogReturnsConfirmed(false);
      component.deleteJob(makeEvent(), job('completed'));
      expect(jobActionsSpy.delete).not.toHaveBeenCalled();
    });
  });
});
