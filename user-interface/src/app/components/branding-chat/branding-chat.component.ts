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
import { MatChipsModule } from '@angular/material/chips';
import { BrandingApiService } from '../../services/branding-api.service';
import type {
  BrandingMissionSnapshot,
  BrandingTeamOutput,
  ConversationMessage,
  ConversationStateResponse,
} from '../../models';

export interface BrandingChatState {
  mission: BrandingMissionSnapshot;
  latest_output: BrandingTeamOutput | null;
}

@Component({
  selector: 'app-branding-chat',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
  ],
  templateUrl: './branding-chat.component.html',
  styleUrl: './branding-chat.component.scss',
})
export class BrandingChatComponent implements OnInit, AfterViewChecked {
  @Input() conversationId: string | null = null;
  @Output() stateChange = new EventEmitter<BrandingChatState>();

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(BrandingApiService);
  private readonly fb = inject(FormBuilder);

  messages: ConversationMessage[] = [];
  mission: BrandingMissionSnapshot | null = null;
  latestOutput: BrandingTeamOutput | null = null;
  suggestedQuestions: string[] = [];
  loading = false;
  error: string | null = null;
  private _conversationId: string | null = null;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  private static readonly CONVERSATION_UNAVAILABLE_MESSAGE =
    "Couldn't start the conversation. Check that the backend is running and the Branding API is available.";

  private isUnreachableError(err: { status?: number }): boolean {
    return err?.status === 404 || err?.status === 0;
  }

  ngOnInit(): void {
    if (this.conversationId) {
      this._conversationId = this.conversationId;
      this.api.getConversation(this.conversationId).subscribe({
        next: (res) => this.applyState(res),
        error: (err) => {
          this.error = this.isUnreachableError(err)
            ? BrandingChatComponent.CONVERSATION_UNAVAILABLE_MESSAGE
            : (err?.error?.detail ?? err?.message ?? 'Failed to load conversation');
        },
      });
    } else {
      this.api.createConversation().subscribe({
        next: (res) => this.applyState(res),
        error: (err) => {
          this.error = this.isUnreachableError(err)
            ? BrandingChatComponent.CONVERSATION_UNAVAILABLE_MESSAGE
            : (err?.error?.detail ?? err?.message ?? 'Failed to start conversation');
        },
      });
    }
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

  private applyState(res: ConversationStateResponse): void {
    this.messages = res.messages ?? [];
    this.mission = res.mission ?? null;
    this.latestOutput = res.latest_output ?? null;
    this.suggestedQuestions = res.suggested_questions ?? [];
    if (res.conversation_id) {
      this._conversationId = res.conversation_id;
    }
    this.emitState();
  }

  private emitState(): void {
    if (this.mission) {
      this.stateChange.emit({
        mission: this.mission,
        latest_output: this.latestOutput,
      });
    }
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

  retryStartConversation(): void {
    this.error = null;
    this.loading = true;
    this.api.createConversation().subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading = false;
      },
      error: (err) => {
        this.error = this.isUnreachableError(err)
          ? BrandingChatComponent.CONVERSATION_UNAVAILABLE_MESSAGE
          : (err?.error?.detail ?? err?.message ?? 'Failed to start conversation');
        this.loading = false;
      },
    });
  }

  private sendMessage(message: string): void {
    const cid = this._conversationId;
    this.form.reset({ message: '' });

    if (!cid) {
      this.error = null;
      this.messages = [
        ...this.messages,
        { role: 'user', content: message, timestamp: new Date().toISOString() },
      ];
      this.loading = true;
      this.api.createConversation(message).subscribe({
        next: (res) => {
          this.applyState(res);
          this.error = null;
          this.loading = false;
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to send message';
          this.loading = false;
        },
      });
      return;
    }

    this.messages = [
      ...this.messages,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ];
    this.loading = true;
    this.error = null;
    this.api.sendConversationMessage(cid, message).subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to send message';
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
}
