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
    // Filters persist to sessionStorage; clear it before each test so state
    // from one test does not leak into the next via the new component's
    // ngOnInit → loadFilters call.
    sessionStorage.clear();
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

  it('renders friendly fallback label for sources without an explicit type entry', () => {
    component.jobs = [
      {
        unified: {
          source: 'soc2_compliance',
          jobId: 'j2',
          status: 'running',
          createdAt: new Date().toISOString(),
          label: 'Audit run',
        },
        seDetail: undefined,
      } as any,
    ];
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const jobLabel = host.querySelector('td.col-job .job-label')?.textContent?.trim();
    // Falls back to SOURCE_DISPLAY['soc2_compliance'].label — never the raw id.
    expect(jobLabel).toBe('SOC2 Compliance');
    // When the primary already equals the team label, suppress the secondary
    // .job-team line so the cell doesn't render "SOC2 Compliance / SOC2 Compliance".
    expect(host.querySelector('td.col-job .job-team')).toBeNull();
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

  describe('getActionAriaLabel', () => {
    it('composes action, team, type, and repo name for SE job', () => {
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
        seDetail: undefined,
      } as any;
      expect(component.getActionAriaLabel(job, 'Stop')).toBe(
        'Stop Software Engineering Run Team job for payments-api',
      );
      expect(component.getActionAriaLabel(job, 'Resume')).toBe(
        'Resume Software Engineering Run Team job for payments-api',
      );
      expect(component.getActionAriaLabel(job, 'Restart')).toBe(
        'Restart Software Engineering Run Team job for payments-api',
      );
      expect(component.getActionAriaLabel(job, 'Delete')).toBe(
        'Delete Software Engineering Run Team job for payments-api',
      );
    });

    it('falls back to unified.label for non-SE rows', () => {
      const job = {
        unified: { source: 'blogging', jobType: undefined, jobId: 'j1', status: 'completed', label: 'My Post', createdAt: '' },
        seDetail: undefined,
      } as any;
      expect(component.getActionAriaLabel(job, 'Delete')).toBe('Delete Blogging Blog pipeline job for My Post');
    });

    it('omits "for ..." suffix when no subject is available', () => {
      const job = {
        unified: { source: 'soc2_compliance', jobId: 'j2', status: 'failed', label: '', createdAt: '' },
        seDetail: undefined,
      } as any;
      expect(component.getActionAriaLabel(job, 'Restart')).toBe('Restart SOC2 Compliance SOC2 Compliance job');
    });

    it('renders aria-label on each visible action button in the row', () => {
      component.jobs = [
        {
          unified: {
            source: 'blogging',
            jobId: 'j-done',
            status: 'completed',
            label: 'Sample Post',
            createdAt: new Date().toISOString(),
          },
          seDetail: undefined,
        } as any,
      ];
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      // status 'completed' → restart + delete are visible, stop + resume are not.
      const restart = host.querySelector('button.restart-button');
      const del = host.querySelector('button.delete-button');
      expect(restart?.getAttribute('aria-label')).toBe('Restart Blogging Blog pipeline job for Sample Post');
      expect(del?.getAttribute('aria-label')).toBe('Delete Blogging Blog pipeline job for Sample Post');
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

  describe('stuck-job detection', () => {
    const STUCK_MS = 2 * 60 * 60 * 1000;
    const oldCreatedAt = () => new Date(Date.now() - STUCK_MS - 60_000).toISOString();
    const youngCreatedAt = () => new Date(Date.now() - 60_000).toISOString();

    const makeJob = (
      status: string,
      jobId: string,
      createdAt: string,
      progress: number | null = null,
    ) =>
      ({
        unified: {
          source: 'software_engineering',
          jobId,
          status,
          createdAt,
          label: 'job',
          progress,
        },
        seDetail: undefined,
      }) as any;

    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-05-12T12:00:00Z'));
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    const record = (rows: any[]) => (component as any).recordProgressSamples(rows);

    it('is false when status is not running, even after threshold elapsed', () => {
      const job = makeJob('failed', 'j1', oldCreatedAt(), 50);
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('is false when running but younger than threshold', () => {
      const job = makeJob('running', 'j1', youngCreatedAt(), 10);
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('is false after one unchanged poll, true after the second', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
      record([job]);
      expect(component.isStuck(job)).toBe(true);
    });

    it('resets when progress changes', () => {
      const a = makeJob('running', 'j1', oldCreatedAt(), 25);
      const b = makeJob('running', 'j1', oldCreatedAt(), 35);
      record([a]);
      record([a]);
      expect(component.isStuck(a)).toBe(true);
      record([b]);
      expect(component.isStuck(b)).toBe(false);
    });

    it('treats missing createdAt as not stuck', () => {
      const job = makeJob('running', 'j1', '', 10);
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('tooltip uses getTimeAgo-style phrasing relative to lastChangedAt', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([job]);
      // Advance 7 minutes; progress still unchanged in the next sample.
      vi.setSystemTime(new Date(Date.now() + 7 * 60_000));
      record([job]);
      expect(component.getStuckTooltip(job)).toBe('No progress in 7m — may be stuck');
    });

    it('returns fallback tooltip when no history exists', () => {
      const job = makeJob('running', 'unseen', oldCreatedAt(), 0);
      expect(component.getStuckTooltip(job)).toBe('No progress detected — may be stuck');
    });

    it('prunes history for jobs that disappear from a subsequent poll', () => {
      const a = makeJob('running', 'j1', oldCreatedAt(), 25);
      const b = makeJob('running', 'j2', oldCreatedAt(), 50);
      record([a, b]);
      const history: Map<string, unknown> = (component as any).progressHistory;
      expect(history.has('software_engineering::j1')).toBe(true);
      expect(history.has('software_engineering::j2')).toBe(true);
      record([a]);
      expect(history.has('software_engineering::j1')).toBe(true);
      expect(history.has('software_engineering::j2')).toBe(false);
    });

    it('aria-label on the row mentions "appears stuck" when stuck', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.unified.jobType = 'run_team';
      record([job]);
      record([job]);
      expect(component.getJobAriaLabel(job)).toContain('appears stuck');
    });

    it('does not flag a job whose progress is null but phase keeps changing', () => {
      const makePhaseJob = (phase: string) => {
        const job = makeJob('running', 'j1', oldCreatedAt(), null);
        job.unified.phase = phase;
        return job;
      };
      record([makePhaseJob('setup')]);
      record([makePhaseJob('planning')]);
      record([makePhaseJob('execution')]);
      expect(component.isStuck(makePhaseJob('execution'))).toBe(false);
    });

    it('flags a job with null progress + frozen phase across enough polls', () => {
      const makePhaseJob = () => {
        const job = makeJob('running', 'j1', oldCreatedAt(), null);
        job.unified.phase = 'execution';
        return job;
      };
      record([makePhaseJob()]);
      record([makePhaseJob()]);
      expect(component.isStuck(makePhaseJob())).toBe(true);
    });

    it('resets sampleCount when a failed job is resumed back into running', () => {
      // Job has been failing in place with identical progress / no phase
      // change across many polls. When the user resumes it, the next poll
      // shows it as 'running' again with the same progress and phase. The
      // resumed running attempt should *not* be flagged stuck on its first
      // observation just because the fingerprint pre-resume was sticky.
      const failed = makeJob('failed', 'j1', oldCreatedAt(), 25);
      record([failed]);
      record([failed]);
      record([failed]); // sampleCount = 3 while failed

      const resumed = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([resumed]);
      expect(component.isStuck(resumed)).toBe(false);
      // And on the next poll where it's still unchanged, the running streak
      // is now 2 → stuck (so the heuristic still works post-resume).
      record([resumed]);
      expect(component.isStuck(resumed)).toBe(true);
    });

    it('excludes SE jobs waiting for answers even after threshold elapsed', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.seDetail = { waitingForAnswers: true, progress: 25 } as any;
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('renders the stuck glyph with aria-hidden="false" and a non-empty aria-label', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.unified.jobType = 'run_team';
      job.unified.repoPath = '/work/payments-api';
      record([job]);
      record([job]);
      component.jobs = [job];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      const glyph = host.querySelector('.col-time .stuck-indicator') as HTMLElement | null;
      expect(glyph).toBeTruthy();
      // mat-icon defaults aria-hidden=true; we must override so SRs announce
      // the label.
      expect(glyph?.getAttribute('aria-hidden')).toBe('false');
      expect(glyph?.getAttribute('role')).toBe('img');
      expect(glyph?.getAttribute('aria-label')?.length ?? 0).toBeGreaterThan(0);
    });
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

  describe('filters', () => {
    const buildRow = (overrides: {
      jobId: string;
      status: string;
      source?: string;
      waitingForAnswers?: boolean;
    }): any => ({
      unified: {
        source: overrides.source ?? 'software_engineering',
        jobId: overrides.jobId,
        status: overrides.status,
        label: overrides.jobId,
        createdAt: new Date().toISOString(),
      },
      seDetail: overrides.waitingForAnswers ? { waitingForAnswers: true } : undefined,
    });

    it('statusCounts buckets running/pending/waiting → active', () => {
      component.jobs = [
        buildRow({ jobId: 'r', status: 'running' }),
        buildRow({ jobId: 'p', status: 'pending' }),
        buildRow({ jobId: 'w', status: 'running', waitingForAnswers: true }),
      ];
      const counts = component.statusCounts;
      expect(counts.active).toBe(3);
      expect(counts.failed).toBe(0);
      expect(counts.completed).toBe(0);
      expect(counts.all).toBe(3);
    });

    it('statusCounts buckets failed/interrupted/agent_crash → failed', () => {
      component.jobs = [
        buildRow({ jobId: 'f', status: 'failed' }),
        buildRow({ jobId: 'i', status: 'interrupted' }),
        buildRow({ jobId: 'c', status: 'agent_crash' }),
      ];
      const counts = component.statusCounts;
      expect(counts.failed).toBe(3);
      expect(counts.active).toBe(0);
      expect(counts.completed).toBe(0);
    });

    it('statusCounts buckets all terminal "finished" statuses → completed', () => {
      // Terminal "the run finished" bucket. `needs_human_review` is the
      // Blogging team's review-ready terminal state; `completed_with_errors`
      // is the Investment Strategy Lab's partial-success terminal state.
      // Neither should leak into Active.
      component.jobs = [
        buildRow({ jobId: 'd', status: 'completed' }),
        buildRow({ jobId: 'x', status: 'cancelled' }),
        buildRow({ jobId: 'r', status: 'needs_human_review', source: 'blogging' }),
        buildRow({ jobId: 'e', status: 'completed_with_errors', source: 'investment' }),
      ];
      const counts = component.statusCounts;
      expect(counts.completed).toBe(4);
      expect(counts.active).toBe(0);
      expect(counts.failed).toBe(0);
      expect(counts.all).toBe(4);
    });

    it('statusCounts.all always equals total job count', () => {
      component.jobs = [
        buildRow({ jobId: 'r', status: 'running' }),
        buildRow({ jobId: 'f', status: 'failed' }),
        buildRow({ jobId: 'd', status: 'completed' }),
      ];
      const counts = component.statusCounts;
      expect(counts.all).toBe(3);
      expect(counts.active + counts.failed + counts.completed).toBe(counts.all);
    });

    it('setStatus("failed") restricts filteredJobs to failed-bucket rows', () => {
      component.jobs = [
        buildRow({ jobId: 'r', status: 'running' }),
        buildRow({ jobId: 'f', status: 'failed' }),
        buildRow({ jobId: 'i', status: 'interrupted' }),
        buildRow({ jobId: 'd', status: 'completed' }),
      ];
      component.setStatus('failed');
      const ids = component.filteredJobs.map((r) => r.unified.jobId);
      expect(ids.sort()).toEqual(['f', 'i']);
    });

    it('SE row waitingForAnswers is Active even when status is "completed"', () => {
      // The waiting flag wins over unified.status — a job that's waiting for
      // human answers is live work, not done work.
      const row = buildRow({ jobId: 'w', status: 'completed', waitingForAnswers: true });
      component.jobs = [row];
      component.setStatus('active');
      expect(component.filteredJobs).toHaveLength(1);
      component.setStatus('completed');
      expect(component.filteredJobs).toHaveLength(0);
    });

    it('toggleTeam restricts to that team, again removes the restriction', () => {
      component.jobs = [
        buildRow({ jobId: 'a', status: 'running', source: 'software_engineering' }),
        buildRow({ jobId: 'b', status: 'running', source: 'blogging' }),
      ];
      component.toggleTeam('blogging' as any);
      expect(component.filteredJobs.map((r) => r.unified.jobId)).toEqual(['b']);
      component.toggleTeam('blogging' as any);
      expect(component.filteredJobs).toHaveLength(2);
    });

    it('clearTeams() resets to all teams visible', () => {
      component.jobs = [
        buildRow({ jobId: 'a', status: 'running', source: 'software_engineering' }),
        buildRow({ jobId: 'b', status: 'running', source: 'blogging' }),
      ];
      component.toggleTeam('software_engineering' as any);
      component.toggleTeam('blogging' as any);
      component.clearTeams();
      expect(component.selectedTeams.size).toBe(0);
      expect(component.filteredJobs).toHaveLength(2);
    });

    it('persists status + teams to sessionStorage and restores them', () => {
      component.setStatus('failed');
      component.toggleTeam('blogging' as any);

      const raw = sessionStorage.getItem('jobs-dashboard-filters-v1');
      expect(raw).toBeTruthy();
      expect(JSON.parse(raw!)).toEqual({ status: 'failed', teams: ['blogging'] });

      // Fresh component instance reads the same key.
      const fresh = TestBed.createComponent(JobsDashboardComponent);
      fresh.detectChanges();
      expect(fresh.componentInstance.selectedStatus).toBe('failed');
      expect([...fresh.componentInstance.selectedTeams]).toEqual(['blogging']);
    });

    it('drops unknown teams during restore (defensive against schema drift)', () => {
      sessionStorage.setItem(
        'jobs-dashboard-filters-v1',
        JSON.stringify({ status: 'active', teams: ['blogging', 'not_a_real_team'] }),
      );
      const fresh = TestBed.createComponent(JobsDashboardComponent);
      fresh.detectChanges();
      expect([...fresh.componentInstance.selectedTeams]).toEqual(['blogging']);
    });

    it('ArrowRight on the All pill moves selection to Active', () => {
      const target = document.createElement('button');
      target.classList.add('filter-pill');
      const evt = new KeyboardEvent('keydown', { key: 'ArrowRight', cancelable: true });
      Object.defineProperty(evt, 'currentTarget', { value: target });
      component.onPillKeydown(evt, 'all');
      expect(component.selectedStatus).toBe('active');
    });

    it('ArrowLeft on the All pill wraps to Completed', () => {
      const target = document.createElement('button');
      const evt = new KeyboardEvent('keydown', { key: 'ArrowLeft', cancelable: true });
      Object.defineProperty(evt, 'currentTarget', { value: target });
      component.onPillKeydown(evt, 'all');
      expect(component.selectedStatus).toBe('completed');
    });

    it('renders "no jobs match" empty state when filters exclude every row', () => {
      component.jobs = [buildRow({ jobId: 'r', status: 'running' })];
      component.setStatus('failed');
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.filtered-empty')).toBeTruthy();
      expect(host.querySelector('.filtered-empty h2')?.textContent).toContain('No jobs match');
      expect(host.querySelector('.filter-pill.clear-all')).toBeTruthy();
    });

    it('Clear filters button resets status and teams to defaults', () => {
      component.setStatus('failed');
      component.toggleTeam('blogging' as any);
      component.clearAllFilters();
      expect(component.selectedStatus).toBe('all');
      expect(component.selectedTeams.size).toBe(0);
    });

    it('renders one pill per status bucket with live counts', () => {
      component.jobs = [
        buildRow({ jobId: 'r', status: 'running' }),
        buildRow({ jobId: 'f', status: 'failed' }),
      ];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      const pills = host.querySelectorAll('.status-pills .filter-pill');
      expect(pills.length).toBe(4); // all / active / failed / completed
      const labels = Array.from(pills).map((p) => p.querySelector('.pill-label')?.textContent?.trim());
      const counts = Array.from(pills).map((p) => p.querySelector('.pill-count')?.textContent?.trim());
      expect(labels).toEqual(['All', 'Active', 'Failed', 'Completed']);
      expect(counts).toEqual(['2', '1', '1', '0']);
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
