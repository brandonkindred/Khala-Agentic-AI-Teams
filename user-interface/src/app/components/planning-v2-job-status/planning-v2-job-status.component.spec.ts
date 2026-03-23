import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV2JobStatusComponent } from './planning-v2-job-status.component';

describe('PlanningV2JobStatusComponent', () => {
  let component: PlanningV2JobStatusComponent;
  let fixture: ComponentFixture<PlanningV2JobStatusComponent>;
  let apiSpy: { getPlanningV2Status: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      getPlanningV2Status: vi.fn().mockReturnValue(of({
        job_id: 'j1',
        status: 'running',
        progress: 0,
        current_phase: 'planning',
        waiting_for_answers: false,
        completed_phases: [],
      })),
    };
    await TestBed.configureTestingModule({
      imports: [PlanningV2JobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PlanningV2JobStatusComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    component.jobId = 'j1';
    fixture.detectChanges();
    expect(component).toBeTruthy();
  });

  it('should poll and set status on init', () => {
    component.jobId = 'j1';
    fixture.detectChanges();
    expect(apiSpy.getPlanningV2Status).toHaveBeenCalledWith('j1');
    expect(component.status).toBeTruthy();
    expect(component.status?.status).toBe('running');
  });

  it('should emit statusChange when status received', () => {
    component.jobId = 'j1';
    let emitted: any;
    component.statusChange.subscribe((v) => (emitted = v));
    fixture.detectChanges();
    expect(emitted?.job_id).toBe('j1');
  });
});
