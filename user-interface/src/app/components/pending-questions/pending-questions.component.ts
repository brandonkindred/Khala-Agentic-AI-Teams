import { Component, EventEmitter, Input, OnChanges, Output, SimpleChanges, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatChipsModule } from '@angular/material/chips';
import { Observable, throwError } from 'rxjs';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningApiService } from '../../services/planning-api.service';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import type {
  PendingQuestion,
  AnswerSubmission,
  JobStatusResponse,
  PlanningStatusResponse,
  ProductAnalysisStatusResponse,
  AutoAnswerResponse,
} from '../../models';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import { QuestionCardComponent } from './question-card/question-card.component';

/** Endpoint type determines which API to call for submitting answers. */
export type SubmitEndpointType = 'run-team' | 'planning' | 'product-analysis' | 'coding-team';

/**
 * Everything `answersSubmitted` can emit — the post-submit status of whichever
 * endpoint the component was configured with. Consumers narrow to the shape
 * their own `submitEndpoint` produces.
 */
export type AnswersSubmittedStatus =
  | JobStatusResponse
  | PlanningStatusResponse
  | ProductAnalysisStatusResponse
  | CodingTeamJobStatus;

/**
 * Per-endpoint capabilities. `Record` over the full union makes adding a new
 * endpoint a compile error until its capabilities are declared here — the
 * single source of truth for whether AI auto-answer is offered (default-deny:
 * an endpoint must opt IN; coding-team decisions are user-only by policy and
 * planning exposes no auto-answer API).
 */
const ENDPOINT_CAPABILITIES: Record<SubmitEndpointType, { autoAnswer: boolean }> = {
  'run-team': { autoAnswer: true },
  'planning': { autoAnswer: false },
  'product-analysis': { autoAnswer: true },
  'coding-team': { autoAnswer: false },
};

interface QuestionAnswer {
  questionId: string;
  /** Selected option IDs (multi-select) */
  selectedOptionIds: Set<string>;
  otherText: string;
  wasAutoAnswered: boolean;
  autoAnswerRationale: string;
  autoAnswerConfidence: number;
  autoAnswerRisks: string[];
}

@Component({
  selector: 'app-pending-questions',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatCheckboxModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatExpansionModule,
    MatChipsModule,
    QuestionCardComponent,
  ],
  templateUrl: './pending-questions.component.html',
  styleUrl: './pending-questions.component.scss',
})
export class PendingQuestionsComponent implements OnChanges {
  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly planningApi = inject(PlanningApiService);
  private readonly codingTeamApi = inject(CodingTeamApiService);

  @Input() jobId: string | null = null;
  @Input() questions: PendingQuestion[] = [];
  /** Which endpoint to call: 'run-team' (default), 'planning', 'product-analysis', or 'coding-team'. */
  @Input() submitEndpoint: SubmitEndpointType = 'run-team';
  /**
   * Temporal-native pause token from job status. Echoed on coding-team answer submit when set;
   * ignored by other endpoints.
   */
  @Input() resumeToken: string | null = null;
  @Output() answersSubmitted = new EventEmitter<AnswersSubmittedStatus>();

  answers = new Map<string, QuestionAnswer>();
  submitting = false;
  error: string | null = null;

  /** Track which questions are currently being auto-answered. */
  autoAnsweringQuestions = new Set<string>();

