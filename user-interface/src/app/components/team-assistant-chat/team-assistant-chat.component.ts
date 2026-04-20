import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnInit,
  OnChanges,
  SimpleChanges,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import type {
  TeamAssistantMessage,
  TeamAssistantConversationState,
  TeamAssistantFieldSpec,
} from '../../models/team-assistant.model';

@Component({
  selector: 'app-team-assistant-chat',
  standalone: true,
  imports: [
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
  ],
  templateUrl: './team-assistant-chat.component.html',
  styleUrl: './team-assistant-chat.component.scss',
})
export class TeamAssistantChatComponent implements OnInit, OnChanges, AfterViewChecked {
  @Input() teamApiUrl = '';
  @Input() teamName = 'Assistant';
  @Input() teamDescription = '';
  /** Field definitions — drives the right-side form panel. */
  @Input() fields: TeamAssistantFieldSpec[] = [];
  /** When set, use this specific conversation instead of the singleton. */
  @Input() conversationId: string | null = null;
  /** Hide the "Launch workflow" button entirely (teams with no runnable workflow). */
  @Input() hideLaunchButton = false;

  /**
   * Emitted after the backend launch completes. For async teams, consumers
   * typically read ``job_id`` and navigate to a jobs view. For synchronous
   * teams (market_research, branding, nutrition, deepthought, road_trip,
   * agentic_team_provisioning, investment), ``job_id`` is ``null`` and the
   * actual results are carried in ``upstream_body`` — dashboards that
   * embed a sync team should read that directly.
   */
  @Output() workflowLaunched = new EventEmitter<{
    job_id: string | null;
    conversation_id: string;
    upstream_status: number;
    upstream_body: Record<string, unknown>;
  }>();
  /** Emitted when a conversation is loaded/created, so parent can track the ID. */
  @Output() conversationLoaded = new EventEmitter<string>();

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

  /** Field currently being edited (key), or null if none. */
  editingField: string | null = null;
  /** Draft value while editing a field. */
  editingValue = '';

  chatForm = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    this.loadConversation();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['conversationId'] && !changes['conversationId'].firstChange) {
      this.loadConversation();
    }
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  // --- Chat actions ---

  onSubmit(): void {
    if (this.chatForm.invalid || this.loading) return;
    const message = this.chatForm.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedQuestion(question: string): void {
    this.sendMessage(question);
  }

  onLaunch(): void {
    if (!this.teamApiUrl) return;
    this.loading = true;
    this.error = null;
    this.api.launch(this.teamApiUrl, this.conversationId ?? undefined).subscribe({
      next: (r) => {
        this.loading = false;
        this.workflowLaunched.emit({
          job_id: r.job_id,
          conversation_id: r.conversation_id,
          upstream_status: r.upstream_status,
          upstream_body: r.upstream_body,
        });
      },
      error: (err) => {
        this.loading = false;
        const detail = err?.error?.detail;
        if (detail && typeof detail === 'object') {
          this.error = detail.error === 'missing_required_fields'
            ? `Still missing: ${(detail.missing ?? []).join(', ')}`
            : detail.message ?? JSON.stringify(detail);
        } else {
          this.error = detail ?? err?.message ?? 'Failed to launch workflow';
        }
      },
    });
  }

  retryLoad(): void {
    this.error = null;
    this.loadConversation();
  }

  /** Reset the conversation to start fresh. Can be called by the parent component. */
  resetConversation(): void {
    if (!this.teamApiUrl) return;
    this.loading = true;
    this.error = null;
    this.api.resetConversation(this.teamApiUrl, this.conversationId ?? undefined).subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to reset conversation';
        this.loading = false;
      },
    });
  }

  // --- Inline field editing ---

  startEdit(fieldKey: string): void {
    this.editingField = fieldKey;
    const current = this.context[fieldKey];
    this.editingValue = current != null ? String(current) : '';
  }

  saveEdit(): void {
    if (!this.editingField) return;
    const key = this.editingField;
    const value = this.editingValue.trim();
    this.context = { ...this.context, [key]: value || undefined };
    this.editingField = null;
    this.editingValue = '';
    this.api.updateContext(this.teamApiUrl, { [key]: value }, this.conversationId ?? undefined).subscribe({
      next: res => {
        this.context = res.context ?? this.context;
        this.checkReadiness();
      },
      error: () => { /* context fetch is best-effort */ },
    });
  }

  cancelEdit(): void {
    this.editingField = null;
    this.editingValue = '';
  }

  fieldValue(key: string): string {
    const v = this.context[key];
    return v != null && v !== '' ? String(v) : '';
  }

  isFieldFilled(key: string): boolean {
    const v = this.context[key];
    return v != null && v !== '';
  }

  // --- Helpers ---

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  // --- Private ---

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
    if (res.conversation_id) {
      this.conversationLoaded.emit(res.conversation_id);
    }
    this.checkReadiness();
  }

  private loadConversation(): void {
    if (!this.teamApiUrl) return;
    this.loading = true;
    this.api.getConversation(this.teamApiUrl, this.conversationId ?? undefined).subscribe({
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
    this.chatForm.reset({ message: '' });
    this.messages = [
      ...this.messages,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ];
    this.loading = true;
    this.error = null;
    this.api.sendMessage(this.teamApiUrl, message, this.conversationId ?? undefined).subscribe({
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
    this.api.getReadiness(this.teamApiUrl, this.conversationId ?? undefined).subscribe({
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
