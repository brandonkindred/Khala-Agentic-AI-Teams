import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PendingQuestionsComponent } from './pending-questions.component';

describe('PendingQuestionsComponent', () => {
  let component: PendingQuestionsComponent;
  let fixture: ComponentFixture<PendingQuestionsComponent>;
  let apiSpy: {
    submitAnswers: ReturnType<typeof vi.fn>;
    submitPlanningV2Answers: ReturnType<typeof vi.fn>;
    submitProductAnalysisAnswers: ReturnType<typeof vi.fn>;
  };

  const mockQuestion = {
    id: 'q1',
    question: 'Choose one?',
    required: true,
    options: [{ id: 'a1', label: 'A1' }, { id: 'other', label: 'Other' }],
  };

  beforeEach(async () => {
    apiSpy = {
      submitAnswers: vi.fn(),
      submitPlanningV2Answers: vi.fn(),
      submitProductAnalysisAnswers: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [PendingQuestionsComponent, NoopAnimationsModule],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PendingQuestionsComponent);
    component = fixture.componentInstance;
    component.jobId = 'job-1';
    component.questions = [mockQuestion as any];
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should initialize answers in ngOnChanges when questions change', () => {
    expect(component.answers.has('q1')).toBe(true);
    expect(component.getAnswer('q1')?.selectedOptionIds.size).toBe(0);
  });

  it('should call submitAnswers (run-team) when submitEndpoint is run-team and submitAnswers()', () => {
    component.submitEndpoint = 'run-team';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    const mockStatus = { job_id: 'job-1', status: 'completed', task_results: [], task_ids: [], failed_tasks: [], pending_questions: [] };
    apiSpy.submitAnswers.mockReturnValue(of(mockStatus));

    let emitted: any;
    component.answersSubmitted.subscribe((r) => (emitted = r));
    component.submitAnswers();

    expect(apiSpy.submitAnswers).toHaveBeenCalledWith('job-1', expect.objectContaining({ answers: expect.any(Array) }));
    expect(emitted).toEqual(mockStatus);
    expect(component.submitting).toBe(false);
  });

  it('should call submitPlanningV2Answers when submitEndpoint is planning-v2', () => {
    component.submitEndpoint = 'planning-v2';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    apiSpy.submitPlanningV2Answers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.submitAnswers();

    expect(apiSpy.submitPlanningV2Answers).toHaveBeenCalledWith('job-1', expect.any(Object));
  });

  it('should call submitProductAnalysisAnswers when submitEndpoint is product-analysis', () => {
    component.submitEndpoint = 'product-analysis';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    apiSpy.submitProductAnalysisAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.submitAnswers();

    expect(apiSpy.submitProductAnalysisAnswers).toHaveBeenCalledWith('job-1', expect.any(Object));
  });

  it('should set error on submit failure', () => {
    component.submitEndpoint = 'run-team';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    apiSpy.submitAnswers.mockReturnValue(throwError(() => ({ error: { detail: 'Server error' } })));
    component.submitAnswers();

    expect(component.error).toBeTruthy();
    expect(component.submitting).toBe(false);
  });

  it('should not submit when jobId is null', () => {
    component.jobId = null;
    component.submitAnswers();
    expect(apiSpy.submitAnswers).not.toHaveBeenCalled();
  });

  it('isQuestionAnswered returns false when no option selected', () => {
    expect(component.isQuestionAnswered(mockQuestion as any)).toBe(false);
  });
});
