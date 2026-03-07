import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { FrontendCodeV2JobStatusComponent } from './frontend-code-v2-job-status.component';

describe('FrontendCodeV2JobStatusComponent', () => {
  let component: FrontendCodeV2JobStatusComponent;
  let fixture: ComponentFixture<FrontendCodeV2JobStatusComponent>;

  beforeEach(async () => {
    const apiSpy = { getFrontendCodeV2Status: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })) };
    await TestBed.configureTestingModule({
      imports: [FrontendCodeV2JobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(FrontendCodeV2JobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
