import {
  Component,
  OnInit,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { StartupAdvisorApiService } from '../../services/startup-advisor-api.service';
import type {
  StartupAdvisorMessage,
  StartupAdvisorArtifact,
} from '../../models';

@Component({
  selector: 'app-startup-advisor-dashboard',
  standalone: true,
  imports: [
    JsonPipe,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
  ],
  templateUrl: './startup-advisor-dashboard.component.html',
  styleUrl: './startup-advisor-dashboard.component.scss',
})
export class StartupAdvisorDashboardComponent implements OnInit, AfterViewChecked {
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(StartupAdvisorApiService);
  private readonly fb = inject(FormBuilder);

  messages: StartupAdvisorMessage[] = [];
  artifacts: StartupAdvisorArtifact[] = [];
  context: Record<string, unknown> = {};
  suggestedQuestions: string[] = [];
  loading = false;
  error: string | null = null;
  conversationId: string | null = null;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    this.loadConversation();
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  private loadConversation(): void {
    this.loading = true;
    this.error = null;
    this.api.getConversation().subscribe({
      next: (state) => {
        this.conversationId = state.conversation_id;
        this.messages = state.messages;
        this.artifacts = state.artifacts;
        this.context = state.context;
        this.suggestedQuestions = state.suggested_questions;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.status === 0 || err?.status === 404
          ? 'Could not connect to the Startup Advisor service. Check that the backend is running.'
          : (err?.error?.detail ?? err?.message ?? 'Failed to load conversation.');
        this.loading = false;
      },
    });
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

  retryConnect(): void {
    this.loadConversation();
  }

  private sendMessage(message: string): void {
    this.form.reset({ message: '' });
    this.messages = [
      ...this.messages,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ];
    this.loading = true;
    this.error = null;

    this.api.sendMessage(message).subscribe({
      next: (state) => {
        this.messages = state.messages;
        this.artifacts = state.artifacts;
        this.context = state.context;
        this.suggestedQuestions = state.suggested_questions;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to send message.';
        this.loading = false;
      },
    });
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

  formatContextKey(key: string): string {
    return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  contextEntries(): [string, unknown][] {
    return Object.entries(this.context).filter(([, v]) => v != null && v !== '');
  }

  formatArtifactType(type: string): string {
    return type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }
}
