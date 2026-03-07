import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityJobStatusComponent } from './accessibility-job-status.component';

describe('AccessibilityJobStatusComponent', () => {
  let component: AccessibilityJobStatusComponent;
  let fixture: ComponentFixture<AccessibilityJobStatusComponent>;

  beforeEach(async () => {
    const apiSpy = { getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed', audit_id: 'a1' })) };
    await TestBed.configureTestingModule({
      imports: [AccessibilityJobStatusComponent],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityJobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should poll when jobId set', () => {
    expect(component.status).toBeTruthy();
  });
});
