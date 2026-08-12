import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach, afterEach } from 'vitest';
import { AgentProvisioningApiService } from '../../../services/agent-provisioning-api.service';
import { AgentProvisioningDashboardComponent } from './agent-provisioning-dashboard.component';

interface ApiStub {
  healthCheck: ReturnType<typeof vi.fn>;
  startProvisioning: ReturnType<typeof vi.fn>;
  listJobs: ReturnType<typeof vi.fn>;
  listAgents: ReturnType<typeof vi.fn>;
  getJobStatus: ReturnType<typeof vi.fn>;
  deprovisionAgent: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  deleteJob: ReturnType<typeof vi.fn>;
}

describe('AgentProvisioningDashboardComponent (extra coverage)', () => {
  let api: ApiStub;
  let queryParams$: Subject<Record<string, string>>;
  let fixture: ComponentFixture<AgentProvisioningDashboardComponent>;
  let component: AgentProvisioningDashboardComponent;

  beforeEach(async () => {
    queryParams$ = new Subject();
    api = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      startProvisioning: vi.fn().mockReturnValue(of({ job_id: 'j-new' })),
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listAgents: vi.fn().mockReturnValue(of({ agents: [] })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed', current_phase: 'install' })),
      deprovisionAgent: vi.fn().mockReturnValue(of({})),
      cancelJob: vi.fn().mockReturnValue(of({})),
      deleteJob: vi.fn().mockReturnValue(of({})),
    };

    await TestBed.configureTestingModule({
      imports: [AgentProvisioningDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: AgentProvisioningApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(AgentProvisioningDashboardComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  // ---------------------------------------------------------------------
  // Lifecycle / init
  // ---------------------------------------------------------------------

  it('initialises with default form and tabs', () => {
    fixture.detectChanges();
    expect(component.selectedTabIndex).toBe(0);
    expect(component.activeTab).toBe('provision');
    expect(component.provisionForm.value.manifest_path).toBe('default.yaml');
    expect(api.listJobs).toHaveBeenCalled();
    expect(api.listAgents).toHaveBeenCalled();
  });

  it('handles jobId query param by viewing job after jobs load', () => {
    api.listJobs.mockReturnValue(of({ jobs: [{ job_id: 'j-target' }] }));
    fixture.detectChanges();
    queryParams$.next({ jobId: 'j-target' });
    // Reload to pick up pending id
    component.loadJobs();
    expect(api.getJobStatus).toHaveBeenCalledWith('j-target');
  });

  // ---------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------

  it('onTabChange maps indices to tabs and triggers loaders', () => {
    fixture.detectChanges();
    api.listJobs.mockClear();
    api.listAgents.mockClear();
    component.onTabChange(1);
    expect(component.activeTab).toBe('jobs');
    expect(api.listJobs).toHaveBeenCalled();
    component.onTabChange(2);
    expect(component.activeTab).toBe('environments');
    expect(api.listAgents).toHaveBeenCalled();
    component.onTabChange(99);
    expect(component.activeTab).toBe('provision');
  });

  // ---------------------------------------------------------------------
  // Provision submission
  // ---------------------------------------------------------------------

  it('onSubmitProvision skips when form is invalid', () => {
    fixture.detectChanges();
    component.provisionForm.reset();
    component.onSubmitProvision();
    expect(api.startProvisioning).not.toHaveBeenCalled();
  });

  it('onSubmitProvision handles success and starts polling', () => {
    fixture.detectChanges();
    component.provisionForm.patchValue({ agent_id: 'a1' });
    component.onSubmitProvision();
    expect(api.startProvisioning).toHaveBeenCalled();
    expect(component.currentJobId).toBe('j-new');
    expect(component.submitting).toBe(false);
  });

  it('onSubmitProvision sets submitError on failure', () => {
    fixture.detectChanges();
    component.provisionForm.patchValue({ agent_id: 'a1' });
    api.startProvisioning.mockReturnValue(throwError(() => ({ error: { detail: 'no' } })));
    component.onSubmitProvision();
    expect(component.submitError).toBe('no');
    expect(component.submitting).toBe(false);
  });

  it('onSubmitProvision uses default manifest if empty', () => {
    fixture.detectChanges();
    component.provisionForm.patchValue({ agent_id: 'a', manifest_path: '' });
    component.onSubmitProvision();
    const arg = api.startProvisioning.mock.calls[0][0];
    expect(arg.manifest_path).toBe('default.yaml');
  });

  // ---------------------------------------------------------------------
  // Job and agent loaders error paths
  // ---------------------------------------------------------------------

  it('loadJobs handles error', () => {
    api.listJobs.mockReturnValue(throwError(() => new Error('x')));
    fixture.detectChanges();
    expect(component.jobsLoading).toBe(false);
  });

  it('loadAgents handles error', () => {
    api.listAgents.mockReturnValue(throwError(() => new Error('x')));
    fixture.detectChanges();
    expect(component.agentsLoading).toBe(false);
  });

  // ---------------------------------------------------------------------
  // Deprovision
  // ---------------------------------------------------------------------

  it('deprovisionAgent requires confirm; reloads on success', () => {
    fixture.detectChanges();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deprovisionAgent('a1');
    expect(api.deprovisionAgent).not.toHaveBeenCalled();

    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.listAgents.mockClear();
    component.deprovisionAgent('a1');
    expect(api.deprovisionAgent).toHaveBeenCalledWith('a1');
    expect(api.listAgents).toHaveBeenCalled();
  });

  it('deprovisionAgent handles error gracefully', () => {
    fixture.detectChanges();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.deprovisionAgent.mockReturnValue(throwError(() => new Error('x')));
    component.deprovisionAgent('a1');
  });

  // ---------------------------------------------------------------------
  // viewJobStatus / startJobPolling
  // ---------------------------------------------------------------------

  it('viewJobStatus loads status and starts polling for running jobs', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    fixture.detectChanges();
    component.viewJobStatus('j1');
    expect(component.currentJobId).toBe('j1');
    expect(component.currentJobStatus?.status).toBe('running');
    expect(component['jobPollSub']).toBeTruthy();
  });

  it('viewJobStatus stays without polling for completed jobs', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
    fixture.detectChanges();
    component.viewJobStatus('j1');
    expect(component['jobPollSub']).toBeNull();
  });

  it('viewJobStatus handles error', () => {
    fixture.detectChanges();
    api.getJobStatus.mockReturnValue(throwError(() => new Error('boom')));
    component.viewJobStatus('j1');
  });

  // ---------------------------------------------------------------------
  // clearCurrentJob / canStop / stop / delete
  // ---------------------------------------------------------------------

  it('clearCurrentJob resets state and form', () => {
    fixture.detectChanges();
    component.currentJobId = 'x';
    component.currentJobStatus = { job_id: 'x', status: 'running' } as never;
    component.clearCurrentJob();
    expect(component.currentJobId).toBeNull();
    expect(component.currentJobStatus).toBeNull();
    expect(component.provisionForm.value.manifest_path).toBe('default.yaml');
  });

  it('canStopCurrentJob true for pending/running', () => {
    fixture.detectChanges();
    expect(component.canStopCurrentJob).toBe(false);
    component.currentJobStatus = { status: 'pending' } as never;
    expect(component.canStopCurrentJob).toBe(true);
    component.currentJobStatus = { status: 'completed' } as never;
    expect(component.canStopCurrentJob).toBe(false);
  });

  it('stopCurrentJob no-ops without current job', () => {
    fixture.detectChanges();
    component.currentJobId = null;
    component.stopCurrentJob();
    expect(api.cancelJob).not.toHaveBeenCalled();
  });

  it('stopCurrentJob cancels and refreshes status', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'cancelled' }));
    component.stopCurrentJob();
    expect(api.cancelJob).toHaveBeenCalledWith('j1');
    expect(component.currentJobStatus?.status).toBe('cancelled');
  });

  it('stopCurrentJob handles error', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.cancelJob.mockReturnValue(throwError(() => ({ error: { detail: 'cancel-fail' } })));
    component.stopCurrentJob();
    expect(component.submitError).toBe('cancel-fail');
  });

  it('deleteCurrentJob no-ops without confirm', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteCurrentJob();
    expect(api.deleteJob).not.toHaveBeenCalled();
  });

  it('deleteCurrentJob no-ops without current job', () => {
    fixture.detectChanges();
    component.currentJobId = null;
    component.deleteCurrentJob();
    expect(api.deleteJob).not.toHaveBeenCalled();
  });

  it('deleteCurrentJob deletes and clears state', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteCurrentJob();
    expect(api.deleteJob).toHaveBeenCalledWith('j1');
    expect(component.currentJobId).toBeNull();
  });

  it('deleteCurrentJob handles error', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.deleteJob.mockReturnValue(throwError(() => ({ message: 'del-fail' })));
    component.deleteCurrentJob();
    expect(component.submitError).toBe('del-fail');
  });

  // ---------------------------------------------------------------------
  // Phase helpers
  // ---------------------------------------------------------------------

  it('getPhaseLabel returns label or id fallback', () => {
    fixture.detectChanges();
    const phase = component.phases[0];
    expect(component.getPhaseLabel(phase.id)).toBe(phase.label);
    expect(component.getPhaseLabel('not-a-phase')).toBe('not-a-phase');
  });

  it('isPhaseCompleted / isPhaseCurrent', () => {
    fixture.detectChanges();
    component.currentJobStatus = { completed_phases: ['p1'], current_phase: 'p2' } as never;
    expect(component.isPhaseCompleted('p1')).toBe(true);
    expect(component.isPhaseCompleted('p2')).toBe(false);
    expect(component.isPhaseCurrent('p2')).toBe(true);
    expect(component.isPhaseCurrent('p1')).toBe(false);
  });

  it('isJobActive / isJobComplete / isJobFailed', () => {
    fixture.detectChanges();
    component.currentJobStatus = { status: 'running' } as never;
    expect(component.isJobActive).toBe(true);
    expect(component.isJobComplete).toBe(false);
    expect(component.isJobFailed).toBe(false);
    component.currentJobStatus = { status: 'completed' } as never;
    expect(component.isJobComplete).toBe(true);
    component.currentJobStatus = { status: 'failed' } as never;
    expect(component.isJobFailed).toBe(true);
  });

  // ---------------------------------------------------------------------
  // Polling cleanup
  // ---------------------------------------------------------------------

  it('ngOnDestroy unsubscribes', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    component['startJobPolling']('j1');
    const sub = component['jobPollSub'];
    expect(sub).toBeTruthy();
    const spy = vi.spyOn(sub!, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
  });
});
