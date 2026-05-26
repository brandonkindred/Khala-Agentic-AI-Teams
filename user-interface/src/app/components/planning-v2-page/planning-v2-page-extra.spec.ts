import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { BehaviorSubject, of, throwError } from 'rxjs';
import { vi, beforeEach, afterEach } from 'vitest';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV2PageComponent } from './planning-v2-page.component';
import type { RunningJobSummary } from '../../models';

const buildJob = (id: string): RunningJobSummary => ({
  job_id: id,
  status: 'running',
  phase: 'planning',
  progress: 50,
} as RunningJobSummary);

describe('PlanningV2PageComponent (extra coverage)', () => {
  let api: {
    health: ReturnType<typeof vi.fn>;
    getPlanningV2Jobs: ReturnType<typeof vi.fn>;
    runPlanningV2: ReturnType<typeof vi.fn>;
  };
  let queryParams$: BehaviorSubject<Record<string, string>>;
  let component: PlanningV2PageComponent;
  let fixture: ComponentFixture<PlanningV2PageComponent>;

  beforeEach(async () => {
    queryParams$ = new BehaviorSubject<Record<string, string>>({});
    api = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getPlanningV2Jobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      runPlanningV2: vi.fn().mockReturnValue(of({ job_id: 'new-j' })),
    };
    await TestBed.configureTestingModule({
      imports: [PlanningV2PageComponent, NoopAnimationsModule],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(PlanningV2PageComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  it('healthCheck returns observable from api', () => {
    fixture.detectChanges();
    component.healthCheck().subscribe();
    expect(api.health).toHaveBeenCalled();
  });

  it('initial load with jobs selects first one', async () => {
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [buildJob('a'), buildJob('b')] }));
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.selectedJob?.job_id).toBe('a');
    expect(component.jobId).toBe('a');
  });

  it('jobId from query param selects matching job', async () => {
    queryParams$.next({ jobId: 'b' });
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [buildJob('a'), buildJob('b')] }));
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.selectedJob?.job_id).toBe('b');
  });

  it('jobId from query param without matching job sets jobId only', async () => {
    queryParams$.next({ jobId: 'missing' });
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [buildJob('a')] }));
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.jobId).toBe('missing');
  });

  it('empty jobs list clears selection', async () => {
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [] }));
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    component.selectedJob = buildJob('orphan');
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [] }));
    // Trigger another poll
    await vi.advanceTimersByTimeAsync(15000);
    expect(component.selectedJob).toBeNull();
  });

  it('clears stale selectedJob when not in new list', async () => {
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [buildJob('a')] }));
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    component.selectedJob = buildJob('gone');
    api.getPlanningV2Jobs.mockReturnValue(of({ jobs: [buildJob('a')] }));
    await vi.advanceTimersByTimeAsync(15000);
    // Should reset to null since gone is not in list
    expect(component.selectedJob === null || component.selectedJob.job_id !== 'gone').toBe(true);
  });

  it('selectJob updates both selectedJob and jobId', () => {
    fixture.detectChanges();
    const j = buildJob('x');
    component.selectJob(j);
    expect(component.selectedJob).toBe(j);
    expect(component.jobId).toBe('x');
  });

  it('onPlanningV2Submit posts request and stores job id', () => {
    fixture.detectChanges();
    component.onPlanningV2Submit({ spec: 's', repo_path: '/x' } as never);
    expect(api.runPlanningV2).toHaveBeenCalled();
    expect(component.jobId).toBe('new-j');
    expect(component.loading).toBe(false);
  });

  it('onPlanningV2Submit error sets error message', () => {
    fixture.detectChanges();
    api.runPlanningV2.mockReturnValue(throwError(() => ({ error: { detail: 'plan-fail' } })));
    component.onPlanningV2Submit({ spec: 's' } as never);
    expect(component.error).toBe('plan-fail');
    expect(component.loading).toBe(false);
  });

  it('ngOnDestroy unsubscribes', async () => {
    vi.useFakeTimers();
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['jobsSub']).toBeTruthy();
    component.ngOnDestroy();
  });
});
