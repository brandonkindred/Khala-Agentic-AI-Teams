import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BlogPipelineFlowComponent, BLOG_PHASES } from './blog-pipeline-flow.component';
import type { BlogJobStatusResponse } from '../../models';

describe('BlogPipelineFlowComponent', () => {
  let component: BlogPipelineFlowComponent;
  let fixture: ComponentFixture<BlogPipelineFlowComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BlogPipelineFlowComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(BlogPipelineFlowComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have BLOG_PHASES with 8 phases in order', () => {
    expect(component.BLOG_PHASES.length).toBe(8);
    expect(component.BLOG_PHASES.map((p) => p.id)).toEqual([
      'research',
      'review',
      'draft_initial',
      'copy_edit',
      'fact_check',
      'compliance',
      'rewrite',
      'finalize',
    ]);
  });

  it('should mark phases completed and current for known phase', () => {
    const status: BlogJobStatusResponse = {
      job_id: 'j1',
      status: 'running',
      phase: 'copy_edit',
      progress: 50,
      title_choices: [],
      research_sources_count: 0,
      draft_iterations: 0,
      rewrite_iterations: 0,
    };
    component.status = status;
    fixture.detectChanges();

    expect(component.isPhaseCompleted('research')).toBe(true);
    expect(component.isPhaseCompleted('review')).toBe(true);
    expect(component.isPhaseCompleted('draft_initial')).toBe(true);
    expect(component.isCurrentPhase('copy_edit')).toBe(true);
    expect(component.isPhasePending('fact_check')).toBe(true);
  });

  it('should mark all phases completed when status is completed', () => {
    const status: BlogJobStatusResponse = {
      job_id: 'j1',
      status: 'completed',
      phase: 'finalize',
      progress: 100,
      title_choices: [],
      research_sources_count: 0,
      draft_iterations: 0,
      rewrite_iterations: 0,
    };
    component.status = status;
    fixture.detectChanges();

    expect(component.isPhaseCompleted('research')).toBe(true);
    expect(component.isPhaseCompleted('finalize')).toBe(true);
  });
});
