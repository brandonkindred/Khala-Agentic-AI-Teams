import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { SecurityContext } from '@angular/core';
import { DomSanitizer } from '@angular/platform-browser';
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

  it('getStoryAgentMessages reflects in-place status updates (no stale cache)', () => {
    // The SSE 'update' handler mutates selectedJobStatus in place (Object.assign),
    // so this must recompute — not cache on the object reference — or newly arrived
    // agent messages would never render.
    const status = {
      current_gap_round: 0,
      story_chat_history: [{ gap_round: 0, role: 'assistant', content: 'first' }],
    } as unknown as NonNullable<typeof component.selectedJobStatus>;
    component.selectedJobStatus = status;
    expect(component.getStoryAgentMessages().length).toBe(1);

    // Simulate Object.assign-style in-place mutation from an SSE update.
    status.story_chat_history = [
      { gap_round: 0, role: 'assistant', content: 'first' },
      { gap_round: 0, role: 'assistant', content: 'second' },
    ] as typeof status.story_chat_history;
    expect(component.getStoryAgentMessages().length).toBe(2); // reflects the update
  });

  it('neutralizes malicious HTML in the artifact view modal markdown', () => {
    const sanitizer = TestBed.inject(DomSanitizer);
    component.viewArtifactModal = {
      name: 'notes.md',
      content: '<script>alert(1)</script>\n# Safe Title',
    };

    const html = sanitizer.sanitize(SecurityContext.HTML, component.getViewModalMarkdownHtml()) ?? '';

    expect(html).not.toContain('<script');
    expect(html).not.toContain('alert(1)');
    expect(html).toContain('Safe Title');
  });

  it('returns empty markdown html when the artifact is not markdown', () => {
    component.viewArtifactModal = { name: 'notes.json', content: '<script>alert(1)</script>' };
    expect(component.getViewModalMarkdownHtml()).toBe('');
  });
});
