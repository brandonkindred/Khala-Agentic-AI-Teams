import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { PendingQuestionsComponent } from './pending-questions.component';

describe('PendingQuestionsComponent', () => {
  let component: PendingQuestionsComponent;
  let fixture: ComponentFixture<PendingQuestionsComponent>;
  let apiSpy: {
    submitAnswers: ReturnType<typeof vi.fn>;
    submitPlanningV2Answers: ReturnType<typeof vi.fn>;
    submitProductAnalysisAnswers: ReturnType<typeof vi.fn>;
  };
  let planningV3ApiSpy: { submitAnswers: ReturnType<typeof vi.fn> };

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
    planningV3ApiSpy = { submitAnswers: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [PendingQuestionsComponent, NoopAnimationsModule],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: apiSpy },
        { provide: PlanningV3ApiService, useValue: planningV3ApiSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PendingQuestionsComponent);
    component = fixture.componentInstance;
    component.jobId = 'job-1';
    fixture.componentRef.setInput('questions', [mockQuestion as any]);
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

  it('should call PlanningV3ApiService.submitAnswers when submitEndpoint is planning-v3', () => {
    component.submitEndpoint = 'planning-v3';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    planningV3ApiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.submitAnswers();

    expect(planningV3ApiSpy.submitAnswers).toHaveBeenCalledWith('job-1', expect.any(Array));
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

  it('onQuestionOptionToggled adds + removes selectedOptionIds', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    expect(component.getAnswer('q1')!.selectedOptionIds.has('a1')).toBe(true);
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: false });
    expect(component.getAnswer('q1')!.selectedOptionIds.has('a1')).toBe(false);
  });

  it('onQuestionOptionToggled clears otherText when removing "other"', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: true });
    component.onQuestionOtherTextChanged('q1', 'My text');
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: false });
    expect(component.getAnswer('q1')!.otherText).toBe('');
  });

  it('onQuestionOptionToggled clears wasAutoAnswered on toggle', () => {
    const a = component.getAnswer('q1')!;
    a.wasAutoAnswered = true;
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    expect(component.getAnswer('q1')!.wasAutoAnswered).toBe(false);
  });

  it('onQuestionOptionToggled no-op for missing question', () => {
    expect(() => component.onQuestionOptionToggled('missing', { optionId: 'a1', checked: true })).not.toThrow();
  });

  it('onQuestionOtherTextChanged updates otherText', () => {
    component.onQuestionOtherTextChanged('q1', 'hello');
    expect(component.getAnswer('q1')!.otherText).toBe('hello');
  });

  it('onQuestionOtherTextChanged no-op for missing question', () => {
    expect(() => component.onQuestionOtherTextChanged('missing', 'x')).not.toThrow();
  });

  it('isQuestionAnswered handles "other" without text', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: true });
    expect(component.isQuestionAnswered(mockQuestion as any)).toBe(false);
    component.onQuestionOtherTextChanged('q1', 'detail');
    expect(component.isQuestionAnswered(mockQuestion as any)).toBe(true);
  });

  it('allRequiredAnswered + answeredCount', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    expect(component.allRequiredAnswered).toBe(true);
    expect(component.answeredCount).toBe(1);
  });

  it('autoAnswerQuestion early-exits without jobId', () => {
    component.jobId = null;
    component.autoAnswerQuestion(mockQuestion as any);
    expect(apiSpy.submitAnswers).not.toHaveBeenCalled();
  });

  it('autoAnswerQuestion ignores when already auto-answering', () => {
    component.autoAnsweringQuestions.add('q1');
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.autoAnsweringQuestions.has('q1')).toBe(true);
  });

  it('autoAnswerQuestion success for run-team', () => {
    apiSpy.submitAnswers = vi.fn();
    (apiSpy as unknown as { autoAnswerRunTeam: ReturnType<typeof vi.fn> }).autoAnswerRunTeam = vi
      .fn()
      .mockReturnValue(of({ selected_option_id: 'a1' }));
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.hasAutoAnswerResult('q1')).toBe(true);
    expect(component.isAutoAnswering('q1')).toBe(false);
  });

  it('autoAnswerQuestion error path sets error', () => {
    (apiSpy as unknown as { autoAnswerRunTeam: ReturnType<typeof vi.fn> }).autoAnswerRunTeam = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.error).toContain('oops');
  });

  it('autoAnswerQuestion uses product-analysis endpoint when set', () => {
    component.submitEndpoint = 'product-analysis';
    (apiSpy as unknown as { autoAnswerProductAnalysis: ReturnType<typeof vi.fn> }).autoAnswerProductAnalysis = vi
      .fn()
      .mockReturnValue(of({ selected_option_id: 'a1' }));
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.hasAutoAnswerResult('q1')).toBe(true);
  });

  it('autoAnswerQuestion no-op for planning-v3', () => {
    component.submitEndpoint = 'planning-v3';
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.hasAutoAnswerResult('q1')).toBe(false);
    expect(component.isAutoAnswering('q1')).toBe(false);
  });

  it('applyAutoAnswer no-ops without result', () => {
    component.applyAutoAnswer('q1');
    expect(apiSpy.submitAnswers).not.toHaveBeenCalled();
  });

  it('applyAutoAnswer success emits + clears', () => {
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    apiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    let emitted: unknown;
    component.answersSubmitted.subscribe((r) => (emitted = r));
    component.applyAutoAnswer('q1');
    expect(emitted).toBeDefined();
    expect(component.hasAutoAnswerResult('q1')).toBe(false);
  });

  it('applyAutoAnswer error path sets error', () => {
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    apiSpy.submitAnswers.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.applyAutoAnswer('q1');
    expect(component.error).toContain('oops');
  });

  it('applyAutoAnswer uses planning-v3 endpoint', () => {
    component.submitEndpoint = 'planning-v3';
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    planningV3ApiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.applyAutoAnswer('q1');
    expect(planningV3ApiSpy.submitAnswers).toHaveBeenCalled();
  });

  it('applyAutoAnswer uses product-analysis endpoint', () => {
    component.submitEndpoint = 'product-analysis';
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    apiSpy.submitProductAnalysisAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.applyAutoAnswer('q1');
    expect(apiSpy.submitProductAnalysisAnswers).toHaveBeenCalled();
  });

  it('dismissAutoAnswer removes auto answer result', () => {
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    component.dismissAutoAnswer('q1');
    expect(component.hasAutoAnswerResult('q1')).toBe(false);
  });

  it('initializeAnswers cleans up answers for removed questions', () => {
    component.answers.set('removed_q', { questionId: 'removed_q' } as any);
    component.autoAnswerResults.set('removed_q', {} as any);
    component.initializeAnswers();
    expect(component.answers.has('removed_q')).toBe(false);
    expect(component.autoAnswerResults.has('removed_q')).toBe(false);
  });

  it('getAutoAnswerResult returns set value', () => {
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    expect(component.getAutoAnswerResult('q1')).toBeDefined();
  });
});
