import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BackendCodeV2JobStatusComponent } from './backend-code-v2-job-status.component';

describe('BackendCodeV2JobStatusComponent', () => {
  let component: BackendCodeV2JobStatusComponent;
  let fixture: ComponentFixture<BackendCodeV2JobStatusComponent>;

  beforeEach(async () => {
    const apiSpy = { getBackendCodeV2Status: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })) };
    await TestBed.configureTestingModule({
      imports: [BackendCodeV2JobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BackendCodeV2JobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
