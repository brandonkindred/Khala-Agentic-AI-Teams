import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
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
      providers: [provideHttpClient(), provideRouter([]), { provide: BloggingApiService, useValue: apiSpy }],
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

  it('memoizes getStoryAgentMessages per status and rebuilds on a new status', () => {
    const makeStatus = () =>
      ({
        current_gap_round: 0,
        story_chat_history: [
          { gap_round: 0, role: 'assistant', content: 'hi' },
          { gap_round: 1, role: 'assistant', content: 'later round' },
        ],
      }) as unknown as NonNullable<typeof component.selectedJobStatus>;

    component.selectedJobStatus = makeStatus();
    const msgs1 = component.getStoryAgentMessages();
    expect(msgs1.length).toBe(1); // only the current round (0) + undefined-round messages
    expect(component.getStoryAgentMessages()).toBe(msgs1); // cached on second call

    component.selectedJobStatus = makeStatus(); // a poll delivers a fresh status object
    expect(component.getStoryAgentMessages()).not.toBe(msgs1); // cache invalidated, rebuilt
  });
});
