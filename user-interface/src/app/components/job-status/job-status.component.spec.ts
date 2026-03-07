import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { JobStatusComponent } from './job-status.component';

describe('JobStatusComponent', () => {
  let component: JobStatusComponent;
  let fixture: ComponentFixture<JobStatusComponent>;
  let apiSpy: { getJobStatus: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      getJobStatus: vi.fn().mockReturnValue(of({
        job_id: 'j1',
        status: 'running',
        progress: 0,
        phase: 'planning',
        waiting_for_answers: false,
        task_results: [],
        task_ids: [],
        failed_tasks: [],
        pending_questions: [],
      })),
    };
    await TestBed.configureTestingModule({
      imports: [JobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(JobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should start polling when jobId is set', () => {
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j1');
    expect(component.status).toBeTruthy();
    expect(component.status?.job_id).toBe('j1');
  });

  it('should set loading false when jobId is null on init', () => {
    const f = TestBed.createComponent(JobStatusComponent);
    const c = f.componentInstance;
    c.jobId = null;
    c.ngOnInit();
    expect(c.loading).toBe(false);
  });
});
