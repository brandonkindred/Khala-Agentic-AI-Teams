import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityJobStatusComponent } from './accessibility-job-status.component';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('AccessibilityJobStatusComponent', () => {
  let component: AccessibilityJobStatusComponent;
  let fixture: ComponentFixture<AccessibilityJobStatusComponent>;
  let apiSpy: { getJobStatus: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed', audit_id: 'a1', current_phase: null, completed_phases: [] })) };
    await TestBed.configureTestingModule({
      imports: [AccessibilityJobStatusComponent],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityJobStatusComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    fixture.componentRef.setInput('jobId', 'j1');
    expect(component).toBeTruthy();
  });

  it('should poll when jobId set', () => {
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    expect(component.status).toBeTruthy();
  });
});
