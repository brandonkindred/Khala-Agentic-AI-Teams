import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnInit,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
} from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import type {
  TeamAssistantMessage,
  TeamAssistantConversationState,
} from '../../models/team-assistant.model';

@Component({
  selector: 'app-team-assistant-chat',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './team-assistant-chat.component.html',
  styleUrl: './team-assistant-chat.component.scss',
})
export class TeamAssistantChatComponent implements OnInit, AfterViewChecked {
  /** Base URL for the team's assistant API, e.g. '/api/soc2-compliance/assistant'. */
  @Input() teamApiUrl = '';
  /** Display name shown in the card header. */
  @Input() teamName = 'Assistant';
  /** Short description shown below the title. */
  @Input() teamDescription = '';
  /** Emitted when all required fields are collected and the user clicks Launch. */
  @Output() launchWorkflow = new EventEmitter<Record<string, unknown>>();

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(TeamAssistantApiService);
  private readonly fb = inject(FormBuilder);

  messages: TeamAssistantMessage[] = [];
  context: Record<string, unknown> = {};
  suggestedQuestions: string[] = [];
  loading = false;
  error: string | null = null;
  ready = false;
  missingFields: string[] = [];

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    this.loadConversation();
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  onSubmit(): void {
    if (this.form.invalid || this.loading) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedQuestion(question: string): void {
    this.sendMessage(question);
  }

  onLaunch(): void {
    this.launchWorkflow.emit({ ...this.context });
  }

  retryLoad(): void {
    this.error = null;
    this.loadConversation();
  }

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      const d = new Date(timestamp);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  contextKeys(): string[] {
    return Object.keys(this.context).filter(k => this.context[k] != null && this.context[k] !== '');
  }

  // --- private ---

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  private applyState(res: TeamAssistantConversationState): void {
    this.messages = res.messages ?? [];
    this.context = res.context ?? {};
    this.suggestedQuestions = res.suggested_questions ?? [];
    this.checkReadiness();
  }

  private loadConversation(): void {
    if (!this.teamApiUrl) return;
    this.loading = true;
    this.api.getConversation(this.teamApiUrl).subscribe({
      next: res => {
        this.applyState(res);
        this.loading = false;
      },
      error: err => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to load conversation';
        this.loading = false;
      },
    });
  }

  private sendMessage(message: string): void {
    if (!this.teamApiUrl) return;
    this.form.reset({ message: '' });
    this.messages = [
      ...this.messages,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ];
    this.loading = true;
    this.error = null;
    this.api.sendMessage(this.teamApiUrl, message).subscribe({
      next: res => {
        this.applyState(res);
        this.loading = false;
      },
      error: err => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to send message';
        this.loading = false;
      },
    });
  }

  private checkReadiness(): void {
    this.api.getReadiness(this.teamApiUrl).subscribe({
      next: res => {
        this.ready = res.ready;
        this.missingFields = res.missing_fields ?? [];
      },
      error: () => {
        this.ready = false;
      },
    });
  }
}
