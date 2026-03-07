import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { ProductAnalysisJobStatusComponent } from './product-analysis-job-status.component';

describe('ProductAnalysisJobStatusComponent', () => {
  let component: ProductAnalysisJobStatusComponent;
  let fixture: ComponentFixture<ProductAnalysisJobStatusComponent>;
  let apiSpy: { getProductAnalysisStatus: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      getProductAnalysisStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', progress: 0, current_phase: 'spec_review', waiting_for_answers: false })),
    };
    await TestBed.configureTestingModule({
      imports: [ProductAnalysisJobStatusComponent],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(ProductAnalysisJobStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should poll and set status when jobId is set', () => {
    expect(apiSpy.getProductAnalysisStatus).toHaveBeenCalledWith('j1');
    expect(component.status).toBeTruthy();
    expect(component.status?.job_id).toBe('j1');
  });
});
