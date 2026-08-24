import { Component, Input, inject, OnInit, ViewChild, ElementRef, AfterViewChecked } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { AssistantMessage } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Chat component for conversational interaction with the Personal Assistant.
 */
@Component({
  selector: 'app-pa-chat',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatChipsModule,
  ],
  templateUrl: './pa-chat.component.html',
  styleUrl: './pa-chat.component.scss',
})
export class PaChatComponent implements OnInit, AfterViewChecked {
  @Input() userId = 'default';
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);

  messages: AssistantMessage[] = [];
  loading = false;
  form: FormGroup;

  quickActions = [
    { label: "Today's events", message: "What's on my calendar today?" },
    { label: 'My tasks', message: 'Show my tasks' },
    { label: 'Find deals', message: 'Find deals for my wishlist' },
    { label: 'Grocery list', message: 'Create a grocery list' },
  ];

  constructor() {
    this.form = this.fb.nonNullable.group({
      message: ['', [Validators.required, Validators.minLength(1)]],
    });
  }

  ngOnInit(): void {
    this.messages.push({
      role: 'assistant',
      content: `Hello! I'm your personal assistant. I can help you with:

• Managing your tasks and grocery lists
• Scheduling calendar events
• Finding deals on items you want
• Making reservations
• Generating documents and checklists
• Learning your preferences to serve you better

How can I help you today?`,
      timestamp: new Date().toISOString(),
    });
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  private scrollToBottom(): void {
    if (this.messagesContainer) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  onSubmit(): void {
    if (this.form.valid && !this.loading) {
      const message = this.form.getRawValue().message.trim();
      this.sendMessage(message);
    }
  }

  onQuickAction(action: { label: string; message: string }): void {
    this.sendMessage(action.message);
  }

  private sendMessage(message: string): void {
    this.messages.push({
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    });
    this.form.reset();
    this.loading = true;

    this.api.sendMessage(this.userId, { message }).subscribe({
      next: (res) => {
        this.messages.push({
          role: 'assistant',
          content: res.response || res.message || 'I processed your request.',
          timestamp: res.timestamp || new Date().toISOString(),
        });
        this.loading = false;
      },
      error: (err) => {
        this.messages.push({
          role: 'assistant',
          content: `Sorry, something went wrong: ${extractErrorDetail(err, 'Unknown error')}`,
          timestamp: new Date().toISOString(),
        });
        this.loading = false;
      },
    });
  }

  formatTime(timestamp: string): string {
    return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
}
