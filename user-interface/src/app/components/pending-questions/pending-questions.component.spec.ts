import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningApiService } from '../../services/planning-api.service';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { PendingQuestionsComponent } from './pending-questions.component';

describe('PendingQuestionsComponent', () => {
  let component: PendingQuestionsComponent;
  let fixture: ComponentFixture<PendingQuestionsComponent>;
  let apiSpy: {
    submitAnswers: ReturnType<typeof vi.fn>;
    submitProductAnalysisAnswers: ReturnType<typeof vi.fn>;
  };
  let planningApiSpy: { submitAnswers: ReturnType<typeof vi.fn> };
  let codingTeamApiSpy: { submitAnswers: ReturnType<typeof vi.fn> };

  const mockQuestion = {
    id: 'q1',
    question: 'Choose one?',
    required: true,
    options: [{ id: 'a1', label: 'A1' }, { id: 'other', label: 'Other' }],
  };

  beforeEach(async () => {
    apiSpy = {
      submitAnswers: vi.fn(),
      submitProductAnalysisAnswers: vi.fn(),
    };
    planningApiSpy = { submitAnswers: vi.fn() };
    codingTeamApiSpy = { submitAnswers: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [PendingQuestionsComponent, NoopAnimationsModule],
      providers: [
        { provide: SoftwareEngineeringApiService, useValue: apiSpy },
        { provide: PlanningApiService, useValue: planningApiSpy },
        { provide: CodingTeamApiService, useValue: codingTeamApiSpy },
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

  it('should call PlanningApiService.submitAnswers when submitEndpoint is planning', () => {
    component.submitEndpoint = 'planning';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    planningApiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.submitAnswers();

    expect(planningApiSpy.submitAnswers).toHaveBeenCalledWith('job-1', expect.any(Array));
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

  it('autoAnswerQuestion no-op for planning', () => {
    component.submitEndpoint = 'planning';
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

  it('applyAutoAnswer uses planning endpoint', () => {
    component.submitEndpoint = 'planning';
    component.autoAnswerResults.set('q1', { selected_option_id: 'a1' } as any);
    planningApiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'completed' } as any));
    component.applyAutoAnswer('q1');
    expect(planningApiSpy.submitAnswers).toHaveBeenCalled();
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

  it('should call CodingTeamApiService.submitAnswers when submitEndpoint is coding-team', () => {
    component.submitEndpoint = 'coding-team';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    const mockStatus = { job_id: 'job-1', status: 'running', waiting_for_answers: false };
    codingTeamApiSpy.submitAnswers.mockReturnValue(of(mockStatus));

    let emitted: any;
    component.answersSubmitted.subscribe((r) => (emitted = r));
    component.submitAnswers();

    expect(codingTeamApiSpy.submitAnswers).toHaveBeenCalledWith('job-1', {
      answers: [{ question_id: 'q1', selected_option_id: 'a1', other_text: null }],
    });
    // The coding-team backend contract is single-select: no multi-select field.
    const body = codingTeamApiSpy.submitAnswers.mock.calls[0][1];
    expect('selected_option_ids' in body.answers[0]).toBe(false);
    expect(body.resume_token).toBeUndefined();
    expect(emitted).toEqual(mockStatus);
  });

  it('echoes resume_token on coding-team submit for Temporal-native pauses', () => {
    component.submitEndpoint = 'coding-team';
    component.resumeToken = 'job-1:tok-abc';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    codingTeamApiSpy.submitAnswers.mockReturnValue(
      of({ job_id: 'job-1', status: 'waiting_for_user', waiting_for_answers: true }),
    );

    component.submitAnswers();

    expect(codingTeamApiSpy.submitAnswers).toHaveBeenCalledWith('job-1', {
      answers: [{ question_id: 'q1', selected_option_id: 'a1', other_text: null }],
      resume_token: 'job-1:tok-abc',
    });
  });

  it('coding-team submit error path sets error', () => {
    component.submitEndpoint = 'coding-team';
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    codingTeamApiSpy.submitAnswers.mockReturnValue(
      throwError(() => ({ error: { detail: 'Job is not waiting for answers.' } })),
    );
    component.submitAnswers();

    expect(component.error).toContain('not waiting');
    expect(component.submitting).toBe(false);
  });

  it('autoAnswerEnabled is false for coding-team and planning, true otherwise', () => {
    component.submitEndpoint = 'coding-team';
    expect(component.autoAnswerEnabled).toBe(false);
    component.submitEndpoint = 'planning';
    expect(component.autoAnswerEnabled).toBe(false);
    component.submitEndpoint = 'run-team';
    expect(component.autoAnswerEnabled).toBe(true);
    component.submitEndpoint = 'product-analysis';
    expect(component.autoAnswerEnabled).toBe(true);
  });

  it('an out-of-union endpoint surfaces an error instead of stranding the submit spinner', () => {
    component.submitEndpoint = 'bogus-endpoint' as any;
    component.questions = [{ ...mockQuestion, required: false } as any];
    component.initializeAnswers();
    component.getAnswer('q1')!.selectedOptionIds.add('a1');
    component.answers = new Map(component.answers);

    component.submitAnswers();

    expect(component.submitting).toBe(false);
    expect(component.error).toContain('Unsupported submit endpoint');
  });

  it('autoAnswerQuestion no-op for coding-team (user-only decisions)', () => {
    component.submitEndpoint = 'coding-team';
    component.autoAnswerQuestion(mockQuestion as any);
    expect(component.hasAutoAnswerResult('q1')).toBe(false);
    expect(component.isAutoAnswering('q1')).toBe(false);
  });

  it('single-select toggle replaces the previous selection', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: true });
    const ids = component.getAnswer('q1')!.selectedOptionIds;
    expect(ids.has('a1')).toBe(false);
    expect(ids.has('other')).toBe(true);
    expect(ids.size).toBe(1);
  });

  it('single-select toggle away from "other" clears otherText', () => {
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: true });
    component.onQuestionOtherTextChanged('q1', 'custom answer');
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    expect(component.getAnswer('q1')!.otherText).toBe('');
    expect(component.getAnswer('q1')!.selectedOptionIds.has('other')).toBe(false);
  });

  it('blocks submission while an optional question is half-answered (blank "other")', () => {
    const required = { ...mockQuestion, id: 'q1', required: true };
    const optional = { ...mockQuestion, id: 'q2', required: false };
    fixture.componentRef.setInput('questions', [required as any, optional as any]);
    fixture.detectChanges();

    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    component.onQuestionOptionToggled('q2', { optionId: 'other', checked: true });

    // Required answered, but the optional 'other' has no text — the backend
    // would 400 the whole batch, so the gate must hold.
    expect(component.allRequiredAnswered).toBe(true);
    expect(component.allAnswersSubmittable).toBe(false);
    component.submitAnswers();
    expect(apiSpy.submitAnswers).not.toHaveBeenCalled();

    component.onQuestionOtherTextChanged('q2', 'now valid');
    expect(component.allAnswersSubmittable).toBe(true);
  });

  it('allows submission when an optional question is left untouched', () => {
    const required = { ...mockQuestion, id: 'q1', required: true };
    const optional = { ...mockQuestion, id: 'q2', required: false };
    fixture.componentRef.setInput('questions', [required as any, optional as any]);
    fixture.detectChanges();

    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    expect(component.allAnswersSubmittable).toBe(true);

    apiSpy.submitAnswers.mockReturnValue(of({ job_id: 'job-1', status: 'running' }));
    component.submitAnswers();
    // The untouched optional question is omitted from the submission entirely.
    const body = apiSpy.submitAnswers.mock.calls[0][1];
    expect(body.answers.map((a: { question_id: string }) => a.question_id)).toEqual(['q1']);
  });

  it('multi-select questions still accumulate selections', () => {
    const multiQuestion = { ...mockQuestion, allow_multiple: true };
    fixture.componentRef.setInput('questions', [multiQuestion as any]);
    fixture.detectChanges();
    component.onQuestionOptionToggled('q1', { optionId: 'a1', checked: true });
    component.onQuestionOptionToggled('q1', { optionId: 'other', checked: true });
    const ids = component.getAnswer('q1')!.selectedOptionIds;
    expect(ids.has('a1')).toBe(true);
    expect(ids.has('other')).toBe(true);
  });
});
