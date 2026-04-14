import {
  Component,
  Input,
  OnInit,
  OnDestroy,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { Subscription, timer, EMPTY } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import type { PersonaChatMessage } from '../../models';

const POLL_MS = 5_000;

@Component({
  selector: 'app-persona-chat',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './persona-chat.component.html',
  styleUrl: './persona-chat.component.scss',
})
export class PersonaChatComponent implements OnInit, OnDestroy, AfterViewChecked {
  @Input() runId = '';
  @Input() isTerminal = false;

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(PersonaTestingApiService);
  private readonly fb = inject(FormBuilder);
  private pollSub: Subscription | null = null;

  messages: PersonaChatMessage[] = [];
  loading = false;
  sending = false;
  error: string | null = null;
  private maxSeenId = 0;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    if (!this.runId) return;

    this.pollSub = timer(0, POLL_MS)
      .pipe(
        switchMap(() => {
          if (this.sending) return EMPTY;
          return this.api.getChatHistory(this.runId, this.maxSeenId);
        }),
      )
      .subscribe({
        next: (resp) => {
          if (resp.messages.length > 0) {
            this.messages = [...this.messages, ...resp.messages];
            this.maxSeenId = Math.max(
              this.maxSeenId,
              ...resp.messages.map((m) => m.message_id),
            );
          }
        },
        error: (err) => {
          this.error = err?.error?.detail ?? 'Failed to load chat';
        },
      });
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
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

  onSubmit(): void {
    if (this.form.invalid || this.sending) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;

    this.form.reset({ message: '' });
    this.sending = true;
    this.error = null;

    // Optimistic user message
    const optimistic: PersonaChatMessage = {
      message_id: -1,
      role: 'user',
      content: message,
      message_type: 'chat',
      timestamp: new Date().toISOString(),
    };
    this.messages = [...this.messages, optimistic];

    this.api.sendChatMessage(this.runId, message).subscribe({
      next: (resp) => {
        // Replace all messages with authoritative list
        this.messages = resp.messages;
        this.maxSeenId = Math.max(0, ...resp.messages.map((m) => m.message_id));
        this.sending = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? 'Failed to send message';
        // Remove optimistic message
        this.messages = this.messages.filter((m) => m.message_id !== -1);
        this.sending = false;
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

  messageTypeIcon(type: string): string {
    switch (type) {
      case 'question_received':
        return 'help_outline';
      case 'answer_given':
        return 'check_circle_outline';
      case 'status_update':
        return 'info_outline';
      default:
        return '';
    }
  }
}
