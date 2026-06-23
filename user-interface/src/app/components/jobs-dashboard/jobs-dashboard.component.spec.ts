import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
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
      imports: [JobsDashboardComponent, NoopAnimationsModule],
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

  it('getStatusClass gives completed_with_failures a distinct warning class (not plain success)', () => {
    const job = { unified: { status: 'completed_with_failures' }, seDetail: null } as any;
    expect(component.getStatusClass(job)).toBe('status-completed-with-failures');
    // Must not collapse to the clean-success class.
    const clean = { unified: { status: 'completed' }, seDetail: null } as any;
    expect(component.getStatusClass(clean)).toBe('status-completed');
  });

  it('getStatusClass shows already_complete as a success (green), not the pending default', () => {
    const job = { unified: { status: 'already_complete' }, seDetail: null } as any;
    expect(component.getStatusClass(job)).toBe('status-completed');
  });

  it('getStatusLabel renders already_complete as a human label', () => {
    const job = { unified: { status: 'already_complete' }, seDetail: null } as any;
    expect(component.getStatusLabel(job)).toBe('Already complete');
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

  it('renders non-empty status-badge text for every status (WCAG 1.4.1 safety net)', () => {
    // The 3px left-border row indicator encodes status via colour only and shares
    // colours across statuses (waiting/interrupted both use --kh-warning,
    // pending/cancelled both use --kh-border-muted), so the badge text is the
    // load-bearing non-colour cue. Guard against future refactors silently
    // dropping that text fallback. See #496.
    const cases = [
      { status: 'running', waiting: false, expected: 'Running' },
      { status: 'pending', waiting: false, expected: 'Pending' },
      { status: 'completed', waiting: false, expected: 'Completed' },
      { status: 'failed', waiting: false, expected: 'Failed' },
      { status: 'cancelled', waiting: false, expected: 'Cancelled' },
      { status: 'interrupted', waiting: false, expected: 'Interrupted' },
      { status: 'running', waiting: true, expected: 'Waiting' },
    ];
    component.jobs = cases.map(({ status, waiting }, i) => ({
      unified: {
        source: 'software_engineering',
        jobType: 'run_team',
        jobId: `j${i}`,
        status,
        createdAt: new Date().toISOString(),
        label: 'Run',
        repoPath: '/work/repo',
      },
      seDetail: waiting ? { waitingForAnswers: true } : undefined,
    }) as any);
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const badges = Array.from(host.querySelectorAll('.status-badge'));
    expect(badges.length).toBe(cases.length);
    const labels = badges.map((b) => (b.textContent ?? '').trim());
    for (const label of labels) {
      expect(label.length).toBeGreaterThan(0);
    }
    expect(new Set(labels)).toEqual(new Set(cases.map((c) => c.expected)));
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
      // status 'completed' → overflow trigger visible, stop + resume are not.
      const overflow = host.querySelector('button.overflow-button');
      expect(overflow?.getAttribute('aria-label')).toBe('More actions Blogging Blog pipeline job for Sample Post');
    });
  });

  it('refresh sets loading and restarts polling', () => {
    component.refresh();
    expect(component.loading).toBe(true);
  });

  it('trackByJobId returns composite key', () => {
    const job = { unified: { source: 'se', jobId: 'j1' } } as any;
    expect(component.trackByJobId(0, job)).toBe('se::j1');
  });

  describe('stuck-job detection', () => {
    const STUCK_MS = 2 * 60 * 60 * 1000;
    const STILL_MS = 30 * 60 * 1000;
    const PAST_STILL_MS = STILL_MS + 60_000;
    const oldCreatedAt = () => new Date(Date.now() - STUCK_MS - 60_000).toISOString();
    const youngCreatedAt = () => new Date(Date.now() - 60_000).toISOString();
    const waitPastStillness = () => vi.setSystemTime(new Date(Date.now() + PAST_STILL_MS));

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

    it('is false after one unchanged poll, true after the second once stillness elapses', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
      // Even a second unchanged poll arriving 40s later must not flip the
      // row to "stuck" — the stillness gate guards against that.
      vi.setSystemTime(new Date(Date.now() + 40_000));
      record([job]);
      expect(component.isStuck(job)).toBe(false);
      waitPastStillness();
      expect(component.isStuck(job)).toBe(true);
    });

    it('is not stuck within STUCK_STILL_DURATION_MS of the last signal change', () => {
      const old = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([old]);
      record([old]); // sampleCount = 2 but stillness ≈ 0
      expect(component.isStuck(old)).toBe(false);
      // 29 minutes — still under the 30-minute stillness threshold.
      vi.setSystemTime(new Date(Date.now() + STILL_MS - 60_000));
      record([old]);
      expect(component.isStuck(old)).toBe(false);
    });

    it('flips to stuck exactly at the stillness boundary', () => {
      const old = makeJob('running', 'j1', oldCreatedAt(), 25);
      record([old]);
      // Just under the boundary → still not stuck.
      vi.setSystemTime(new Date(Date.now() + STILL_MS - 1));
      record([old]);
      expect(component.isStuck(old)).toBe(false);
      // Bump exactly to the boundary → stuck (the gate is `>=`).
      vi.setSystemTime(new Date(Date.now() + 1));
      expect(component.isStuck(old)).toBe(true);
    });

    it('resets when progress changes', () => {
      const a = makeJob('running', 'j1', oldCreatedAt(), 25);
      const b = makeJob('running', 'j1', oldCreatedAt(), 35);
      record([a]);
      waitPastStillness();
      record([a]);
      expect(component.isStuck(a)).toBe(true);
      record([b]);
      expect(component.isStuck(b)).toBe(false);
    });

    it('uses firstSeenAt as age basis when createdAt is missing', () => {
      const job = makeJob('running', 'j1', '', 10);
      record([job]);
      record([job]);
      // Two polls fired at the same fake-clock instant → firstSeenAt == now,
      // age == 0, so still not stuck.
      expect(component.isStuck(job)).toBe(false);
    });

    it('flags a Planning-V3-style job (no createdAt) after enough observed-poll time', () => {
      // Planning V3 list endpoint omits created_at, so the row arrives with
      // createdAt undefined. The dashboard should still be able to flag it
      // stuck once it has observed the job frozen for STUCK_THRESHOLD_MS+.
      const makePV3 = (phase: string) => {
        const job = makeJob('running', 'pv3-1', undefined as unknown as string, null);
        job.unified.source = 'planning_v3';
        job.unified.phase = phase;
        return job;
      };
      record([makePV3('drafting')]);
      // Advance past the threshold while keeping the signal stable.
      vi.setSystemTime(new Date(Date.now() + STUCK_MS + 60_000));
      record([makePV3('drafting')]);
      expect(component.isStuck(makePV3('drafting'))).toBe(true);
    });

    it('does not flag a Planning-V3 job whose phase is still advancing', () => {
      const makePV3 = (phase: string) => {
        const job = makeJob('running', 'pv3-1', undefined as unknown as string, null);
        job.unified.source = 'planning_v3';
        job.unified.phase = phase;
        return job;
      };
      record([makePV3('drafting')]);
      vi.setSystemTime(new Date(Date.now() + STUCK_MS + 60_000));
      record([makePV3('reviewing')]);
      // Signal changed → sampleCount resets to 1, firstSeenAt resets to now,
      // so the age basis is fresh and the row is not flagged.
      expect(component.isStuck(makePV3('reviewing'))).toBe(false);
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
      waitPastStillness();
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
      waitPastStillness();
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
      // And on a later poll where it's still unchanged and the stillness
      // duration has elapsed, the running streak satisfies all gates → stuck
      // (so the heuristic still works post-resume).
      waitPastStillness();
      record([resumed]);
      expect(component.isStuck(resumed)).toBe(true);
    });

    it('does not flag an SE row whose detail fetch failed (no observable signal)', () => {
      // fetchSEDetail returned null → seDetail is undefined, progress is null,
      // phase and statusText are empty. Repeated polls in that state must
      // not trip the stuck heuristic, even past the age gate.
      const job = makeJob('running', 'j1', oldCreatedAt(), null);
      job.seDetail = undefined;
      delete (job.unified as any).phase;
      record([job]);
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('excludes SE jobs waiting for answers even after threshold elapsed', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.seDetail = { waitingForAnswers: true, progress: 25 } as any;
      record([job]);
      record([job]);
      expect(component.isStuck(job)).toBe(false);
    });

    it('does not flag a run_team SE job whose per-team currentTaskId advances', () => {
      // run_team orchestrator ticks team_progress.current_task_id / microtask
      // counters while job-level progress/phase stay constant for long
      // stretches. The fingerprint must capture that motion.
      const makeRunTeam = (taskId: string, completed: number) => {
        const job = makeJob('running', 'rt-1', oldCreatedAt(), 40);
        job.unified.jobType = 'run_team';
        job.seDetail = {
          progress: 40,
          statusText: 'executing',
          currentPhase: 'execution',
          teamStatuses: [
            {
              teamId: 'backend',
              label: 'Backend',
              icon: 'dns',
              phase: 'coding',
              phaseLabel: 'Coding',
              isActive: true,
              currentTaskId: taskId,
              microtasksCompleted: completed,
            },
          ],
        } as any;
        return job;
      };
      record([makeRunTeam('task-a', 1)]);
      waitPastStillness();
      record([makeRunTeam('task-b', 2)]);
      // Per-team task advanced → signal flipped → not stuck.
      expect(component.isStuck(makeRunTeam('task-b', 2))).toBe(false);
    });

    it('does not flag a run_team SE job whose microtask phase advances within one task', () => {
      // Within a single task the orchestrator ticks current_microtask /
      // current_microtask_phase / current_microtask_index / phase_detail
      // long before microtasks_completed changes. The fingerprint must
      // capture that motion.
      const makeRunTeam = (microPhase: string, idx: number) => {
        const job = makeJob('running', 'rt-1', oldCreatedAt(), 40);
        job.unified.jobType = 'run_team';
        job.seDetail = {
          progress: 40,
          statusText: 'executing',
          currentPhase: 'execution',
          teamStatuses: [
            {
              teamId: 'backend',
              label: 'Backend',
              icon: 'dns',
              phase: 'coding',
              phaseLabel: 'Coding',
              isActive: true,
              currentTaskId: 'task-a',
              microtasksCompleted: 0,
              currentMicrotask: 'mt-1',
              currentMicrotaskPhase: microPhase,
              currentMicrotaskIndex: idx,
              phaseDetail: `mt:${idx} ${microPhase}`,
            },
          ],
        } as any;
        return job;
      };
      record([makeRunTeam('coding', 0)]);
      waitPastStillness();
      record([makeRunTeam('review', 0)]);
      // Microtask phase advanced → signal flipped → not stuck.
      expect(component.isStuck(makeRunTeam('review', 0))).toBe(false);
    });

    it('flags a run_team SE job whose per-team progress is frozen', () => {
      const makeRunTeam = () => {
        const job = makeJob('running', 'rt-1', oldCreatedAt(), 40);
        job.unified.jobType = 'run_team';
        job.seDetail = {
          progress: 40,
          statusText: 'executing',
          currentPhase: 'execution',
          teamStatuses: [
            {
              teamId: 'backend',
              label: 'Backend',
              icon: 'dns',
              phase: 'coding',
              phaseLabel: 'Coding',
              isActive: true,
              currentTaskId: 'task-a',
              microtasksCompleted: 1,
            },
          ],
        } as any;
        return job;
      };
      record([makeRunTeam()]);
      waitPastStillness();
      record([makeRunTeam()]);
      expect(component.isStuck(makeRunTeam())).toBe(true);
    });

    it('renders the stuck glyph with aria-hidden="false" and a non-empty aria-label', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.unified.jobType = 'run_team';
      job.unified.repoPath = '/work/payments-api';
      record([job]);
      waitPastStillness();
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

    it('renders the row aria-label with "appears stuck" once the row is stuck', () => {
      const job = makeJob('running', 'j1', oldCreatedAt(), 25);
      job.unified.jobType = 'run_team';
      job.unified.repoPath = '/work/payments-api';
      record([job]);
      waitPastStillness();
      record([job]);
      component.jobs = [job];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      const row = host.querySelector('tr.job-row') as HTMLElement | null;
      expect(row).toBeTruthy();
      expect(row?.getAttribute('aria-label')).toContain('appears stuck');
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
      expect(component.canRestartJob(makeJob('completed_with_failures'))).toBe(true);
      expect(component.canRestartJob(makeJob('already_complete'))).toBe(true);
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
        buildRow({ jobId: 'g', status: 'completed_with_failures', source: 'software_engineering' }),
        buildRow({ jobId: 'a', status: 'already_complete', source: 'software_engineering' }),
      ];
      const counts = component.statusCounts;
      expect(counts.completed).toBe(6);
      expect(counts.active).toBe(0);
      expect(counts.failed).toBe(0);
      expect(counts.all).toBe(6);
    });

    it('completed_with_failures (Coding Team partial success) buckets as completed, not active', () => {
      component.jobs = [buildRow({ jobId: 'g', status: 'completed_with_failures' })];
      const counts = component.statusCounts;
      expect(counts.completed).toBe(1);
      expect(counts.active).toBe(0);
    });

    it('already_complete (Coding Team "work already done") buckets as completed, not active', () => {
      component.jobs = [buildRow({ jobId: 'a', status: 'already_complete' })];
      const counts = component.statusCounts;
      expect(counts.completed).toBe(1);
      expect(counts.active).toBe(0);
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

  it('renders muted middle-dot in both progress and agents cells when data is absent', () => {
    component.jobs = [
      {
        unified: {
          source: 'blogging',
          jobId: 'empty-1',
          status: 'pending',
          createdAt: new Date().toISOString(),
          label: 'Draft post',
        },
        seDetail: undefined,
      } as any,
    ];
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const empties = host.querySelectorAll('.cell-empty');
    expect(empties.length).toBe(2);
    for (const el of Array.from(empties)) {
      expect(el.textContent?.trim()).toBe('·');
    }
    expect(host.querySelectorAll('.progress-na').length).toBe(0);
    expect(host.querySelectorAll('.agents-na').length).toBe(0);
  });

  describe('hasOverflowActions', () => {
    const makeJob = (status: string): any => ({
      unified: { source: 'blogging', jobId: 'j1', status },
      seDetail: undefined,
    });

    it('returns true for terminal statuses (restart or delete available)', () => {
      expect(component.hasOverflowActions(makeJob('completed'))).toBe(true);
      expect(component.hasOverflowActions(makeJob('failed'))).toBe(true);
      expect(component.hasOverflowActions(makeJob('cancelled'))).toBe(true);
      expect(component.hasOverflowActions(makeJob('interrupted'))).toBe(true);
      expect(component.hasOverflowActions(makeJob('agent_crash'))).toBe(true);
    });

    it('returns false for running/pending (no restart or delete)', () => {
      expect(component.hasOverflowActions(makeJob('running'))).toBe(false);
      expect(component.hasOverflowActions(makeJob('pending'))).toBe(false);
    });
  });

  describe('overflow menu', () => {
    const makeRow = (status: string, source = 'blogging'): any => ({
      unified: {
        source,
        jobId: 'j1',
        status,
        label: 'Test Job',
        createdAt: new Date().toISOString(),
      },
      seDetail: undefined,
    });

    it('renders overflow trigger for completed status', () => {
      component.jobs = [makeRow('completed')];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.overflow-button')).toBeTruthy();
    });

    it('renders overflow trigger for failed status', () => {
      component.jobs = [makeRow('failed')];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.overflow-button')).toBeTruthy();
    });

    it('does not render overflow trigger for running jobs', () => {
      component.jobs = [makeRow('running')];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.overflow-button')).toBeNull();
    });

    it('does not render overflow trigger for pending jobs', () => {
      component.jobs = [makeRow('pending')];
      fixture.detectChanges();
      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.overflow-button')).toBeNull();
    });

    it('running jobs show only Stop inline, no overflow', () => {
      component.jobs = [makeRow('running')];
      fixture.detectChanges();
      const actions = fixture.nativeElement.querySelector('td.col-actions');
      expect(actions?.querySelectorAll('button.stop-button').length).toBe(1);
      expect(actions?.querySelectorAll('button.resume-button').length).toBe(0);
      expect(actions?.querySelectorAll('button.overflow-button').length).toBe(0);
    });

    it('failed jobs show Resume inline plus overflow', () => {
      component.jobs = [makeRow('failed')];
      fixture.detectChanges();
      const actions = fixture.nativeElement.querySelector('td.col-actions');
      expect(actions?.querySelectorAll('button.stop-button').length).toBe(0);
      expect(actions?.querySelectorAll('button.resume-button').length).toBe(1);
      expect(actions?.querySelectorAll('button.overflow-button').length).toBe(1);
    });

    it('completed jobs show only overflow trigger, no inline primary action', () => {
      component.jobs = [makeRow('completed')];
      fixture.detectChanges();
      const actions = fixture.nativeElement.querySelector('td.col-actions');
      expect(actions?.querySelectorAll('button.stop-button').length).toBe(0);
      expect(actions?.querySelectorAll('button.resume-button').length).toBe(0);
      expect(actions?.querySelectorAll('button.overflow-button').length).toBe(1);
    });

    it('overflow trigger click stops event propagation', () => {
      component.jobs = [makeRow('completed')];
      fixture.detectChanges();
      const overflowBtn = fixture.nativeElement.querySelector('.overflow-button') as HTMLElement;
      const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
      const stopSpy = vi.spyOn(clickEvent, 'stopPropagation');
      overflowBtn.dispatchEvent(clickEvent);
      expect(stopSpy).toHaveBeenCalled();
    });

    it('overflow menu contains Restart and Delete items for completed status', () => {
      component.jobs = [makeRow('completed')];
      fixture.detectChanges();
      const overflowBtn = fixture.nativeElement.querySelector('.overflow-button') as HTMLElement;
      overflowBtn.click();
      fixture.detectChanges();
      const overlay = document.querySelector('.cdk-overlay-container');
      const items = Array.from(overlay?.querySelectorAll('[mat-menu-item]') ?? []);
      const spans = items.map((el) => el.querySelector('span')?.textContent?.trim());
      expect(spans).toContain('Restart');
      expect(spans).toContain('Delete');
    });

    it('clicking Restart menu item triggers restartJob with confirmation dialog', () => {
      dialogSpy.open.mockReturnValue({ afterClosed: () => of(true) });
      component.jobs = [makeRow('failed', 'software_engineering')];
      fixture.detectChanges();
      const overflowBtn = fixture.nativeElement.querySelector('.overflow-button') as HTMLElement;
      overflowBtn.click();
      fixture.detectChanges();
      const overlay = document.querySelector('.cdk-overlay-container');
      const items = Array.from(overlay?.querySelectorAll('[mat-menu-item]') ?? []);
      const restartItem = items.find((el) => el.querySelector('span')?.textContent?.trim() === 'Restart') as HTMLElement;
      restartItem?.click();
      fixture.detectChanges();
      expect(dialogSpy.open).toHaveBeenCalled();
      expect(jobActionsSpy.restart).toHaveBeenCalledWith('software_engineering', 'j1');
    });

    it('clicking Delete menu item triggers deleteJob with confirmation dialog', () => {
      dialogSpy.open.mockReturnValue({ afterClosed: () => of(true) });
      component.jobs = [makeRow('completed')];
      fixture.detectChanges();
      const overflowBtn = fixture.nativeElement.querySelector('.overflow-button') as HTMLElement;
      overflowBtn.click();
      fixture.detectChanges();
      const overlay = document.querySelector('.cdk-overlay-container');
      const items = Array.from(overlay?.querySelectorAll('[mat-menu-item]') ?? []);
      const deleteItem = items.find((el) => el.querySelector('span')?.textContent?.trim() === 'Delete') as HTMLElement;
      deleteItem?.click();
      fixture.detectChanges();
      expect(dialogSpy.open).toHaveBeenCalled();
      expect(jobActionsSpy.delete).toHaveBeenCalledWith('blogging', 'j1');
    });
  });

  describe('live-refresh indicator', () => {
    it('renders a pulsing live-dot next to the timestamp once lastUpdated is set', () => {
      component.lastUpdated = new Date();
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      const lastUpdated = host.querySelector('.last-updated');
      expect(lastUpdated).not.toBeNull();
      // ng-reflect-message is set by MatTooltip in dev builds. Use a substring
      // match — Angular's reflect-attribute serialization can drop trailing
      // characters across runtimes, so anchoring on the stable prefix is more
      // robust than a full string compare.
      expect(lastUpdated?.getAttribute('ng-reflect-message') ?? '').toContain('Auto-refreshes every 20 second');

      const dot = host.querySelector('.last-updated .live-dot');
      expect(dot).not.toBeNull();
      expect(dot?.classList.contains('is-paused')).toBe(false);
      expect(dot?.getAttribute('aria-hidden')).toBe('true');
    });

    it('marks the live-dot as paused when the component is in an error state', () => {
      component.lastUpdated = new Date();
      component.error = 'boom';
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      const dot = host.querySelector('.last-updated .live-dot');
      expect(dot).not.toBeNull();
      expect(dot?.classList.contains('is-paused')).toBe(true);
      expect(component.isPollingActive).toBe(false);
    });
  });

  describe('visibility-based polling', () => {
    afterEach(() => {
      // Remove the per-test instance override so document.hidden falls back to its
      // prototype getter (false) for later tests.
      delete (document as unknown as { hidden?: boolean }).hidden;
    });

    function setHidden(hidden: boolean): void {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
      document.dispatchEvent(new Event('visibilitychange'));
    }

    it('suspends polling while the tab is hidden and resumes when visible', () => {
      // ngOnInit (shared beforeEach detectChanges) started polling.
      expect(component['pollSub']).not.toBeNull();

      setHidden(true);
      expect(component['pollSub']).toBeNull(); // polling fully suspended, not just dimmed
      expect(component.isPollingActive).toBe(false);

      setHidden(false);
      expect(component['pollSub']).not.toBeNull(); // resumed with an immediate refresh
      expect(component.isPollingActive).toBe(true);
    });
  });

  describe('table refresh progress bar', () => {
    const row = (jobId: string) =>
      ({
        unified: {
          source: 'software_engineering',
          jobId,
          status: 'running',
          createdAt: new Date().toISOString(),
          label: 'Run',
          progress: 50,
        },
        seDetail: undefined,
      }) as any;

    it('renders an indeterminate progress bar at the top of the table when loading is true', () => {
      component.jobs = [row('j1')];
      component.loading = true;
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      const bar = host.querySelector('.jobs-table-container .table-refresh-bar');
      expect(bar).toBeTruthy();
      expect(bar?.getAttribute('mode')).toBe('indeterminate');
      expect(bar?.getAttribute('role')).toBe('progressbar');
    });

    it('does not render the refresh bar when loading is false', () => {
      component.jobs = [row('j1')];
      component.loading = false;
      fixture.detectChanges();

      const bar = fixture.nativeElement.querySelector('.jobs-table-container .table-refresh-bar');
      expect(bar).toBeNull();
    });

    it('bar disappears after loading flips from true to false', () => {
      component.jobs = [row('j1')];
      component.loading = true;
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.table-refresh-bar')).toBeTruthy();

      component.loading = false;
      fixture.detectChanges();

      expect(host.querySelector('.table-refresh-bar')).toBeNull();
    });

    it('does not render the refresh bar when no jobs exist', () => {
      component.jobs = [];
      component.loading = true;
      fixture.detectChanges();

      const host = fixture.nativeElement as HTMLElement;
      expect(host.querySelector('.jobs-table-container')).toBeNull();
      expect(host.querySelector('.table-refresh-bar')).toBeNull();
      expect(host.querySelector('.loading-state')).toBeTruthy();
    });
  });
});
