import {
  Component,
  OnInit,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { FormBuilder, FormGroup, FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { StartupAdvisorApiService } from '../../services/startup-advisor-api.service';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import {
  STARTUP_ADVISOR_PROFILE_FIELDS,
  type StartupAdvisorMessage,
  type StartupAdvisorArtifact,
} from '../../models';

type InteractionMode = 'chat' | 'form';

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
    MatButtonToggleModule,
    MatSnackBarModule,
    DashboardShellComponent,
  ],
  templateUrl: './startup-advisor-dashboard.component.html',
  styleUrl: './startup-advisor-dashboard.component.scss',
})
export class StartupAdvisorDashboardComponent implements OnInit, AfterViewChecked {
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(StartupAdvisorApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  readonly profileFields = STARTUP_ADVISOR_PROFILE_FIELDS;

  messages: StartupAdvisorMessage[] = [];
  artifacts: StartupAdvisorArtifact[] = [];
  context: Record<string, unknown> = {};
  suggestedQuestions: string[] = [];
  loading = false;
  error: string | null = null;
  conversationId: string | null = null;

  mode: InteractionMode = 'chat';

  /** Chat input form */
  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  /** Manual profile form — built dynamically from profileFields */
  profileForm!: FormGroup;

  /** Tracks which sidebar context field is being inline-edited */
  editingContextKey: string | null = null;
  editingContextValue = '';

  savingProfile = false;

  ngOnInit(): void {
    this.buildProfileForm();
    this.loadConversation();
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  private buildProfileForm(): void {
    const controls: Record<string, FormControl<string>> = {};
    for (const field of this.profileFields) {
      controls[field.key] = new FormControl('', { nonNullable: true });
    }
    this.profileForm = new FormGroup(controls);
  }

  private syncProfileFormFromContext(): void {
    for (const field of this.profileFields) {
      const val = this.context[field.key];
      if (val != null && val !== '') {
        this.profileForm.get(field.key)?.setValue(String(val), { emitEvent: false });
      }
    }
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
        this.syncProfileFormFromContext();
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

  // -- Chat mode --

  onSubmit(): void {
    if (this.form.invalid || this.loading) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedQuestion(question: string): void {
    this.mode = 'chat';
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
        this.syncProfileFormFromContext();
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to send message.';
        this.loading = false;
      },
    });
  }

  // -- Manual form mode --

  onSaveProfile(): void {
    const values: Record<string, string> = {};
    for (const field of this.profileFields) {
      const val = (this.profileForm.get(field.key)?.value ?? '').trim();
      if (val) {
        values[field.key] = val;
      }
    }

    if (Object.keys(values).length === 0) return;

    this.savingProfile = true;
    this.error = null;

    this.api.updateContext(values).subscribe({
      next: (state) => {
        this.context = state.context;
        this.messages = state.messages;
        this.artifacts = state.artifacts;
        this.suggestedQuestions = state.suggested_questions;
        this.syncProfileFormFromContext();
        this.savingProfile = false;
        this.snackBar.open('Profile updated', 'OK', { duration: 2500 });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to update profile.';
        this.savingProfile = false;
      },
    });
  }

  /** Count how many profile form fields have values */
  filledFieldCount(): number {
    let count = 0;
    for (const field of this.profileFields) {
      if ((this.profileForm.get(field.key)?.value ?? '').trim()) count++;
    }
    return count;
  }

  // -- Sidebar inline editing --

  startEditContext(key: string, value: unknown): void {
    this.editingContextKey = key;
    this.editingContextValue = String(value ?? '');
  }

  cancelEditContext(): void {
    this.editingContextKey = null;
    this.editingContextValue = '';
  }

  saveEditContext(): void {
    if (!this.editingContextKey) return;
    const key = this.editingContextKey;
    const val = this.editingContextValue.trim();

    this.editingContextKey = null;
    this.editingContextValue = '';

    if (!val) return;

    this.api.updateContext({ [key]: val }).subscribe({
      next: (state) => {
        this.context = state.context;
        this.syncProfileFormFromContext();
      },
      error: (err) => {
        this.snackBar.open(err?.error?.detail ?? 'Failed to update field.', 'OK', { duration: 3000 });
      },
    });
  }

  // -- Formatting helpers --

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
