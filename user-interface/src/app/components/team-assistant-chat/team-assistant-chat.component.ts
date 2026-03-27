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
export class TeamAssistantChatComponent implements OnInit, AfterViewChecked {
  @Input() teamApiUrl = '';
  @Input() teamName = 'Assistant';
  @Input() teamDescription = '';
  /** Field definitions — drives the right-side form panel. */
  @Input() fields: TeamAssistantFieldSpec[] = [];
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
    this.launchWorkflow.emit({ ...this.context });
  }

  retryLoad(): void {
    this.error = null;
    this.loadConversation();
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
    // Push to backend
    this.api.updateContext(this.teamApiUrl, { [key]: value }).subscribe({
      next: res => {
        this.context = res.context ?? this.context;
        this.checkReadiness();
      },
      error: () => { /* Context update failed silently; local state is already set */ },
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
    this.chatForm.reset({ message: '' });
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
