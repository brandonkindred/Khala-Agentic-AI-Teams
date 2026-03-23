import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { BloggingApiService } from '../../services/blogging-api.service';
import { BloggingDashboardComponent } from './blogging-dashboard.component';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('BloggingDashboardComponent', () => {
  let component: BloggingDashboardComponent;
  let fixture: ComponentFixture<BloggingDashboardComponent>;
  let apiSpy: {
    startResearchReviewAsync: ReturnType<typeof vi.fn>;
    startFullPipelineAsync: ReturnType<typeof vi.fn>;
    getJobs: ReturnType<typeof vi.fn>;
    getJobStatus: ReturnType<typeof vi.fn>;
    getJobArtifacts: ReturnType<typeof vi.fn>;
    getJobArtifactContent: ReturnType<typeof vi.fn>;
    health: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      startResearchReviewAsync: vi.fn(),
      startFullPipelineAsync: vi.fn(),
      getJobs: vi.fn(),
      getJobStatus: vi.fn(),
      getJobArtifacts: vi.fn(),
      getJobArtifactContent: vi.fn(),
      health: vi.fn(),
    };
    apiSpy.getJobs.mockReturnValue(of([]));
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'x', status: 'running' }));
    apiSpy.health.mockReturnValue(of({ brand_spec_configured: false }));

    await TestBed.configureTestingModule({
      imports: [BloggingDashboardComponent, NoopAnimationsModule],
      providers: [provideRouter([]), { provide: BloggingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BloggingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should fetch all jobs via getJobs(false) on init', () => {
    expect(apiSpy.getJobs).toHaveBeenCalledWith(false);
  });

  it('should call startResearchReviewAsync and refetch jobs on success', () => {
    apiSpy.startResearchReviewAsync.mockReturnValue(of({ job_id: 'new-job-123' }));
    apiSpy.getJobs.mockReturnValue(of([{ job_id: 'new-job-123', status: 'running', brief: 'Test', progress: 0 }]));

    component.onResearchReviewSubmit({ brief: 'Test', max_results: 20 });

    expect(apiSpy.startResearchReviewAsync).toHaveBeenCalledWith({
      brief: 'Test',
      max_results: 20,
    });
    expect(component.loading).toBe(false);
  });

  it('should set error on startResearchReviewAsync failure', () => {
    apiSpy.startResearchReviewAsync.mockReturnValue(
      throwError(() => ({ error: { detail: 'Server error' } }))
    );

    component.onResearchReviewSubmit({ brief: 'Test', max_results: 20 });

    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
  });
});
