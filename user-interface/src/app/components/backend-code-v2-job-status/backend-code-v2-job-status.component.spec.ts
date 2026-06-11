import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BackendCodeV2JobStatusComponent } from './backend-code-v2-job-status.component';

describe('BackendCodeV2JobStatusComponent', () => {
  let component: BackendCodeV2JobStatusComponent;
  let fixture: ComponentFixture<BackendCodeV2JobStatusComponent>;
  let apiSpy: { getBackendCodeV2Status: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { getBackendCodeV2Status: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', completed_phases: [], current_phase: null })) };
    await TestBed.configureTestingModule({
      imports: [BackendCodeV2JobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BackendCodeV2JobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('should create', () => {
    fixture.detectChanges();
    expect(component).toBeTruthy();
  });

  it('polls every 30s so review sub-step details stay visible', () => {
    vi.useFakeTimers();
    fixture.detectChanges();
    expect(apiSpy.getBackendCodeV2Status).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(30_000);
    expect(apiSpy.getBackendCodeV2Status).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(30_000);
    expect(apiSpy.getBackendCodeV2Status).toHaveBeenCalledTimes(3);
  });
});
