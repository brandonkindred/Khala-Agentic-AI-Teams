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
import { Observable } from 'rxjs';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import type { PendingQuestion, AnswerSubmission, JobStatusResponse, PlanningV3StatusResponse, ProductAnalysisStatusResponse, AutoAnswerResponse } from '../../models';
import { QuestionCardComponent } from './question-card/question-card.component';

/** Endpoint type determines which API to call for submitting answers. */
export type SubmitEndpointType = 'run-team' | 'planning-v3' | 'product-analysis';

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
  private readonly planningV3Api = inject(PlanningV3ApiService);

  @Input() jobId: string | null = null;
  @Input() questions: PendingQuestion[] = [];
  /** Which endpoint to call: 'run-team' (default), 'planning-v3', or 'product-analysis'. */
  @Input() submitEndpoint: SubmitEndpointType = 'run-team';
  @Output() answersSubmitted = new EventEmitter<JobStatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse>();

  answers: Map<string, QuestionAnswer> = new Map();
  submitting = false;
  error: string | null = null;

  /** Track which questions are currently being auto-answered. */
  autoAnsweringQuestions: Set<string> = new Set();

  /** Store auto-answer results for display. */
  autoAnswerResults: Map<string, AutoAnswerResponse> = new Map();

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

  get answeredCount(): number {
    return this.questions.filter((q) => this.isQuestionAnswered(q)).length;
  }

  autoAnswerQuestion(question: PendingQuestion): void {
    if (!this.jobId || this.isAutoAnswering(question.id)) return;

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

    if (this.submitEndpoint === 'planning-v3') {
      // Planning V3 API does not expose auto-answer; skip or use a future endpoint
      this.autoAnsweringQuestions.delete(question.id);
    } else if (this.submitEndpoint === 'product-analysis') {
      this.api.autoAnswerProductAnalysis(this.jobId, question.id).subscribe({
        next: handleSuccess,
        error: handleError,
      });
    } else {
      this.api.autoAnswerRunTeam(this.jobId, question.id).subscribe({
        next: handleSuccess,
        error: handleError,
      });
    }
  }

  applyAutoAnswer(questionId: string): void {
    const result = this.autoAnswerResults.get(questionId);
    if (!result || !this.jobId) return;

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

    this.getSubmitObservable(request).subscribe({
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

  private getSubmitObservable(
    request: { answers: AnswerSubmission[] }
  ): Observable<JobStatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse> {
    if (this.submitEndpoint === 'planning-v3') {
      const body = request.answers.map((a) => ({
        question_id: a.question_id,
        selected_option_id: a.selected_option_id ?? undefined,
        selected_option_ids: a.selected_option_ids,
        other_text: a.other_text ?? undefined,
      }));
      return this.planningV3Api.submitAnswers(this.jobId!, body);
    } else if (this.submitEndpoint === 'product-analysis') {
      return this.api.submitProductAnalysisAnswers(this.jobId!, request) as Observable<ProductAnalysisStatusResponse>;
    } else {
      return this.api.submitAnswers(this.jobId!, request);
    }
  }

  dismissAutoAnswer(questionId: string): void {
    this.autoAnswerResults.delete(questionId);
  }

  submitAnswers(): void {
    if (!this.jobId || !this.allRequiredAnswered) return;

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

    const handleSuccess = (response: JobStatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse): void => {
      this.submitting = false;
      this.answersSubmitted.emit(response);
    };

    const handleError = (err: { error?: { detail?: string }; message?: string }): void => {
      this.submitting = false;
      this.error = err?.error?.detail ?? err?.message ?? 'Failed to submit answers';
    };

    if (this.submitEndpoint === 'planning-v3') {
      const body = request.answers.map((a) => ({
        question_id: a.question_id,
        selected_option_id: a.selected_option_id ?? undefined,
        selected_option_ids: a.selected_option_ids,
        other_text: a.other_text ?? undefined,
      }));
      this.planningV3Api.submitAnswers(this.jobId, body).subscribe({
        next: handleSuccess,
        error: handleError,
      });
    } else if (this.submitEndpoint === 'product-analysis') {
      this.api.submitProductAnalysisAnswers(this.jobId, request).subscribe({
        next: (res) => handleSuccess(res as ProductAnalysisStatusResponse),
        error: handleError,
      });
    } else {
      this.api.submitAnswers(this.jobId, request).subscribe({
        next: handleSuccess,
        error: handleError,
      });
    }
  }
}