  /** Store auto-answer results for display. */
  autoAnswerResults = new Map<string, AutoAnswerResponse>();

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['questions']) {
      this.initializeAnswers();
    }
  }

  private initializeAnswers(): void {
    const currentQuestionIds = new Set(this.questions.map(q => q.id));

    // Remove answers for questions no longer present
    for (const questionId of this.answers.keys()) {
      if (!currentQuestionIds.has(questionId)) {
        this.answers.delete(questionId);
        this.autoAnswerResults.delete(questionId);
      }
    }

    // Add default answers only for NEW questions
    for (const q of this.questions) {
      if (!this.answers.has(q.id)) {
        this.answers.set(q.id, {
          questionId: q.id,
          selectedOptionIds: new Set<string>(),
          otherText: '',
          wasAutoAnswered: false,
          autoAnswerRationale: '',
          autoAnswerConfidence: 0,
          autoAnswerRisks: [],
        });
      }
    }
  }

  getAnswer(questionId: string): QuestionAnswer | undefined {
    return this.answers.get(questionId);
  }

  /** Handle option toggle events from child QuestionCardComponent */
  onQuestionOptionToggled(questionId: string, event: { optionId: string; checked: boolean }): void {
    const answer = this.answers.get(questionId);
    if (answer) {
      if (event.checked) {
        const question = this.questions.find((q) => q.id === questionId);
        // Single-select questions render as radios: the card only emits the newly
        // checked option, so replace the previous selection instead of accumulating.
        if (question?.allow_multiple !== true) {
          answer.selectedOptionIds.clear();
          if (event.optionId !== 'other') {
            answer.otherText = '';
          }
        }
        answer.selectedOptionIds.add(event.optionId);
      } else {
        answer.selectedOptionIds.delete(event.optionId);
        if (event.optionId === 'other') {
          answer.otherText = '';
        }
      }
      if (answer.wasAutoAnswered) {
        answer.wasAutoAnswered = false;
      }
      this.answers = new Map(this.answers);
    }
  }

  /** Handle other text change events from child QuestionCardComponent */
  onQuestionOtherTextChanged(questionId: string, text: string): void {
    const answer = this.answers.get(questionId);
    if (answer) {
      answer.otherText = text;
      // Reassign the Map so derived bindings (isQuestionAnswered → child [isAnswered],
      // submit-gate) re-evaluate, matching onQuestionOptionToggled's reference update.
      this.answers = new Map(this.answers);
    }
  }

  isQuestionAnswered(question: PendingQuestion): boolean {
    const answer = this.answers.get(question.id);
    if (!answer) return false;

    // All questions use multi-select (checkboxes)
    if (answer.selectedOptionIds.size === 0) return false;
    // If "other" is selected, require text
    if (answer.selectedOptionIds.has('other') && !answer.otherText.trim()) {
      return false;
    }
    return true;
  }

  isAutoAnswering(questionId: string): boolean {
    return this.autoAnsweringQuestions.has(questionId);
  }

  hasAutoAnswerResult(questionId: string): boolean {
    return this.autoAnswerResults.has(questionId);
  }

  getAutoAnswerResult(questionId: string): AutoAnswerResponse | undefined {
    return this.autoAnswerResults.get(questionId);
  }

  get allRequiredAnswered(): boolean {
    return this.questions
      .filter((q) => q.required)
      .every((q) => this.isQuestionAnswered(q));
  }

  /**
   * True when the batch as a whole is valid to submit: every required question
   * is answered AND no touched question is half-answered (e.g. an optional
   * question with 'other' ticked but no text — the backend rejects the entire
   * batch over a single invalid answer, so the gate must cover optional ones too).
   */
  get allAnswersSubmittable(): boolean {
    return (
      this.allRequiredAnswered &&
      this.questions.every((q) => {
        const answer = this.answers.get(q.id);
        // Untouched questions are simply omitted from the submission.
        if (!answer || answer.selectedOptionIds.size === 0) return true;
        return this.isQuestionAnswered(q);
      })
    );
  }

  get answeredCount(): number {
    return this.questions.filter((q) => this.isQuestionAnswered(q)).length;
  }

  /**
   * Whether the AI auto-answer affordance is offered for this endpoint.
   * Driven by ENDPOINT_CAPABILITIES; an unknown value (impossible under the
   * union type, but reachable from a template typo) denies by default.
   */
  get autoAnswerEnabled(): boolean {
    return ENDPOINT_CAPABILITIES[this.submitEndpoint]?.autoAnswer ?? false;
  }

  autoAnswerQuestion(question: PendingQuestion): void {
    if (!this.autoAnswerEnabled || !this.jobId || this.isAutoAnswering(question.id)) return;

    this.autoAnsweringQuestions.add(question.id);
    this.error = null;

    const handleSuccess = (response: AutoAnswerResponse): void => {
      this.autoAnsweringQuestions.delete(question.id);
      this.autoAnswerResults.set(question.id, response);
    };

    const handleError = (err: { error?: { detail?: string }; message?: string }): void => {
      this.autoAnsweringQuestions.delete(question.id);
      this.error = `Auto-answer failed for Q${this.questions.indexOf(question) + 1}: ${err?.error?.detail ?? err?.message ?? 'Unknown error'}`;
    };

    // Explicit per-endpoint dispatch — never fall through to a foreign team's
    // auto-answer endpoint. Endpoints without an auto-answer API are already
    // excluded by the autoAnswerEnabled guard above.
    switch (this.submitEndpoint) {
      case 'product-analysis':
        this.api.autoAnswerProductAnalysis(this.jobId, question.id).subscribe({
          next: handleSuccess,
          error: handleError,
        });
        break;
      case 'run-team':
        this.api.autoAnswerRunTeam(this.jobId, question.id).subscribe({
          next: handleSuccess,
          error: handleError,
        });
        break;
      default:
        this.autoAnsweringQuestions.delete(question.id);
        break;
    }
  }

  applyAutoAnswer(questionId: string): void {
    const result = this.autoAnswerResults.get(questionId);
    if (!result || !this.jobId) return;
    const jobId = this.jobId;

    // Mark as submitting (reuse autoAnsweringQuestions set for spinner)
    this.autoAnsweringQuestions.add(questionId);
    this.error = null;

    const submission: AnswerSubmission = {
      question_id: questionId,
      selected_option_id: result.selected_option_id,
      selected_option_ids: [result.selected_option_id],
      other_text: null,
    };
    const request = { answers: [submission] };

    this.getSubmitObservable(jobId, request).subscribe({
      next: (statusResponse) => {
        this.autoAnsweringQuestions.delete(questionId);
        this.autoAnswerResults.delete(questionId);
        this.answers.delete(questionId);
        // Emit so parent refreshes and question disappears
        this.answersSubmitted.emit(statusResponse);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.autoAnsweringQuestions.delete(questionId);
        this.error = `Failed to submit auto-answer: ${err?.error?.detail ?? err?.message ?? 'Unknown error'}`;
      },
    });
  }

  /**
   * Route the answer submission to the API service matching `submitEndpoint`,
   * translating the shared `AnswerSubmission[]` shape into each backend's
   * expected request body (e.g. coding-team strips the multi-select field).
   */
  private getSubmitObservable(
    jobId: string,
    request: { answers: AnswerSubmission[] }
  ): Observable<
    | JobStatusResponse
    | PlanningStatusResponse
    | ProductAnalysisStatusResponse
    | CodingTeamJobStatus
  > {
    switch (this.submitEndpoint) {
      case 'planning': {
        const body = request.answers.map((a) => ({
          question_id: a.question_id,
          selected_option_id: a.selected_option_id ?? undefined,
          selected_option_ids: a.selected_option_ids,
          other_text: a.other_text ?? undefined,
        }));
        return this.planningApi.submitAnswers(jobId, body);
      }
      case 'product-analysis':
        return this.api.submitProductAnalysisAnswers(jobId, request) as Observable<ProductAnalysisStatusResponse>;
      case 'coding-team': {
        // Coding-team answers are single-select: strip the multi-select field to
        // match the backend contract exactly. Echo resume_token for Temporal-native pauses.
        const body = {
          answers: request.answers.map((a) => ({
            question_id: a.question_id,
            selected_option_id: a.selected_option_id,
            other_text: a.other_text,
          })),
          ...(this.resumeToken ? { resume_token: this.resumeToken } : {}),
        };
        return this.codingTeamApi.submitAnswers(jobId, body);
      }
      case 'run-team':
        return this.api.submitAnswers(jobId, request);
      default: {
        // Compile-time exhaustiveness: a new SubmitEndpointType member fails to
        // build until it gets an explicit submit route (no silent fallthrough
        // to a foreign team's endpoint). At runtime an out-of-union value (e.g.
        // a string-typed binding) must flow through the subscriber's error path
        // — a synchronous throw here would strand `submitting` on true with no
        // visible error.
        const unhandled: never = this.submitEndpoint;
        return throwError(() => new Error(`Unsupported submit endpoint: ${String(unhandled)}`));
      }
    }
  }

  dismissAutoAnswer(questionId: string): void {
    this.autoAnswerResults.delete(questionId);
  }

  /**
   * Submit the currently-selected answers for every answerable question,
   * dispatching to the correct backend via `getSubmitObservable` and
   * emitting `answersSubmitted` with the resulting job status on success.
   * A no-op when there is no active job or not every question is answered.
   */
  submitAnswers(): void {
    if (!this.jobId || !this.allAnswersSubmittable) return;
    const jobId = this.jobId;

    const submissions: AnswerSubmission[] = [];
    for (const q of this.questions) {
      const answer = this.answers.get(q.id);
      if (!answer) continue;

      // All questions use multi-select (checkboxes)
      if (answer.selectedOptionIds.size > 0) {
        const selectedIds = Array.from(answer.selectedOptionIds);
        submissions.push({
          question_id: q.id,
          selected_option_id: selectedIds[0] || null, // Primary selection for backward compatibility
          selected_option_ids: selectedIds,
          other_text: answer.selectedOptionIds.has('other') ? answer.otherText : null,
        });
      }
    }

    this.submitting = true;
    this.error = null;

    const request = { answers: submissions };

    this.getSubmitObservable(jobId, request).subscribe({
      next: (response) => {
        this.submitting = false;
        this.answersSubmitted.emit(response);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.submitting = false;
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to submit answers';
      },
    });
  }
}
