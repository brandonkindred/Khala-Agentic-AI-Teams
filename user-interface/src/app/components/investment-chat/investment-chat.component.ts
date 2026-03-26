import {
  Component,
  EventEmitter,
  Input,
  Output,
  inject,
  OnInit,
  ViewChild,
  ElementRef,
  AfterViewChecked,
} from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { InvestmentApiService } from '../../services/investment-api.service';
import type { AdvisorChatMessage, IPS } from '../../models';

@Component({
  selector: 'app-investment-chat',
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
  templateUrl: './investment-chat.component.html',
  styleUrl: './investment-chat.component.scss',
})
export class InvestmentChatComponent implements OnInit, AfterViewChecked {
  @Input() userId = 'default';
  @Output() profileCreated = new EventEmitter<IPS>();
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(InvestmentApiService);
  private readonly fb = inject(FormBuilder);

  messages: AdvisorChatMessage[] = [];
  loading = false;
  sessionId: string | null = null;
  sessionStatus: 'active' | 'completed' | 'awaiting_confirmation' | null = null;
  currentTopic: string | null = null;
  missingFields: string[] = [];
  form: FormGroup;

  quickActions = [
    { label: 'Build my profile', message: "I'd like to set up my investment profile." },
    { label: 'Review my portfolio', message: 'Can you review my current portfolio allocation?' },
    { label: 'Strategy ideas', message: 'What trading strategies would you recommend for my risk level?' },
    { label: 'Market outlook', message: "What's your current market outlook?" },
  ];

  constructor() {
    this.form = this.fb.nonNullable.group({
      message: ['', [Validators.required, Validators.minLength(1)]],
    });
  }

  ngOnInit(): void {
    this.messages.push({
      role: 'assistant',
      content: `Hello! I'm your investment advisor. I can help you with:

\u2022 Building your Investment Policy Statement (IPS)
\u2022 Reviewing portfolio allocations and proposals
\u2022 Discussing trading strategies and risk management
\u2022 Answering questions about your investment options

What would you like to work on today?`,
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

  onConfirmProfile(): void {
    if (!this.sessionId) return;
    this.loading = true;
    this.api.completeAdvisorSession(this.sessionId).subscribe({
      next: (res) => {
        this.messages.push({
          role: 'assistant',
          content: res.message || 'Your Investment Policy Statement has been created successfully!',
          timestamp: new Date().toISOString(),
        });
        this.sessionStatus = 'completed';
        this.loading = false;
        if (res.ips) {
          this.profileCreated.emit(res.ips);
        }
      },
      error: (err) => {
        this.messages.push({
          role: 'assistant',
          content: `Sorry, I couldn't finalize the profile: ${err?.error?.detail || err?.message || 'Unknown error'}`,
          timestamp: new Date().toISOString(),
        });
        this.loading = false;
      },
    });
  }

  private sendMessage(message: string): void {
    this.messages.push({
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    });
    this.form.reset();
    this.loading = true;

    if (!this.sessionId) {
      this.startSessionAndSend(message);
    } else {
      this.sendToSession(message);
    }
  }

  private startSessionAndSend(message: string): void {
    this.api.startAdvisorSession({ user_id: this.userId }).subscribe({
      next: (res) => {
        this.sessionId = res.session_id;
        this.updateSessionState(res);
        // Now send the actual message
        this.sendToSession(message);
      },
      error: (err) => {
        this.handleError(err);
      },
    });
  }

  private sendToSession(message: string): void {
    this.api.sendAdvisorMessage(this.sessionId!, { message }).subscribe({
      next: (res) => {
        this.updateSessionState(res);
        this.messages.push({
          role: 'assistant',
          content: res.advisor_message,
          timestamp: new Date().toISOString(),
        });
        this.loading = false;
      },
      error: (err) => {
        this.handleError(err);
      },
    });
  }

  private updateSessionState(res: { session_status: string; current_topic?: string; missing_fields?: string[] }): void {
    this.sessionStatus = res.session_status as 'active' | 'completed' | 'awaiting_confirmation';
    this.currentTopic = res.current_topic ?? null;
    this.missingFields = res.missing_fields ?? [];
  }

  private handleError(err: { error?: { detail?: string }; message?: string }): void {
    this.messages.push({
      role: 'assistant',
      content: `Sorry, something went wrong: ${err?.error?.detail || err?.message || 'Unknown error'}`,
      timestamp: new Date().toISOString(),
    });
    this.loading = false;
  }

  formatTime(timestamp: string): string {
    return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
}
