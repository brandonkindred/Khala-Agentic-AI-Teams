import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach, afterEach } from 'vitest';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AISystemsDashboardComponent } from './ai-systems-dashboard.component';

interface ApiStub {
  healthCheck: ReturnType<typeof vi.fn>;
  startBuild: ReturnType<typeof vi.fn>;
  listJobs: ReturnType<typeof vi.fn>;
  listBlueprints: ReturnType<typeof vi.fn>;
  getBlueprint: ReturnType<typeof vi.fn>;
  getJobStatus: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  deleteJob: ReturnType<typeof vi.fn>;
}

describe('AISystemsDashboardComponent (extra coverage)', () => {
  let api: ApiStub;
  let queryParams$: Subject<Record<string, string>>;
  let fixture: ComponentFixture<AISystemsDashboardComponent>;
  let component: AISystemsDashboardComponent;

  beforeEach(async () => {
    queryParams$ = new Subject();
    api = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      startBuild: vi.fn().mockReturnValue(of({ job_id: 'j-new' })),
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listBlueprints: vi.fn().mockReturnValue(of({ blueprints: ['bp1', 'bp2'] })),
      getBlueprint: vi.fn().mockReturnValue(of({ project_name: 'bp1' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed' })),
      cancelJob: vi.fn().mockReturnValue(of({})),
      deleteJob: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [AISystemsDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: AISystemsApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(AISystemsDashboardComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => TestBed.resetTestingModule());

  it('initialises and loads jobs + blueprints', () => {
    fixture.detectChanges();
    expect(component.activeTab).toBe('build');
    expect(api.listJobs).toHaveBeenCalled();
    expect(api.listBlueprints).toHaveBeenCalled();
    expect(component.blueprintNames).toEqual(['bp1', 'bp2']);
  });

  it('handles jobId query param after jobs load', () => {
    api.listJobs.mockReturnValue(of({ jobs: [{ job_id: 'j-target' }] }));
    fixture.detectChanges();
    queryParams$.next({ jobId: 'j-target' });
    component.loadJobs();
    expect(api.getJobStatus).toHaveBeenCalledWith('j-target');
  });

  it('healthCheck delegates to api', () => {
    fixture.detectChanges();
    component.healthCheck().subscribe();
    expect(api.healthCheck).toHaveBeenCalled();
  });

  it('onTabChange handles all tab indices', () => {
    fixture.detectChanges();
    api.listJobs.mockClear();
    api.listBlueprints.mockClear();
    component.onTabChange(1);
    expect(component.activeTab).toBe('jobs');
    expect(api.listJobs).toHaveBeenCalled();
    component.onTabChange(2);
    expect(component.activeTab).toBe('blueprints');
    expect(api.listBlueprints).toHaveBeenCalled();
    component.onTabChange(0);
    expect(component.activeTab).toBe('build');
    component.onTabChange(99);
    expect(component.activeTab).toBe('build');
  });

  it('onSubmitBuild skips invalid form', () => {
    fixture.detectChanges();
    component.onSubmitBuild();
    expect(api.startBuild).not.toHaveBeenCalled();
  });

  it('onSubmitBuild submits valid form and starts polling', () => {
    fixture.detectChanges();
    component.buildForm.patchValue({ project_name: 'p', spec_path: 's.yaml' });
    component.onSubmitBuild();
    expect(api.startBuild).toHaveBeenCalled();
    expect(component.currentJobId).toBe('j-new');
  });

  it('onSubmitBuild handles error', () => {
    fixture.detectChanges();
    component.buildForm.patchValue({ project_name: 'p', spec_path: 's.yaml' });
    api.startBuild.mockReturnValue(throwError(() => ({ error: { detail: 'bad' } })));
    component.onSubmitBuild();
    expect(component.submitError).toBe('bad');
  });

  it('onAssistantLaunched starts polling for valid job_id', () => {
    fixture.detectChanges();
    component.onAssistantLaunched({ job_id: 'jx', conversation_id: 'c' });
    expect(component.currentJobId).toBe('jx');
    component.onAssistantLaunched({ job_id: null, conversation_id: 'c' });
  });

  it('loadJobs handles error', () => {
    api.listJobs.mockReturnValue(throwError(() => new Error('x')));
    fixture.detectChanges();
    expect(component.jobsLoading).toBe(false);
  });

  it('loadBlueprintNames handles error', () => {
    api.listBlueprints.mockReturnValue(throwError(() => new Error('x')));
    fixture.detectChanges();
    expect(component.blueprintsLoading).toBe(false);
  });

  it('loadBlueprint handles success and error', () => {
    fixture.detectChanges();
    component.loadBlueprint('bp1');
    expect(api.getBlueprint).toHaveBeenCalledWith('bp1');
    expect(component.selectedBlueprint?.project_name).toBe('bp1');

    api.getBlueprint.mockReturnValue(throwError(() => new Error('x')));
    component.loadBlueprint('bp2');
    expect(component.blueprintLoading).toBe(false);
  });

  it('viewJobStatus loads status and polls for running jobs', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    fixture.detectChanges();
    component.viewJobStatus('j1');
    expect(component.currentJobId).toBe('j1');
    expect(component['jobPollSub']).toBeTruthy();
  });

  it('viewJobStatus skips polling for completed jobs', () => {
    fixture.detectChanges();
    component.viewJobStatus('j1');
    expect(component['jobPollSub']).toBeNull();
  });

  it('viewJobStatus handles error', () => {
    fixture.detectChanges();
    api.getJobStatus.mockReturnValue(throwError(() => new Error('x')));
    component.viewJobStatus('j1');
  });

  it('clearCurrentJob resets state', () => {
    fixture.detectChanges();
    component.currentJobId = 'x';
    component.currentJobStatus = { status: 'running' } as never;
    component.clearCurrentJob();
    expect(component.currentJobId).toBeNull();
  });

  it('canStopCurrentJob respects status', () => {
    fixture.detectChanges();
    expect(component.canStopCurrentJob).toBe(false);
    component.currentJobStatus = { status: 'running' } as never;
    expect(component.canStopCurrentJob).toBe(true);
  });

  it('stopCurrentJob no-ops without id', () => {
    fixture.detectChanges();
    component.stopCurrentJob();
    expect(api.cancelJob).not.toHaveBeenCalled();
  });

  it('stopCurrentJob cancels and reloads status', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'cancelled' }));
    component.stopCurrentJob();
    expect(api.cancelJob).toHaveBeenCalledWith('j1');
    expect(component.currentJobStatus?.status).toBe('cancelled');
  });

  it('stopCurrentJob handles cancel error', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.cancelJob.mockReturnValue(throwError(() => ({ error: { detail: 'cancel' } })));
    component.stopCurrentJob();
    expect(component.submitError).toBe('cancel');
  });

  it('deleteCurrentJob no-ops without id or confirm', () => {
    fixture.detectChanges();
    component.currentJobId = null;
    component.deleteCurrentJob();
    expect(api.deleteJob).not.toHaveBeenCalled();
    component.currentJobId = 'j1';
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteCurrentJob();
    expect(api.deleteJob).not.toHaveBeenCalled();
  });

  it('deleteCurrentJob deletes on confirm and clears state', () => {
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
    api.deleteJob.mockReturnValue(throwError(() => ({ message: 'del fail' })));
    component.deleteCurrentJob();
    expect(component.submitError).toBe('del fail');
  });

  it('phase helpers compute correctly', () => {
    fixture.detectChanges();
    const phase = component.phases[0];
    expect(component.getPhaseLabel(phase.id)).toBe(phase.label);
    expect(component.getPhaseLabel('unknown')).toBe('unknown');
    component.currentJobStatus = { completed_phases: ['a'], current_phase: 'b' } as never;
    expect(component.isPhaseCompleted('a')).toBe(true);
    expect(component.isPhaseCurrent('b')).toBe(true);
    expect(component.isPhaseCurrent('a')).toBe(false);
  });

  it('isJobActive/Complete/Failed reflect status', () => {
    fixture.detectChanges();
    component.currentJobStatus = { status: 'running' } as never;
    expect(component.isJobActive).toBe(true);
    component.currentJobStatus = { status: 'completed' } as never;
    expect(component.isJobComplete).toBe(true);
    component.currentJobStatus = { status: 'failed' } as never;
    expect(component.isJobFailed).toBe(true);
  });

  it('ngOnDestroy cleans up subs', () => {
    fixture.detectChanges();
    component.currentJobId = 'j1';
    api.getJobStatus.mockReturnValue(of({ status: 'running' }));
    component['startJobPolling']('j1');
    component.ngOnDestroy();
  });
});
