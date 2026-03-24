import { Component, EventEmitter, Input, Output, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatRadioModule } from '@angular/material/radio';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import type { PendingQuestion, AutoAnswerResponse } from '../../../models';

@Component({
  selector: 'app-question-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    FormsModule,
    MatCheckboxModule,
    MatRadioModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatFormFieldModule,
    MatInputModule,
  ],
  templateUrl: './question-card.component.html',
  styleUrl: './question-card.component.scss',
})
export class QuestionCardComponent {
  @Input({ required: true }) question!: PendingQuestion;
  @Input({ required: true }) questionIndex!: number;
  @Input() autoAnswerResult?: AutoAnswerResponse;
  @Input() isAutoAnswering = false;
  @Input() isAnswered = false;

  @Output() optionToggled = new EventEmitter<{ optionId: string; checked: boolean }>();
  @Output() otherTextChanged = new EventEmitter<string>();
  @Output() autoAnswerRequested = new EventEmitter<void>();
  @Output() autoAnswerApplied = new EventEmitter<void>();
  @Output() autoAnswerDismissed = new EventEmitter<void>();

  selectedOptionIds = new Set<string>();
  otherText = '';
  wasAutoAnswered = false;
  autoAnswerConfidence = 0;

  get isMultiSelect(): boolean {
    return this.question.allow_multiple !== false;
  }

  onOptionToggle(optionId: string, checked: boolean): void {
    if (!this.isMultiSelect && checked) {
      // Single-select: clear all other selections, then set the new one.
      this.selectedOptionIds.clear();
      this.otherText = '';
      this.selectedOptionIds.add(optionId);
    } else if (checked) {
      this.selectedOptionIds.add(optionId);
    } else {
      this.selectedOptionIds.delete(optionId);
      if (optionId === 'other') {
        this.otherText = '';
      }
    }
    this.wasAutoAnswered = false;
    this.optionToggled.emit({ optionId, checked });
  }

  onRadioChange(optionId: string): void {
    this.selectedOptionIds.clear();
    this.otherText = '';
    this.selectedOptionIds.add(optionId);
    this.wasAutoAnswered = false;
    this.optionToggled.emit({ optionId, checked: true });
  }

  isOptionSelected(optionId: string): boolean {
    return this.selectedOptionIds.has(optionId);
  }

  isOtherSelected(): boolean {
    return this.selectedOptionIds.has('other');
  }

  onOtherTextChange(text: string): void {
    this.otherText = text;
    this.otherTextChanged.emit(text);
  }

  onAutoAnswerRequest(): void {
    this.autoAnswerRequested.emit();
  }

  onApplyAutoAnswer(): void {
    this.autoAnswerApplied.emit();
  }

  onDismissAutoAnswer(): void {
    this.autoAnswerDismissed.emit();
  }

  getConfidenceLabel(confidence: number): string {
    if (confidence >= 0.8) return 'High';
    if (confidence >= 0.6) return 'Medium';
    return 'Low';
  }

  getSelectedOptionIds(): string[] {
    return Array.from(this.selectedOptionIds);
  }

  hasSelections(): boolean {
    return this.selectedOptionIds.size > 0;
  }
}
